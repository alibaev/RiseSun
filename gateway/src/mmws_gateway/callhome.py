"""Серверный (call-home) транспорт для счётчиков, самостоятельно
инициирующих TCP-соединение к Gateway — подтверждённое на практике
2026-08-18 расхождение с ТЗ Table 1 (там Gateway — инициатор), см.
DECISIONS.md, разделы про звонящие домой счётчики Risesun.

Поведение счётчика, подтверждённое на реальном оборудовании:

1. Счётчик сам открывает TCP-соединение к Gateway и, если ничего не
   получает в ответ сразу, периодически шлёт кадр в конверте DL/T645
   (``0x68 ... 0x16``) — это не протокол чтения, а что-то вроде
   heartbeat/анонса; полноценный HDLC/DLMS-обмен с этим конвертом
   ничего общего не имеет и не проходит через него (см. DECISIONS.md,
   «Прорыв: текстовые логи legacy-приложения расшифрованы»).
2. Счётчик держит до ``WINDOW_SIZE`` таких соединений одновременно; при
   попытке открыть ещё одно Gateway обязан закрыть САМОЕ СТАРОЕ из уже
   открытых (скользящее окно, а не «принять N и перестать слушать»).
3. У каждого held-соединения есть ограниченное "окно жизни" от момента
   подключения — весь обмен SNRM->UA->AARQ->AARE->GET нужно успеть
   провести целиком, пока оно не истекло; попытка растянуть этот обмен
   во времени (например, сначала заранее провести SNRM, а через
   несколько минут — отдельно AARQ) не работает: соединение продолжает
   технически отвечать на HDLC-уровне (RR-подтверждения на каждый
   присланный I-кадр), но реального ответа приложения (AARE/GET-response)
   уже не даёт — подтверждено многократно живым чтением 2026-08-18, см.
   DECISIONS.md. Поэтому SNRM и последующий AARQ/GET всегда выполняются
   ОДНИМ непрерывным заходом (``hdlc_dlms.read_register``), а не
   отдельными фазами, разнесёнными по времени. Один-единственный такой
   заход сразу после подключения тоже обычно ответа не даёт — нужны
   именно повторные попытки с интервалом в несколько секунд, поэтому
   заход повторяется целиком (со свежим SNRM) несколько раз на одном
   соединении, прежде чем перейти к другому.

Ключевое наблюдение для адресации: 6-байтный адрес внутри DL/T645-кадра
восстанавливается в десятичный серийный номер счётчика реверсом
порядка байт (см. ``serial_from_dlt645_address``) — подтверждено на
обоих проверенных реальных счётчиках. Это позволяет Gateway опознавать
звонящий счётчик без заранее заданного справочника серийных номеров.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from .errors import GatewayError, NoConnectionEstablishedError

logger = logging.getLogger("mmws_gateway.callhome")


# Найдено 2026-09-08 при разборе таймаутов AARQ/AARE: окно в 10
# ГЛОБАЛЬНО на весь пул (см. `_admit`/`claim` — фильтрация по serial
# идёт по всему self._pool, вытеснение при переполнении не привязано
# к конкретному счётчику) подбиралось во времена, когда одновременно
# было активно на порядок меньше счётчиков. При реальном парке ~150
# счётчиков (см. DECISIONS.md, «Массовая активация 141 счётчика»)
# окно=10 держит одновременно held лишь ~7% парка — Gateway вынужден
# принудительно закрывать (`evicted.raw_sock.close()`) почти каждое
# новое соединение почти сразу после приёма (подтверждено tcpdump +
# логами 2026-09-08: ~51к принятых/эвикшенов за 2ч при 138 различных
# IP, распределение равномерное — каждый счётчик реконнектится каждые
# ~6-7с, не единичный "шумный" счётчик). Форсированный обрыв TCP-сессии
# каждые несколько секунд не даёт GPRS-модему счётчика "отдышаться" для
# полноценного HDLC/DLMS-обмена — правдоподобная причина того, что
# AARE не приходит даже на УЖЕ claim()-нутых (номинально защищённых от
# вытеснения) соединениях. Увеличено 300 (с запасом на рост парка) ->
# 1000 (по прямой просьбе пользователя 2026-09-08, тот же запас с
# кратно большим потолком — см. DEFAULT_MAX_PER_SERIAL ниже про новый
# per-meter лимит, введённый тем же изменением).
DEFAULT_WINDOW_SIZE = 1000
# По просьбе пользователя 2026-09-08: сколько соединений ОДНОГО и того
# же счётчика могут одновременно занимать место в пуле — без этого
# лимита один "шумный" счётчик, агрессивно переоткрывающий соединения
# (см. docstring класса ниже, п.2 — реальное подтверждённое поведение),
# мог бы забить своими же повторами весь пул целиком, вытесняя чужие.
# Проверка — уже ПОСЛЕ опознания серийника (в момент admit серийник ещё
# не известен), см. ``CallHomePool._identify``.
DEFAULT_MAX_PER_SERIAL = 10
# Подтверждено на практике 2026-08-18 на заведомо СВЕЖЕМ (только что
# принятом, см. DEFAULT_MAX_CLAIM_AGE_S) соединении: ответ на SNRM
# приходит стабильно и предсказуемо — просто с задержкой около 7-10с
# (не мгновенно, но и не хаотично). Раньше при коротком таймауте на
# попытку (3с) и паузе перед повтором в 4с получался ровно такой же
# период ~7с у ВСЕГО цикла "попытка+пауза" — из-за чего ответ на
# СТАРЫЙ SNRM раз за разом приходил уже ПОСЛЕ того, как отправлялся
# СЛЕДУЮЩИЙ (см. историю в комментариях ниже и в DECISIONS.md), и
# ошибочно связывался с новой попыткой. Пауза сведена к минимуму, а
# таймаут на попытку увеличен настолько, чтобы реальный ответ успевал
# прийти В РАМКАХ ТОЙ ЖЕ попытки, которая его вызвала.
DEFAULT_RETRY_INTERVAL_S = 0.5
DEFAULT_IDENTIFY_TIMEOUT_S = 5.0
DEFAULT_PER_ATTEMPT_TIMEOUT_MS = 9000
# Сколько раз подряд повторить ВЕСЬ обмен (SNRM+AARQ+GET) на ОДНОМ и том
# же held-соединении, прежде чем сдаться на нём и перейти к следующему —
# большое значение, чтобы не переключаться на другое соединение раньше
# времени: см. комментарий выше про частые короткие попытки.
DEFAULT_MAX_ATTEMPTS_PER_CONNECTION = 20
# Подтверждено на практике 2026-08-18: held-соединение, простоявшее в
# пуле опознанным дольше примерно минуты без единой попытки чтения,
# оказывается уже полностью нежизнеспособным (не отвечает вообще ни на
# один SNRM). claim() с этим порогом просто не отдаёт такие соединения —
# вызывающий код ждёт следующее свежее вместо того, чтобы тратить время
# на заведомо мёртвое.
DEFAULT_MAX_CLAIM_AGE_S = 30.0
# Подтверждено побайтовым разбором tcpdump-захвата 2026-08-18: AARQ
# доходит до счётчика и подтверждается на уровне TCP (ACK) уже в первую
# секунду — то есть проблема НЕ в доставке запроса. После этого счётчик
# может молчать намного дольше, чем ~9с (наш прежний общий таймаут на
# попытку), прежде чем прислать AARE. Раз доставка уже подтверждена
# TCP-подтверждением, повторно слать SNRM тут бессмысленно (проверено:
# счётчик и так уже принял и обработал AARQ) — лучше просто терпеливо
# подождать ответ дольше именно на этом шаге, не начиная сессию заново.
DEFAULT_ASSOCIATION_TIMEOUT_MS = 45000
# Общий бюджет на ВСЕ held-соединения одного счётчика в событийном чтении
# (см. _maybe_trigger_immediate_read) — то же значение, что и старый FIFO-
# путь (max_wait_s в grpc_server.py). Найдено на практике 2026-09-10: все
# 154 исторических успешных чтения пришли через старый путь, который
# перебирает НЕСКОЛЬКО разных held-соединений одного счётчика подряд,
# пока не истечёт этот бюджет — а не через единственную попытку на самом
# свежем соединении. Событийное чтение до этого закрывало все соседние
# соединения счётчика сразу, оставляя себе только один "бросок кубика".
DEFAULT_IMMEDIATE_READ_MAX_WAIT_S = 150.0

# Эксперимент "эмуляция ver2.zip" (2026-09-10, см. DECISIONS.md — по
# просьбе пользователя буквально воспроизвести поведение декомпилированной
# заводской сервисной программы ``IECMeterManage.exe``, а не наши
# накопленные эвристики). Серийники в этом множестве при событийном чтении
# идут НЕ через обычный ``read_batch_via_fresh_connection``, а через тот
# же вызов с параметрами, byte-в-byte списанными с
# ``MeterDLMS.cs::Handclasp``/``TpDLMS.cs::organizeFrame_AARQ``: AARQ с
# client-max-receive-pdu-size=0 (не 2048), ожидание AARE 20с (не 5с) на
# попытку, до 3 попыток, БЕЗ безусловного DISC перед повтором AARQ (в
# декомпилированном коде DISC перед повтором шлётся не всегда, а только
# при рассинхронизации кадра). Пустое множество — заполняется точечно
# вручную для диагностики, не через конфиг (это разовый эксперимент, не
# постоянная фича).
# Эксперимент завершён 2026-09-10 (см. DECISIONS.md) — отрицательный
# результат, множество очищено. Механизм оставлен для повторных
# точечных экспериментов, если понадобится.
VER2_EMULATION_TEST_SERIALS: set[str] = set()

# 2026-09-12 (по просьбе пользователя "покопай почему data-access-error
# 250") — тот же принцип точечного эксперимента, что и у
# VER2_EMULATION_TEST_SERIALS выше: серийники в этом множестве вместо
# обычного _maybe_trigger_immediate_read получают ОДНОРАЗОВОЕ
# диагностическое чтение атрибута 3 (capture_objects) профиля нагрузки
# (см. read_profile_capture_objects_via_established_link) — полностью
# изолировано от job'ов/Backend, только логирует результат. Пусто по
# умолчанию — заполняется на время конкретного эксперимента.
CAPTURE_OBJECTS_DIAGNOSTIC_SERIALS: set[str] = set()

_DLT645_START = 0x68


def serial_from_dlt645_address(addr6: bytes) -> str:
    """Восстанавливает десятичный серийный номер из 6-байтного адреса
    DL/T645-подобного анонс-кадра. Адрес передаётся в обратном порядке
    байт (младший байт — первым); каждый байт — BCD-пара десятичных
    цифр. Подтверждено на обоих проверенных реальных счётчиках Risesun
    2026-08-18: ``52 23 00 01 20 20`` -> ``202001002352``,
    ``13 41 00 06 23 20`` -> ``202306004113``.
    """
    if len(addr6) != 6:
        raise ValueError(f"Ожидался 6-байтный адрес, получено {len(addr6)} байт")
    return "".join(f"{b:02x}" for b in reversed(addr6))


class DlT645FilteringSocket:
    """Обёртка над сокетом, прозрачно съедающая DL/T645-подобные кадры
    (``0x68 ... 0x16``) и одиночные нулевые байты (keepalive-пинги,
    подтверждённые в реальном трафике — см. DECISIONS.md), так что
    протокольный код видит только чистый поток HDLC (``0x7E...``).

    ВАЖНО (баг, найденный и исправленный 2026-08-18): фильтровать можно
    ТОЛЬКО байты НА ГРАНИЦЕ между кадрами — байт 0x00 совершенно законно
    встречается ВНУТРИ настоящего HDLC-кадра (например, в адресном поле
    UA-ответа), и фильтрация "всегда и везде" ломала такие кадры,
    съедая их байты как будто это keepalive-шум. Поэтому фильтрация
    активна только в режиме "ищу начало нового кадра" (``_seeking``);
    как только найден первый настоящий байт кадра, все ПОСЛЕДУЮЩИЕ
    байты этого кадра отдаются без какой-либо фильтрации.

    ВАЖНО (второй баг, найденный 2026-09-07, сообщение пользователя —
    при чтении профиля нагрузки счётчик "передаёт что-то нечитаемое, без
    чёткого начала и конца пакета", внешне похожее на кадр DL/T645, о
    котором см. DECISIONS.md — этот протокол и есть китайский
    национальный стандарт): раньше режим "поиска" включался заново
    ВРУЧНУЮ — вызовом ``reset_seeking()`` — и только перед КАЖДОЙ
    попыткой SNRM. После успешного SNRM/UA фильтрация оставалась
    выключенной НАВСЕГДА до конца TCP-соединения — если счётчик
    присылал очередной периодический DL/T645-анонс уже ПОСЛЕ
    установления HDLC-связи (между AARQ и AARE, между GET и
    GET-response, а особенно вероятно — в паузах многокадрового чтения
    профиля нагрузки, самой долгой операции), эти байты шли НАПРЯМУЮ в
    HDLC-парсер как будто это начало следующего кадра, что либо валило
    парсер сразу (``GatewayError`` "ожидался флаг 0x7E"), либо, если
    границы кадров совпадали неудачно, давало на вид нечитаемую мешанину
    без ясной границы начала/конца — то, что описал пользователь.
    Исправление: ``reset_seeking()`` теперь вызывается автоматически
    перед КАЖДЫМ кадром через хук
    ``transport.TcpTransport.reset_frame_seeking()``
    (``protocols.hdlc.read_frame_from_transport``) — вызывающему коду
    (см. ниже) вызывать его вручную больше не нужно.

    ВАЖНО (третий баг, найденный 2026-09-08 экспериментом с ожиданием
    AARE 150с вместо 45с, см. DECISIONS.md): ``socket.settimeout()`` в
    Python ограничивает КАЖДЫЙ отдельный ``recv()`` по отдельности, а не
    операцию целиком. Раньше вызывающий код (``read_via_call_home``)
    вызывал ``settimeout(association_timeout_ms)`` ОДИН раз перед
    ожиданием AARE, полагая, что это единый бюджет времени — но если по
    сокету прилетал ХОТЬ ОДИН байт (даже отфильтровываемый здесь как
    шум — например, keepalive-заглушка ``0x00``), каждый внутренний
    ``self._sock.recv()``, понадобившийся, чтобы этот байт съесть,
    заново получал ПОЛНЫЙ таймаут — реальное ожидание могло растянуться
    заметно дальше настроенного значения незаметно для логов
    (подтверждено байтовым разбором: 150с от AARQ фактически стали
    ~233с из-за одного шумового пакета). ``set_deadline()`` ниже даёт
    вызывающему коду абсолютный wall-clock дедлайн вместо этого:
    ``_raw_recv()`` — единственная точка, откуда происходит любое
    чтение из нижележащего сокета (напрямую или через
    ``_raw_recv_exact``/``recv``), — пересчитывает остаток времени и
    выставляет ``settimeout()`` заново ПЕРЕД каждым отдельным вызовом,
    так что накопленное время не может незаметно продлеваться шумом.

    ВАЖНО (четвёртая находка, 2026-09-10, см. DECISIONS.md — полный
    аудит ``ver2.zip``): раньше нулевые keepalive-байты и DL/T645-анонсы
    молча съедались, без какого-либо ответа. Теперь на ровно 3 подряд
    нулевых байта и на DL/T645-анонс отправляется эхо ``00 00 00`` —
    см. ``_echo_heartbeat()``."""

    # Сколько последних сырых байт держать в буфере диагностики (2026-09-10,
    # см. DECISIONS.md — по просьбе пользователя, вдохновлено находкой в
    # референсной C#-программе: для части моделей счётчиков штатный
    # DLMS-парсер получает байты, не укладывающиеся в строгий формат
    # ("Invalid data type."), и референс для таких моделей это терпит, а
    # не считает провалом). Наша фильтрация "в поиске кадра" отбрасывает
    # нераспознанные байты ДО попытки разбора — так что при отказе чтения
    # не было видно, было ли на самом деле что-то на проводе, что не
    # сложилось в валидный кадр, или действительно полная тишина.
    _MAX_CAPTURED_BYTES = 4096

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._seeking = True
        self._deadline: float | None = None
        self._captured = bytearray()

    def reset_seeking(self) -> None:
        """Включает фильтрацию заново перед следующим кадром — вызывается
        автоматически из ``transport.TcpTransport.reset_frame_seeking()``
        перед КАЖДЫМ кадром на всём протяжении сессии (не только перед
        SNRM — см. docstring класса, второй найденный баг)."""
        self._seeking = True

    def set_deadline(self, deadline: float | None) -> None:
        """Включает (``deadline`` — абсолютное unix-время) или выключает
        (``None``) режим единого дедлайна на всю последовательность
        чтений — см. docstring класса, третий найденный баг. Пока
        дедлайн включён, обычные вызовы ``settimeout()`` игнорируются:
        дедлайн главнее и не должен незаметно продлеваться извне."""
        self._deadline = deadline

    def get_deadline(self) -> float | None:
        """Текущий абсолютный дедлайн — используется вызывающим кодом,
        чтобы временно сузить его на время попыток AARQ с повтором
        (см. ``hdlc_dlms._send_aarq_and_await_aare``, 2026-09-10) и затем
        восстановить исходное значение для последующих GET."""
        return self._deadline

    def settimeout(self, timeout_s: float) -> None:
        if self._deadline is not None:
            return
        self._sock.settimeout(timeout_s)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()

    def _raw_recv(self, n: int) -> bytes:
        if self._deadline is not None:
            remaining = self._deadline - time.time()
            if remaining <= 0:
                raise socket.timeout("Дедлайн ожидания ответа счётчика истёк")
            self._sock.settimeout(remaining)
        chunk = self._sock.recv(n)
        if chunk:
            self._captured += chunk
            overflow = len(self._captured) - self._MAX_CAPTURED_BYTES
            if overflow > 0:
                del self._captured[:overflow]
        return chunk

    def captured_hex(self) -> str:
        """Все сырые байты, реально пришедшие по сокету (до фильтрации,
        независимо от того, сложились ли они в валидный кадр) — для
        диагностики отказов чтения, см. докстринг ``_MAX_CAPTURED_BYTES``."""
        return self._captured.hex()

    def clear_captured(self) -> None:
        self._captured.clear()

    def _raw_recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._raw_recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Соединение закрыто во время чтения опережающего кадра")
            buf += chunk
        return bytes(buf)

    def _discard_one_dlt645_frame(self) -> bytes:
        # Ведущий 0x68 уже потреблён вызывающим recv() до вызова этого
        # метода — здесь дочитываем оставшиеся addr6(6)+68(1)+control(1)
        # = 8 байт заголовка, затем байт длины, затем данные+CS+0x16.
        header_rest = self._raw_recv_exact(8)
        length = self._raw_recv_exact(1)[0]
        self._raw_recv_exact(length + 2)  # данные + CS + конечный 0x16
        return header_rest[0:6]

    def recv(self, bufsize: int) -> bytes:
        if not self._seeking:
            # Уже внутри кадра, начало которого нашли ранее — отдаём
            # байты как есть, без какой-либо фильтрации.
            return self._raw_recv(bufsize)
        consecutive_zero_bytes = 0
        while True:
            first = self._raw_recv(1)
            if not first:
                return b""
            if first == b"\x00":
                consecutive_zero_bytes += 1
                if consecutive_zero_bytes == 3:
                    # GPRS-heartbeat (2026-09-10, см. DECISIONS.md — найдено
                    # в декомпилированном SocketServer.cs::OnReceiveCompleted
                    # реально работавшей заводской программы ver2.zip): если
                    # по TCP пришло РОВНО 3 нулевых байта, она отвечает теми
                    # же 3 нулевыми байтами. Мы раньше эти байты молча
                    # съедали, никогда не отвечая — гипотеза в том, что
                    # модем ждёт этого эха как подтверждения живости канала
                    # НЕЗАВИСИМО от HDLC/DLMS, и не получив его, перестаёт
                    # доверять каналу ДО того, как реально закроет TCP —
                    # что выглядело бы точно как наша картина (TCP жив,
                    # AARQ доставлен, AARE не приходит).
                    self._echo_heartbeat()
                    consecutive_zero_bytes = 0
                continue
            consecutive_zero_bytes = 0
            if first == bytes([_DLT645_START]):
                self._discard_one_dlt645_frame()
                # Тот же референс отвечает 3 нулевыми байтами и на
                # DL/T645-анонс, не только на голый "00 00 00" — не просто
                # эхо анонса, а тот же фиксированный heartbeat-ответ.
                self._echo_heartbeat()
                continue
            self._seeking = False  # нашли начало кадра — дальше не фильтруем
            return first

    def _echo_heartbeat(self) -> None:
        try:
            self._sock.sendall(b"\x00\x00\x00")
            logger.info("GPRS-heartbeat: отправлено эхо 00 00 00 на %s", self._sock.getpeername())
        except OSError:
            pass  # соединение уже могло закрыться — не мешаем вызывающему коду увидеть это через recv()


@dataclass
class _PooledConnection:
    conn_no: int
    raw_sock: socket.socket
    peer: tuple
    accepted_at: float
    # 2026-09-11 — локальный порт, на который пришло это соединение (см.
    # extra_bind_ports выше): логируется, чтобы можно было сопоставить
    # отказы чтений с тем, как именно пользователь разбил трафик РЭСов
    # по портам, и проверить, зависит ли доля отказов новой партии от
    # нагрузки на конкретный порт.
    local_port: int
    serial: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)


class CallHomePool:
    """Слушает входящие call-home соединения, держит скользящее окно из
    ``window_size`` кандидатов, отдаёт held-соединения по запросу
    ``claim`` для конкретного серийного номера.
    """

    def __init__(
        self,
        *,
        bind_host: str = "0.0.0.0",
        bind_port: int,
        extra_bind_ports: list[int] | None = None,
        window_size: int = DEFAULT_WINDOW_SIZE,
        max_per_serial: int = DEFAULT_MAX_PER_SERIAL,
    ) -> None:
        # 2026-09-11 (по просьбе пользователя — разбить трафик РЭСов по
        # разным портам, отчасти как диагностика: помогает понять, растёт
        # ли доля отказов от общей нагрузки на пул/поток соединений, или
        # это независимо от того, сколько РЭСов на одном порту).
        # ``bind_port`` остаётся "основным" портом (для обратной
        # совместимости — HealthCheck/лог и т.п. репортят именно его),
        # ``extra_bind_ports`` — дополнительные, все слушаются НЕЗАВИСИМО,
        # но принимают соединения в ОДИН общий self._pool — опознание,
        # событийное чтение, лимиты на серийник/окно и вся остальная
        # логика не знают и не должны знать, с какого именно порта
        # пришло соединение.
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._extra_bind_ports = list(extra_bind_ports or [])
        self._window_size = window_size
        self._max_per_serial = max_per_serial
        self._lock = threading.Lock()
        self._pool: dict[int, _PooledConnection] = {}  # порядок вставки = порядок подключения
        self._next_conn_no = 0
        self._listeners: list[socket.socket] = []
        self._accept_threads: list[threading.Thread] = []
        self._stop = threading.Event()
        # Этап 6 (обнаружение новых счётчиков) — сколько раз/когда впервые
        # опознан каждый серийный номер, НЕЗАВИСИМО от скользящего окна
        # held-соединений (которое вытесняет старые записи после
        # window_size новых подключений). Backend периодически опрашивает
        # этот список (см. ListCallHomeSerials в grpc_server.py), чтобы
        # завести счётчик в справочник ещё до того, как оператор узнает
        # его серийный номер откуда-то ещё. Хранится только в памяти
        # процесса Gateway — источник истины после обнаружения переходит
        # в Backend (таблица meters), перезапуск Gateway не теряет уже
        # заведённые счётчики, только временно "забывает", что именно
        # он уже сообщал о них (Backend всё равно не заведёт дубликат —
        # ищет по serial_number).
        self._seen_serials: dict[str, float] = {}

    def start(self) -> None:
        primary = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        primary.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        primary.bind((self._bind_host, self._bind_port))
        self._bind_port = primary.getsockname()[1]  # если был передан 0 (случайный порт)
        self._listeners.append(primary)

        for port in self._extra_bind_ports:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self._bind_host, port))
            self._listeners.append(listener)

        for listener in self._listeners:
            listener.listen(self._window_size + 5)
            listener.settimeout(1.0)
            thread = threading.Thread(target=self._accept_loop, args=(listener,), daemon=True)
            thread.start()
            self._accept_threads.append(thread)

        logger.info(
            "Call-home пул запущен на %s:%s%s (окно=%d)",
            self._bind_host, self.bind_ports, "" if len(self.bind_ports) == 1 else " (несколько портов)",
            self._window_size,
        )

    @property
    def bind_port(self) -> int:
        return self._bind_port

    @property
    def bind_ports(self) -> list[int]:
        """Все порты, на которых слушает пул — основной первым."""
        if self._listeners:
            return [sock.getsockname()[1] for sock in self._listeners]
        return [self._bind_port, *self._extra_bind_ports]

    def stop(self) -> None:
        self._stop.set()
        for listener in self._listeners:
            listener.close()
        for thread in self._accept_threads:
            thread.join(timeout=3)
        with self._lock:
            for pc in self._pool.values():
                pc.cancelled.set()
                try:
                    pc.raw_sock.close()
                except OSError:
                    pass
            self._pool.clear()

    def _accept_loop(self, listener: socket.socket) -> None:
        local_port = listener.getsockname()[1]
        while not self._stop.is_set():
            try:
                raw_sock, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._admit(raw_sock, peer, local_port)

    def _admit(self, raw_sock: socket.socket, peer: tuple, local_port: int) -> None:
        with self._lock:
            self._next_conn_no += 1
            conn_no = self._next_conn_no
            evicted = None
            if len(self._pool) >= self._window_size:
                oldest_no = next(iter(self._pool))
                evicted = self._pool.pop(oldest_no)
            pc = _PooledConnection(
                conn_no=conn_no, raw_sock=raw_sock, peer=peer, accepted_at=time.time(), local_port=local_port,
            )
            self._pool[conn_no] = pc

        if evicted is not None:
            evicted.cancelled.set()
            try:
                evicted.raw_sock.close()
            except OSError:
                pass
            logger.info("Вытеснено соединение #%d (окно из %d заполнено)", evicted.conn_no, self._window_size)

        logger.info("Call-home: принято соединение #%d от %s на порт %d", conn_no, peer, local_port)
        threading.Thread(target=self._identify, args=(pc,), daemon=True).start()

    def _identify(self, pc: _PooledConnection) -> None:
        """Опознаёт счётчика по опережающему DL/T645-анонс-кадру, если
        он есть (счётчик может и не прислать его сразу — тогда серийный
        номер остаётся неизвестен, и claim() по нему это соединение не
        найдёт; такое соединение просто будет вытеснено по возрасту)."""
        pc.raw_sock.settimeout(DEFAULT_IDENTIFY_TIMEOUT_S)
        try:
            first = pc.raw_sock.recv(1)
            if first != bytes([_DLT645_START]):
                return
            header = first
            while len(header) < 10:
                more = pc.raw_sock.recv(10 - len(header))
                if not more:
                    return
                header += more
            length = header[9]
            rest = b""
            while len(rest) < length + 2:
                more = pc.raw_sock.recv(length + 2 - len(rest))
                if not more:
                    return
                rest += more
            addr6 = header[1:7]
            pc.serial = serial_from_dlt645_address(addr6)
            evicted = None
            with self._lock:
                self._seen_serials.setdefault(pc.serial, time.time())
                # Лимит на ОДИН счётчик (DEFAULT_MAX_PER_SERIAL) — серийник
                # известен только теперь, поэтому проверяется здесь, а не в
                # _admit(). Вытесняем самое старое ЧУЖОЕ (по этому же
                # серийнику) held-соединение, если лимит уже выбран без pc.
                same_serial = [
                    p for p in self._pool.values() if p.serial == pc.serial and p.conn_no != pc.conn_no
                ]
                if len(same_serial) >= self._max_per_serial:
                    oldest = same_serial[0]  # self._pool упорядочен по вставке
                    del self._pool[oldest.conn_no]
                    evicted = oldest
            if evicted is not None:
                evicted.cancelled.set()
                try:
                    evicted.raw_sock.close()
                except OSError:
                    pass
                logger.info(
                    "Вытеснено соединение #%d — у счётчика %s уже %d held-соединений (лимит на счётчик)",
                    evicted.conn_no, pc.serial, self._max_per_serial,
                )
            logger.info(
                "Call-home: соединение #%d (порт %d) опознано как счётчик %s",
                pc.conn_no, pc.local_port, pc.serial,
            )
        except (socket.timeout, OSError):
            return

        if pc.serial in CAPTURE_OBJECTS_DIAGNOSTIC_SERIALS:
            # 2026-09-12 — разовый диагностический эксперимент (см.
            # выше), заменяет собой обычную обработку ЭТОГО конкретного
            # соединения целиком: счётчик из allowlist перезванивает
            # очень часто (проверено живым трафиком), следующий дозвон
            # получит обычное событийное чтение как обычно.
            self._run_capture_objects_diagnostic(pc)
            return

        # Событийное чтение сразу при подключении (2026-09-09, см.
        # DECISIONS.md и план ticklish-popping-bear.md) — единственный
        # момент, когда есть реальный шанс успеть SNRM/AARQ/GET до
        # истечения "окна жизни" соединения (см. docstring модуля, п.3);
        # FIFO-очередь старого пути систематически проигрывает эту гонку
        # при заметном бэклоге. Любая ошибка здесь перехватывается
        # ВНУТРИ _maybe_trigger_immediate_read и не должна доходить сюда.
        self._maybe_trigger_immediate_read(pc)

    def claim(self, serial: str, *, max_age_s: float | None = None) -> _PooledConnection | None:
        """Забирает из пула САМОЕ СВЕЖЕЕ held-соединение для данного
        серийного номера (если есть) — оно больше не подлежит вытеснению
        по скользящему окну, ответственность за него переходит
        вызывающему. Предпочтение свежему, а не старейшему совпадению —
        у более старого соединения выше шанс, что его окно жизни (см.
        docstring модуля, п.3) уже истекло.

        ``max_age_s``, если задан, полностью ИСКЛЮЧАЕТ из рассмотрения
        соединения старше этого возраста — подтверждено на практике
        2026-08-18: соединение, которое пролежало в пуле опознанным, но
        невостребованным дольше примерно минуты, не отвечает ВООБЩЕ
        НИЧЕГО ни на один SNRM (проверено 20 попытками подряд), даже
        если формально ещё не закрыто со своей стороны. Лучше подождать
        следующее свежее соединение (см. вызывающий код в
        ``read_via_call_home``), чем тратить время на заведомо мёртвое.
        """
        with self._lock:
            matches = [pc for pc in self._pool.values() if pc.serial == serial]
            if max_age_s is not None:
                now = time.time()
                matches = [pc for pc in matches if now - pc.accepted_at <= max_age_s]
            if not matches:
                return None
            chosen = matches[-1]
            del self._pool[chosen.conn_no]
            return chosen

    _SIBLING_POLL_INTERVAL_S = 1.0

    def _wait_for_sibling_connection(
        self, serial: str, deadline: float
    ) -> "_PooledConnection | None":
        """2026-09-11 (по просьбе пользователя) — опрашивает пул (раз в
        ``_SIBLING_POLL_INTERVAL_S``) в ожидании СЛЕДУЮЩЕГО дозвона того
        же счётчика, пока не истечёт ``deadline`` — используется
        ``_maybe_trigger_immediate_read``, когда все уже испробованные
        held-соединения кончились (провалом или "залипшей" ассоциацией),
        а общий бюджет ожидания ещё позволяет попробовать ещё раз.
        ``None``, если дождаться не удалось за отведённое время."""
        while True:
            with self._lock:
                for p in self._pool.values():
                    if p.serial == serial:
                        del self._pool[p.conn_no]
                        return p
            remaining_budget = deadline - time.time()
            if remaining_budget <= 0:
                return None
            time.sleep(min(self._SIBLING_POLL_INTERVAL_S, remaining_budget))

    def _run_capture_objects_diagnostic(self, pc: _PooledConnection) -> None:
        """2026-09-12, по просьбе пользователя ("покопай почему data-
        access-error 250") — разовый диагностический эксперимент,
        ПОЛНОСТЬЮ изолированный от обычного событийного пути и job'ов:
        читает атрибут 3 (capture_objects) объекта профиля нагрузки
        обычным GET (как и capture_period), логирует результат и
        закрывает соединение. Управляется allowlist'ом
        ``CAPTURE_OBJECTS_DIAGNOSTIC_SERIALS`` (пуст по умолчанию, см.
        выше). Пароль берётся тем же способом, что и в
        ``_maybe_trigger_immediate_read`` (claim-jobs отдаёт его
        независимо от наличия due job'ов) — если у этого же счётчика
        случайно окажется настоящая due job'а, она будет молча забрана
        и НЕ обработана (вернётся в очередь через stale_job_reaper_loop
        на Backend'е по таймауту, как и при любом другом сетевом сбое) —
        приемлемо для точечного эксперимента на одном выбранном
        серийнике, не для постоянной работы."""
        try:
            from . import backend_client
            from .protocols import hdlc_dlms
            from .transport import TcpServerTransport

            claimed = backend_client.claim_due_jobs(pc.serial, peer_ip=pc.peer[0], local_port=pc.local_port)
            if claimed is None or not claimed.password:
                logger.warning(
                    "Диагностика capture_objects: не удалось получить пароль для %s (Backend недоступен?)",
                    pc.serial,
                )
                return
            password_bytes = claimed.password.encode("ascii")

            with self._lock:
                if self._pool.get(pc.conn_no) is not pc:
                    return
                del self._pool[pc.conn_no]

            hdlc_dlms.server_hdlc_address(hdlc_dlms.physical_address(pc.serial, hdlc_dlms.HDLC_DLMS))
            filtering_sock = DlT645FilteringSocket(pc.raw_sock)
            transport = None
            linked = False
            last_error: GatewayError | None = None
            try:
                for attempt in range(1, DEFAULT_MAX_ATTEMPTS_PER_CONNECTION + 1):
                    transport = TcpServerTransport.from_accepted_socket(
                        filtering_sock, peer_host=pc.peer[0], peer_port=pc.peer[1],
                        timeout_ms=DEFAULT_PER_ATTEMPT_TIMEOUT_MS,
                    )
                    try:
                        hdlc_dlms.establish_link(transport, serial=pc.serial)
                        linked = True
                        break
                    except GatewayError as exc:
                        last_error = exc
                        logger.info(
                            "Диагностика capture_objects: попытка %d SNRM на соединении #%d — %s, повтор",
                            attempt, pc.conn_no, exc.code,
                        )
                    except (ConnectionError, OSError) as exc:
                        logger.warning(
                            "Диагностика capture_objects: обрыв соединения #%d при SNRM (%s)",
                            pc.conn_no, exc,
                        )
                        return
                    time.sleep(DEFAULT_RETRY_INTERVAL_S)

                if not linked:
                    logger.warning(
                        "Диагностика capture_objects: счётчик %s не подтвердил SNRM за %d попыток (%s)",
                        pc.serial, DEFAULT_MAX_ATTEMPTS_PER_CONNECTION, last_error,
                    )
                    return

                filtering_sock.set_deadline(time.time() + DEFAULT_ASSOCIATION_TIMEOUT_MS / 1000)
                # OBIS "1.1.63.1.0.ff" — та же рабочая гипотеза адреса
                # буфера профиля нагрузки, что и в load_profile.
                # DEFAULT_LOAD_PROFILE_OBIS (backend), продублирована
                # здесь литералом — это разовый эксперимент, не общий код.
                try:
                    result = hdlc_dlms.read_profile_capture_objects_via_established_link(
                        transport, serial=pc.serial, password=password_bytes,
                        obis="1.1.63.1.0.ff", class_id=7,
                    )
                    logger.warning(
                        "Диагностика capture_objects (%s): УСПЕХ, атрибут 3 = %r", pc.serial, result,
                    )
                except Exception as exc:  # noqa: BLE001 — любой исход интересен для диагностики
                    logger.warning(
                        "Диагностика capture_objects (%s): не удалось прочитать атрибут 3 — %s: %s",
                        pc.serial, type(exc).__name__, exc,
                    )
            finally:
                try:
                    pc.raw_sock.close()
                except OSError:
                    pass
        except Exception:
            logger.exception(
                "Диагностика capture_objects: неожиданная ошибка при обработке соединения #%d", pc.conn_no,
            )

    def _maybe_trigger_immediate_read(self, pc: _PooledConnection) -> None:
        """Событийная попытка прочитать ВСЕ due job'ы счётчика сразу
        после опознания серийника на свежепринятом соединении
        (2026-09-09, см. DECISIONS.md и план ticklish-popping-bear.md)
        — выполняется в этом же фоновом потоке ``_identify`` (уже
        отдельный поток на соединение, не блокирует ``_accept_loop``).
        Backend недоступен/не отвечает/ничего не должен — трактуется
        одинаково: "ничего не делаю", ``pc`` ОСТАЁТСЯ в пуле нетронутым
        для старого пути (``claim()``/job_worker) — сетевой сбой или
        отсутствие due-задач не должны как-либо мешать существующему
        поведению. Любое исключение перехватывается широко (эта функция
        вызывается из середины ``_identify()`` и не должна случайно
        сорвать её собственную обработку ошибок)."""
        try:
            from . import backend_client

            claimed = backend_client.claim_due_jobs(pc.serial, peer_ip=pc.peer[0], local_port=pc.local_port)
            if claimed is None or not (claimed.jobs or claimed.load_profile_jobs):
                return

            with self._lock:
                if self._pool.get(pc.conn_no) is not pc:
                    # pc уже недоступен (вытеснен по лимиту чуть выше в
                    # этом же _identify либо claim()-нут старым путём в
                    # узком окне гонки) — отступаем, job'ы уже RUNNING
                    # на Backend вернутся в очередь по таймауту (см.
                    # stale_job_reaper_loop).
                    logger.info("Immediate-read: соединение #%d уже недоступно, пропуск", pc.conn_no)
                    return
                del self._pool[pc.conn_no]
                # Остальные held-соединения ЭТОГО ЖЕ счётчика больше не
                # нужны пулу (не оставлять их висеть до истечения окна/
                # лимита) — но, в отличие от прежней версии, НЕ выбрасываем
                # их сразу: пробуем как запасные попытки, если pc не
                # ответит (см. DEFAULT_IMMEDIATE_READ_MAX_WAIT_S выше).
                same_serial_others = [p for p in self._pool.values() if p.serial == pc.serial]
                for p in same_serial_others:
                    del self._pool[p.conn_no]

            # Кандидаты пробуются по очереди: сначала pc (только что
            # доказал живость свежим DL/T645-анонсом), затем остальные
            # held-соединения того же счётчика от свежего к старому —
            # тот же принцип предпочтения, что и в claim(). Несколько
            # разных физических дозвонов вместо одного (см. комментарий
            # у DEFAULT_IMMEDIATE_READ_MAX_WAIT_S).
            candidates = [pc] + sorted(same_serial_others, key=lambda p: p.accepted_at, reverse=True)
            if same_serial_others:
                logger.info(
                    "Immediate-read: у счётчика %s ещё %d held-соединений в резерве "
                    "как запасные попытки (счётчику стало активным #%d)",
                    pc.serial, len(same_serial_others), pc.conn_no,
                )

            password_bytes = claimed.password.encode("ascii")

            if claimed.jobs:
                obis_specs = [(job.obis, job.class_id) for job in claimed.jobs]
                deadline = time.time() + DEFAULT_IMMEDIATE_READ_MAX_WAIT_S
                outcomes: list = []
                remaining = list(candidates)
                while True:
                    if time.time() >= deadline:
                        logger.info(
                            "Immediate-read: общий бюджет ожидания (%.0fс) для счётчика %s исчерпан%s",
                            DEFAULT_IMMEDIATE_READ_MAX_WAIT_S, pc.serial,
                            f", {len(remaining)} соединений не пробовали" if remaining else "",
                        )
                        break
                    if not remaining:
                        # 2026-09-11 (по просьбе пользователя) — все уже
                        # случайно оказавшиеся в пуле held-соединения этого
                        # счётчика исчерпаны (либо провалом, либо "залипшей"
                        # ассоциацией, см. ниже), а бюджет ожидания ещё не
                        # истёк: ждём СЛЕДУЮЩИЙ дозвон этого же счётчика,
                        # вместо немедленной сдачи — раньше цикл реагировал
                        # только на то, что уже лежало в пуле на момент
                        # опознания, следующего дозвона не ждал вовсе.
                        next_pc = self._wait_for_sibling_connection(pc.serial, deadline)
                        if next_pc is None:
                            break
                        logger.info(
                            "Immediate-read: дождались следующего дозвона счётчика %s — соединение #%d",
                            pc.serial, next_pc.conn_no,
                        )
                        remaining.append(next_pc)
                        continue
                    candidate = remaining.pop(0)
                    try:
                        if pc.serial in VER2_EMULATION_TEST_SERIALS:
                            from .protocols import dlms as dlms_module

                            logger.info(
                                "Immediate-read: соединение #%d — эксперимент 'эмуляция ver2.zip' "
                                "для счётчика %s", candidate.conn_no, pc.serial,
                            )
                            outcomes = read_batch_via_fresh_connection(
                                candidate, serial=pc.serial, password=password_bytes, obis_specs=obis_specs,
                                aarq_user_information=dlms_module.USER_INFORMATION_INITIATE_VER2_VARIANT,
                                aare_per_attempt_timeout_s=20.0,
                                aare_max_attempts=3,
                                send_disc_before_retry=False,
                            )
                        else:
                            outcomes = read_batch_via_fresh_connection(
                                candidate, serial=pc.serial, password=password_bytes, obis_specs=obis_specs,
                            )
                        if outcomes and not any(o.ok for o in outcomes):
                            # 2026-09-11, см. DECISIONS.md ("покопайся в
                            # истории логов сервера") — эксперимент показал:
                            # ассоциация может успешно установиться (AARE
                            # получен), но ВСЕ последующие чтения (проверено
                            # на 8 разных OBIS = 16 GET в одной ассоциации)
                            # стабильно возвращают один и тот же мусорный
                            # ответ — "залипшая" ассоциация, не помогает ни
                            # разнообразие OBIS, ни отдельные invoke_id. Раньше
                            # такой результат принимался как окончательный
                            # (единственный успешный обмен без исключения —
                            # цикл сразу прерывался). Теперь пробуем СВЕЖУЮ
                            # ассоциацию вместо того, чтобы сдаваться на
                            # заведомо плохой — либо уже готовую запасную из
                            # пула, либо (см. ветку "not remaining" выше)
                            # дождавшись следующего дозвона.
                            logger.info(
                                "Immediate-read: соединение #%d — ассоциация установилась, но все %d "
                                "чтений вернулись с ошибкой (похоже на «залипшую» ассоциацию) — "
                                "пробуем следующую",
                                candidate.conn_no, len(outcomes),
                            )
                            continue
                        break
                    except GatewayError as exc:
                        logger.info(
                            "Immediate-read: соединение #%d — обмен не удался (%s)%s",
                            candidate.conn_no, exc.code,
                            f", пробуем следующее из {len(remaining)} оставшихся" if remaining else "",
                        )
                        outcomes = []
                    except (ConnectionError, OSError) as exc:
                        logger.info("Immediate-read: соединение #%d оборвалось (%s)", candidate.conn_no, exc)
                        outcomes = []
                    finally:
                        candidate.cancelled.set()
                        try:
                            candidate.raw_sock.close()
                        except OSError:
                            pass
                for candidate in remaining:
                    candidate.cancelled.set()
                    try:
                        candidate.raw_sock.close()
                    except OSError:
                        pass

                if not outcomes:
                    logger.info(
                        "Immediate-read: не удалось прочитать счётчик %s ни на одном из %d "
                        "испробованных соединений — job'ы вернутся в очередь по таймауту",
                        pc.serial, len(candidates) - len(remaining),
                    )

                if outcomes:
                    results = [
                        backend_client.JobResultReport(
                            job_id=job.job_id, obis=job.obis, ok=outcome.ok,
                            value=_json_safe_value(outcome.value),
                            error_code=outcome.error.code if outcome.error else None,
                            error_message=outcome.error.message if outcome.error else None,
                            is_partial=False,
                        )
                        for job, outcome in zip(claimed.jobs, outcomes)
                    ]
                    if not backend_client.report_job_results(pc.serial, results):
                        logger.warning(
                            "Immediate-read: не удалось отправить результаты в Backend для %s — "
                            "job'ы вернутся в очередь по таймауту",
                            pc.serial,
                        )
            else:
                # Нет обычных read_current/read_rated_current job'ов —
                # свежепринятые held-соединения того же дозвона (если
                # были) никому не понадобились, закрываем сразу же, не
                # оставляя висеть до истечения окна/лимита (тот же
                # принцип, что и в ветке с register-job'ами выше).
                for candidate in candidates:
                    candidate.cancelled.set()
                    try:
                        candidate.raw_sock.close()
                    except OSError:
                        pass

            if claimed.load_profile_jobs:
                self._run_load_profile_jobs(pc, claimed, password_bytes)
        except Exception:
            logger.exception("Immediate-read: неожиданная ошибка при обработке соединения #%d", pc.conn_no)

    def _run_load_profile_jobs(
        self,
        pc: "_PooledConnection",
        claimed: "backend_client.ClaimDueJobsResult",
        password_bytes: bytes,
    ) -> None:
        """2026-09-11 — перенос ``read_load_profile`` на событийный путь
        (см. DECISIONS.md, план ticklish-popping-bear.md). Вызывается из
        ``_maybe_trigger_immediate_read`` ПОСЛЕ обработки обычных
        read_current/read_rated_current job'ов того же дозвона (если
        были) — к этому моменту все ранее опробованные held-соединения
        уже закрыты (см. ``finally``/`candidate.raw_sock.close()` выше и
        ветку "нет register-job'ов"), поэтому почти всегда приходится
        ждать СЛЕДУЮЩИЙ дозвон этого же счётчика (``_wait_for_sibling_
        connection``) — так же, как ветка "not remaining" в основном
        цикле. Каждый load-profile job обрабатывается на ОТДЕЛЬНОМ
        свежем соединении — несколько job'ов подряд не пытаемся уместить
        в одну ассоциацию (в отличие от read_current-батча): чтение
        профиля — заметно более длительный обмен (датаблоки), и делить
        общий бюджет ожидания между несколькими такими попытками
        означало бы почти гарантированный провал всех."""
        from . import backend_client

        for job in claimed.load_profile_jobs:
            deadline = time.time() + DEFAULT_IMMEDIATE_READ_MAX_WAIT_S
            candidate = self._wait_for_sibling_connection(pc.serial, deadline)
            if candidate is None:
                logger.info(
                    "Immediate-read (профиль): не дождались нового соединения счётчика %s "
                    "для job #%d за %.0fс — job вернётся в очередь по таймауту",
                    pc.serial, job.job_id, DEFAULT_IMMEDIATE_READ_MAX_WAIT_S,
                )
                continue

            error: GatewayError | None
            try:
                from_dt = datetime.fromisoformat(job.from_iso)
                to_dt = datetime.fromisoformat(job.to_iso)
                rows, error = read_load_profile_via_fresh_connection(
                    candidate, serial=pc.serial, password=password_bytes,
                    obis=job.obis, class_id=job.class_id, from_dt=from_dt, to_dt=to_dt,
                )
            except GatewayError as exc:
                logger.info(
                    "Immediate-read (профиль): соединение #%d — обмен не удался (%s)",
                    candidate.conn_no, exc.code,
                )
                rows, error = [], exc
            except (ConnectionError, OSError) as exc:
                logger.info("Immediate-read (профиль): соединение #%d оборвалось (%s)", candidate.conn_no, exc)
                rows, error = [], GatewayError(f"Обрыв соединения #{candidate.conn_no}: {exc}")
            finally:
                candidate.cancelled.set()
                try:
                    candidate.raw_sock.close()
                except OSError:
                    pass

            report_rows = [
                backend_client.LoadProfileRowReport(
                    timestamp_iso=timestamp.isoformat(), values=_json_safe_value(values),
                )
                for timestamp, values in rows
            ]
            ok = error is None
            if not backend_client.report_load_profile_results(
                pc.serial, job_id=job.job_id, obis=job.obis, rows=report_rows, ok=ok,
                error_code=error.code if error else None,
                error_message=error.message if error else None,
                is_partial=(error is not None and len(report_rows) > 0),
            ):
                logger.warning(
                    "Immediate-read (профиль): не удалось отправить результаты в Backend для %s "
                    "(job #%d) — job вернётся в очередь по таймауту",
                    pc.serial, job.job_id,
                )

    def pending_count(self, serial: str | None = None) -> int:
        with self._lock:
            if serial is None:
                return len(self._pool)
            return sum(1 for pc in self._pool.values() if pc.serial == serial)

    def list_seen_serials(self) -> dict[str, float]:
        """Этап 6 — все серийные номера, опознанные этим Gateway с
        момента запуска процесса (первый момент опознания, unix-время),
        независимо от того, вытеснены ли уже их held-соединения из
        скользящего окна."""
        with self._lock:
            return dict(self._seen_serials)


def read_via_call_home(
    pool: CallHomePool,
    *,
    serial: str,
    password: bytes,
    obis: str,
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    # Согласовано с call_timeout_s по умолчанию в backend/app/services/
    # gateway_client.py — иначе Backend отваливается по своему таймауту
    # ДО того, как Gateway сдастся сам, и ошибка выглядит немым обрывом
    # соединения вместо понятного TIMEOUT/GATEWAY_ERROR.
    max_wait_s: float = 60.0,
    per_attempt_timeout_ms: int = DEFAULT_PER_ATTEMPT_TIMEOUT_MS,
    max_attempts_per_connection: int = DEFAULT_MAX_ATTEMPTS_PER_CONNECTION,
    max_claim_age_s: float = DEFAULT_MAX_CLAIM_AGE_S,
    association_timeout_ms: int = DEFAULT_ASSOCIATION_TIMEOUT_MS,
    mechanism_id: int = 1,
) -> object:
    """Пытается прочитать регистр через уже установленные (call-home)
    соединения для данного счётчика — перебирает held-соединения от
    самого свежего. На каждом соединении сначала повторяет SNRM
    (``max_attempts_per_connection`` раз с коротким ``per_attempt_timeout_ms``
    — счётчик действительно не отвечает на первый SNRM, нужны именно
    повторы). После того как SNRM/UA прошёл, AARQ+GET выполняются ОДИН
    раз с намного бОльшим ``association_timeout_ms`` и БЕЗ повторной
    отправки SNRM — см. ``DEFAULT_ASSOCIATION_TIMEOUT_MS``: доставка
    AARQ подтверждена TCP-ACK, дальнейшие повторы SNRM тут не помогут,
    нужно просто терпеливее ждать. Соединения старше ``max_claim_age_s``
    не забираются вовсе (см. docstring ``CallHomePool.claim``).
    """
    # Импорт здесь, а не на верхнем уровне модуля — chain
    # callhome -> protocols.hdlc_dlms -> transport не нужен для тех, кто
    # использует только сам пул (например, тесты адресации).
    from .protocols import hdlc_dlms
    from .transport import TcpServerTransport

    # Валидация адреса ДО цикла ожидания — 2026-09-08, найдено на 22
    # реальных счётчиках (см. errors.AddressingError, DECISIONS.md):
    # правило «физический адрес = последние 5 цифр серийного» может
    # давать значение вне 14-битного поля, и это ВСЕГДА одинаково
    # проваливается для данного serial, сеть тут ни при чём. Без этой
    # проверки джоб держал бы held-соединение и воркер занятыми весь
    # max_wait_s (до 150с) на заведомо обречённую попытку.
    hdlc_dlms.server_hdlc_address(hdlc_dlms.physical_address(serial, hdlc_dlms.HDLC_DLMS))

    deadline = time.time() + max_wait_s
    last_error: GatewayError | None = None
    tried_any = False

    while time.time() < deadline:
        pc = pool.claim(serial, max_age_s=max_claim_age_s)
        if pc is None:
            time.sleep(0.5)
            continue
        tried_any = True
        filtering_sock = DlT645FilteringSocket(pc.raw_sock)
        linked = False
        for attempt in range(1, max_attempts_per_connection + 1):
            if time.time() >= deadline:
                break
            transport = TcpServerTransport.from_accepted_socket(
                filtering_sock, peer_host=pc.peer[0], peer_port=pc.peer[1], timeout_ms=per_attempt_timeout_ms
            )
            try:
                hdlc_dlms.establish_link(transport, serial=serial)
                linked = True
                break
            except GatewayError as exc:
                last_error = exc
                logger.info(
                    "Call-home: попытка %d SNRM на соединении #%d — %s, повтор",
                    attempt, pc.conn_no, exc.code,
                )
            except (ConnectionError, OSError) as exc:
                last_error = GatewayError(f"Обрыв соединения #{pc.conn_no}: {exc}")
                break  # это соединение мертво, пробуем следующее held-соединение (если появится)
            time.sleep(retry_interval_s)

        if linked:
            # SNRM/UA прошёл — доставку AARQ дальше подтверждает сам TCP
            # (ACK), так что смысла пересылать SNRM снова нет. Просто
            # терпеливо ждём AARE/GET-response на том же transport, без
            # разрыва сессии. Абсолютный дедлайн (не settimeout()) — см.
            # DlT645FilteringSocket.set_deadline и DECISIONS.md,
            # «эксперимент с ожиданием AARE 150с» (2026-09-08): без
            # этого случайный шумовой байт (например, keepalive) молча
            # продлевал бы реальное ожидание намного дальше
            # association_timeout_ms. Дедлайн покрывает AARQ->AARE и
            # последующий GET->GET-response этой же попытки целиком.
            filtering_sock.set_deadline(time.time() + association_timeout_ms / 1000)
            try:
                value = hdlc_dlms.read_register_via_established_link(
                    transport, serial=serial, password=password, obis=obis, mechanism_id=mechanism_id
                )
                logger.info("Call-home: чтение %s удалось на соединении #%d", serial, pc.conn_no)
                return value
            except GatewayError as exc:
                last_error = exc
                if exc.code == "AUTH_FAILED":
                    raise  # не связано с проблемой соединения — повторять бессмысленно
                logger.info(
                    "Call-home: AARQ/GET на соединении #%d подтверждён TCP, но AARE/GET-response "
                    "не пришёл за %.0fс (%s) — переходим к следующему соединению",
                    pc.conn_no, association_timeout_ms / 1000, exc.code,
                )
            except (ConnectionError, OSError) as exc:
                last_error = GatewayError(f"Обрыв соединения #{pc.conn_no}: {exc}")

        try:
            pc.raw_sock.close()
        except OSError:
            pass

    if not tried_any:
        raise NoConnectionEstablishedError(
            f"Счётчик {serial} ещё не установил ни одного call-home соединения с Gateway"
        )
    if last_error is not None:
        raise last_error
    raise GatewayError(f"Не удалось прочитать регистр со счётчика {serial} за {max_wait_s}с")


def _json_safe_value(value: object) -> object:
    """Приводит декодированное DLMS-значение к JSON-совместимому виду
    перед отправкой в Backend (2026-09-10, см. DECISIONS.md — найденный
    на живом трафике краш: счётчик вернул пустую octet-string, `datatypes.
    decode_value` честно вернул ``bytes``, а ``json.dumps`` в
    ``backend_client._post_json`` падал с ``TypeError: Object of type
    bytes is not JSON serializable`` — это валило отправку РЕЗУЛЬТАТОВ
    ЦЕЛОГО батча (включая уже успешно прочитанные соседние OBIS той же
    ассоциации), не только этот один объект). ``bytes`` — в hex-строку
    (тот же принцип, что и у ``captured_hex()``); списки/кортежи (Array/
    Structure) — рекурсивно, поэлементно."""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _log_captured_on_failure(filtering_sock: "DlT645FilteringSocket", conn_no: int, stage: str) -> None:
    """Диагностика (2026-09-10, см. DECISIONS.md — находка в референсной
    C#-программе: штатный DLMS-парсер для части моделей счётчиков может
    получить байты, не укладывающиеся в строгий формат ("Invalid data
    type."), и референс для таких моделей это терпит, а не считает
    провалом). Наша фильтрация "в поиске кадра" отбрасывает нераспознанные
    байты ДО попытки разбора — логируем всё, что реально пришло по
    сокету на этом этапе, чтобы увидеть, было ли на проводе что-то, что
    не сложилось в валидный кадр, или действительно полная тишина."""
    raw_hex = filtering_sock.captured_hex()
    if raw_hex:
        logger.info(
            "Immediate-read: соединение #%d — на этапе %s получено %d сырых байт, "
            "не сложившихся в успешный обмен: %s",
            conn_no, stage, len(raw_hex) // 2, raw_hex,
        )
    else:
        logger.info(
            "Immediate-read: соединение #%d — на этапе %s не пришло ВООБЩЕ НИ БАЙТА",
            conn_no, stage,
        )


def read_batch_via_fresh_connection(
    pc: "_PooledConnection",
    *,
    serial: str,
    password: bytes,
    obis_specs: list[tuple[str, int]],
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    per_attempt_timeout_ms: int = DEFAULT_PER_ATTEMPT_TIMEOUT_MS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS_PER_CONNECTION,
    association_timeout_ms: int = DEFAULT_ASSOCIATION_TIMEOUT_MS,
    aarq_user_information: bytes | None = None,
    aare_per_attempt_timeout_s: float | None = None,
    aare_max_attempts: int | None = None,
    send_disc_before_retry: bool = True,
) -> list:
    """Событийный путь (2026-09-09, см. DECISIONS.md и план
    ticklish-popping-bear.md; вызывается из ``CallHomePool.
    _maybe_trigger_immediate_read`` сразу после опознания серийника на
    СВЕЖЕПРИНЯТОМ соединении) — в отличие от ``read_via_call_home``, НЕ
    перебирает несколько held-соединений (их тут просто нет, кроме
    ``pc`` самого): достаточно один раз повторить SNRM (счётчик обычно
    не отвечает на самый первый) и один раз терпеливо дождаться AARE
    после AARQ — та же логика ожидания (абсолютный дедлайн, не
    ``settimeout()``, см. ``DlT645FilteringSocket.set_deadline`` и
    DECISIONS.md, «эксперимент с ожиданием AARE 150с»), просто без
    внешнего цикла по held-соединениям, которого здесь нет смысла
    заводить — соединение только что принято, других кандидатов на
    этот же серийник специально не остаётся (см. вызывающий код,
    закрывает соседние held-соединения того же счётчика).

    ``aarq_user_information``/``aare_per_attempt_timeout_s``/
    ``aare_max_attempts``/``send_disc_before_retry`` — переопределения
    для эксперимента "эмуляция ver2.zip" (2026-09-10, см. DECISIONS.md
    и ``VER2_EMULATION_TEST_SERIALS`` ниже); ``None``/дефолт — обычное
    боевое поведение, не меняется."""
    from .protocols import hdlc_dlms
    from .transport import TcpServerTransport

    # Валидация адреса ДО сокета — тот же принцип, что и в
    # read_via_call_home (см. её комментарий про AddressingError).
    hdlc_dlms.server_hdlc_address(hdlc_dlms.physical_address(serial, hdlc_dlms.HDLC_DLMS))

    filtering_sock = DlT645FilteringSocket(pc.raw_sock)
    linked = False
    transport = None
    last_error: GatewayError | None = None
    for attempt in range(1, max_attempts + 1):
        transport = TcpServerTransport.from_accepted_socket(
            filtering_sock, peer_host=pc.peer[0], peer_port=pc.peer[1], timeout_ms=per_attempt_timeout_ms
        )
        try:
            hdlc_dlms.establish_link(transport, serial=serial)
            linked = True
            break
        except GatewayError as exc:
            last_error = exc
            logger.info(
                "Immediate-read: попытка %d SNRM на соединении #%d — %s, повтор",
                attempt, pc.conn_no, exc.code,
            )
        except (ConnectionError, OSError) as exc:
            raise GatewayError(f"Обрыв соединения #{pc.conn_no}: {exc}") from exc
        time.sleep(retry_interval_s)

    if not linked:
        _log_captured_on_failure(filtering_sock, pc.conn_no, "SNRM")
        raise last_error or GatewayError(
            f"Immediate-read: счётчик {serial} не подтвердил SNRM на свежем соединении "
            f"за {max_attempts} попыток"
        )

    filtering_sock.set_deadline(time.time() + association_timeout_ms / 1000)
    filtering_sock.clear_captured()  # SNRM/UA уже прошли — интересны только байты ПОСЛЕ этого
    kwargs = {}
    if aarq_user_information is not None:
        kwargs["aarq_user_information"] = aarq_user_information
    if aare_per_attempt_timeout_s is not None:
        kwargs["aare_per_attempt_timeout_s"] = aare_per_attempt_timeout_s
    if aare_max_attempts is not None:
        kwargs["aare_max_attempts"] = aare_max_attempts
    kwargs["send_disc_before_retry"] = send_disc_before_retry
    try:
        return hdlc_dlms.read_registers_via_established_link(
            transport, serial=serial, password=password, obis_specs=obis_specs, **kwargs
        )
    except Exception:
        _log_captured_on_failure(filtering_sock, pc.conn_no, "AARQ/AARE/GET")
        raise


def read_load_profile_via_fresh_connection(
    pc: "_PooledConnection",
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int,
    from_dt,
    to_dt,
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    per_attempt_timeout_ms: int = DEFAULT_PER_ATTEMPT_TIMEOUT_MS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS_PER_CONNECTION,
    association_timeout_ms: int = DEFAULT_ASSOCIATION_TIMEOUT_MS,
) -> tuple[list[tuple[object, object]], "GatewayError | None"]:
    """Профиль нагрузки через событийный путь (2026-09-11, см.
    DECISIONS.md — перенос read_load_profile на immediate-read; живой
    эксперимент подтвердил, что ассоциация на свежем соединении держится
    и после AARE, не только до него). Аналог ``read_batch_via_fresh_
    connection`` (тот же SNRM-повтор + терпеливое ожидание AARE через
    единый дедлайн), но вызывает ``hdlc_dlms.read_load_profile_via_
    established_link`` вместо чтения регистров — генератор датаблоков,
    поэтому результат собирается в список, а не возвращается как есть:
    обрыв связи посреди передачи НЕ должен терять уже собранные строки
    (см. вызывающий код и ``backend_client.report_load_profile_results``
    — отчёт с частичным результатом, а не пустая рука).

    Возвращает ``(строки, ошибка)`` — ``ошибка is None`` при чистом
    завершении (в т.ч. если сам диапазон дат пуст — строк тогда нет, но
    это не отказ); при обрыве/отказе строки, собранные ДО этого момента,
    всё равно возвращаются вместе с ошибкой."""
    from .protocols import hdlc_dlms
    from .transport import TcpServerTransport

    hdlc_dlms.server_hdlc_address(hdlc_dlms.physical_address(serial, hdlc_dlms.HDLC_DLMS))

    filtering_sock = DlT645FilteringSocket(pc.raw_sock)
    linked = False
    transport = None
    last_error: GatewayError | None = None
    for attempt in range(1, max_attempts + 1):
        transport = TcpServerTransport.from_accepted_socket(
            filtering_sock, peer_host=pc.peer[0], peer_port=pc.peer[1], timeout_ms=per_attempt_timeout_ms
        )
        try:
            hdlc_dlms.establish_link(transport, serial=serial)
            linked = True
            break
        except GatewayError as exc:
            last_error = exc
            logger.info(
                "Immediate-read (профиль): попытка %d SNRM на соединении #%d — %s, повтор",
                attempt, pc.conn_no, exc.code,
            )
        except (ConnectionError, OSError) as exc:
            raise GatewayError(f"Обрыв соединения #{pc.conn_no}: {exc}") from exc
        time.sleep(retry_interval_s)

    if not linked:
        _log_captured_on_failure(filtering_sock, pc.conn_no, "SNRM (профиль)")
        raise last_error or GatewayError(
            f"Immediate-read (профиль): счётчик {serial} не подтвердил SNRM на свежем соединении "
            f"за {max_attempts} попыток"
        )

    from .protocols.datatypes import DlmsDataError

    filtering_sock.set_deadline(time.time() + association_timeout_ms / 1000)
    filtering_sock.clear_captured()
    rows: list[tuple[object, object]] = []
    try:
        for row in hdlc_dlms.read_load_profile_via_established_link(
            transport, serial=serial, password=password, obis=obis, class_id=class_id,
            from_dt=from_dt, to_dt=to_dt,
        ):
            rows.append(row)
    except GatewayError as exc:
        if exc.code == "AUTH_FAILED":
            raise
        _log_captured_on_failure(filtering_sock, pc.conn_no, "AARQ/AARE/GET (профиль)")
        return rows, exc
    except DlmsDataError as exc:
        # 2026-09-10 нашли этот же класс бага для обычных регистров (см.
        # DECISIONS.md) — DlmsDataError НЕ подкласс GatewayError, поэтому
        # не ловится строкой выше; счётчик ответил, просто некорректными
        # данными (напр. на GET capture_period) — это отказ ЭТОЙ попытки,
        # а не необработанное исключение, роняющее весь поток identify().
        _log_captured_on_failure(filtering_sock, pc.conn_no, "AARQ/AARE/GET (профиль)")
        return rows, GatewayError(f"Некорректные данные в ответе счётчика: {exc}")
    except (ConnectionError, OSError) as exc:
        return rows, GatewayError(f"Обрыв соединения #{pc.conn_no}: {exc}")
    return rows, None


def read_load_profile_via_call_home(
    pool: CallHomePool,
    *,
    serial: str,
    password: bytes,
    obis: str,
    class_id: int,
    from_dt,
    to_dt,
    retry_interval_s: float = DEFAULT_RETRY_INTERVAL_S,
    max_wait_s: float = 150.0,
    per_attempt_timeout_ms: int = DEFAULT_PER_ATTEMPT_TIMEOUT_MS,
    max_attempts_per_connection: int = DEFAULT_MAX_ATTEMPTS_PER_CONNECTION,
    max_claim_age_s: float = DEFAULT_MAX_CLAIM_AGE_S,
    association_timeout_ms: int = DEFAULT_ASSOCIATION_TIMEOUT_MS,
):
    """Профиль нагрузки через call-home (2026-08-19) — тот же приём
    перебора held-соединений, что и ``read_via_call_home`` (см. её
    docstring про повторы SNRM и терпеливое ожидание AARE/GET-response),
    но вместо одного GET на найденном соединении выполняется ВЕСЬ обмен
    чтения буфера целиком (``hdlc_dlms.read_load_profile_via_established_link``
    — GET capture_period, затем GET с диапазоном, возможно несколько
    датаблоков). Если соединение обрывается посреди буфера, уже
    отданные вызывающему коду строки не теряются (генератор), но сам
    обмен НЕ возобновляется с места обрыва — начинается заново на
    следующем held-соединении (протокол не поддерживает докачку внутри
    одной сессии буфера); повторно отданные строки безвредны — Backend
    дедуплицирует по (meter_id, obis, timestamp), см. job_worker.py."""
    from .protocols import hdlc_dlms
    from .transport import TcpServerTransport

    # Валидация адреса ДО цикла ожидания — см. тот же комментарий в
    # read_via_call_home и errors.AddressingError / DECISIONS.md
    # (2026-09-08, найдено на 22 реальных счётчиках).
    hdlc_dlms.server_hdlc_address(hdlc_dlms.physical_address(serial, hdlc_dlms.HDLC_DLMS))

    deadline = time.time() + max_wait_s
    last_error: GatewayError | None = None
    tried_any = False

    while time.time() < deadline:
        pc = pool.claim(serial, max_age_s=max_claim_age_s)
        if pc is None:
            time.sleep(0.5)
            continue
        tried_any = True
        filtering_sock = DlT645FilteringSocket(pc.raw_sock)
        linked = False
        for attempt in range(1, max_attempts_per_connection + 1):
            if time.time() >= deadline:
                break
            transport = TcpServerTransport.from_accepted_socket(
                filtering_sock, peer_host=pc.peer[0], peer_port=pc.peer[1], timeout_ms=per_attempt_timeout_ms
            )
            try:
                hdlc_dlms.establish_link(transport, serial=serial)
                linked = True
                break
            except GatewayError as exc:
                last_error = exc
                logger.info(
                    "Call-home (профиль нагрузки): попытка %d SNRM на соединении #%d — %s, повтор",
                    attempt, pc.conn_no, exc.code,
                )
            except (ConnectionError, OSError) as exc:
                last_error = GatewayError(f"Обрыв соединения #{pc.conn_no}: {exc}")
                break
            time.sleep(retry_interval_s)

        if linked:
            # 2026-09-11 (найдено при разборе, почему старый путь не получает
            # AARE в отличие от read_via_call_home/read_batch_via_fresh_
            # connection) — раньше здесь вызывался settimeout(), а не
            # set_deadline(). DlT645FilteringSocket.settimeout() — no-op,
            # если единый дедлайн уже включён (см. её docstring), а если ещё
            # НЕ включён (как здесь), просто ставит таймаут на сырой сокет,
            # который ничего не знает про накопленное время ожидания — тот
            # же самый баг №3, что был найден и исправлён 2026-09-08 именно
            # в read_via_call_home (см. её докстринг и DECISIONS.md,
            # «эксперимент с ожиданием AARE 150с»), но так и не перенесён
            # сюда, в её сестринскую функцию для профиля нагрузки. Кроме
            # того, после успешного AARQ/AARE _send_aarq_and_await_aare
            # восстанавливает "исходный" дедлайн — а он был None (settimeout
            # его не трогает), так что все ПОСЛЕДУЮЩИЕ GET (capture_period,
            # диапазон, датаблоки) остались бы вовсе без общего дедлайна.
            filtering_sock.set_deadline(time.time() + association_timeout_ms / 1000)
            try:
                yield from hdlc_dlms.read_load_profile_via_established_link(
                    transport, serial=serial, password=password, obis=obis,
                    class_id=class_id, from_dt=from_dt, to_dt=to_dt,
                )
                logger.info(
                    "Call-home: профиль нагрузки %s прочитан на соединении #%d", serial, pc.conn_no
                )
                return
            except GatewayError as exc:
                last_error = exc
                if exc.code == "AUTH_FAILED":
                    raise
                logger.info(
                    "Call-home (профиль нагрузки): обмен на соединении #%d не завершился (%s) — "
                    "переходим к следующему соединению",
                    pc.conn_no, exc.code,
                )
            except (ConnectionError, OSError) as exc:
                last_error = GatewayError(f"Обрыв соединения #{pc.conn_no}: {exc}")

        try:
            pc.raw_sock.close()
        except OSError:
            pass

    if not tried_any:
        raise NoConnectionEstablishedError(
            f"Счётчик {serial} ещё не установил ни одного call-home соединения с Gateway"
        )
    if last_error is not None:
        raise last_error
    raise GatewayError(f"Не удалось прочитать профиль нагрузки со счётчика {serial} за {max_wait_s}с")
