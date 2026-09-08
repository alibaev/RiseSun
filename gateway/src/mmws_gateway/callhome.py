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

from .errors import GatewayError

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
# вытеснения) соединениях. Увеличено с запасом на рост парка.
DEFAULT_WINDOW_SIZE = 300
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
    так что накопленное время не может незаметно продлеваться шумом."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._seeking = True
        self._deadline: float | None = None

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
        return self._sock.recv(n)

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
        while True:
            first = self._raw_recv(1)
            if not first:
                return b""
            if first == b"\x00":
                continue  # одиночный keepalive-байт на границе кадров, не начало кадра
            if first == bytes([_DLT645_START]):
                self._discard_one_dlt645_frame()
                continue
            self._seeking = False  # нашли начало кадра — дальше не фильтруем
            return first


@dataclass
class _PooledConnection:
    conn_no: int
    raw_sock: socket.socket
    peer: tuple
    accepted_at: float
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
        window_size: int = DEFAULT_WINDOW_SIZE,
    ) -> None:
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._window_size = window_size
        self._lock = threading.Lock()
        self._pool: dict[int, _PooledConnection] = {}  # порядок вставки = порядок подключения
        self._next_conn_no = 0
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
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
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self._bind_host, self._bind_port))
        self._bind_port = self._listener.getsockname()[1]  # если был передан 0 (случайный порт)
        self._listener.listen(self._window_size + 5)
        self._listener.settimeout(1.0)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        logger.info(
            "Call-home пул запущен на %s:%s (окно=%d)",
            self._bind_host, self._bind_port, self._window_size,
        )

    @property
    def bind_port(self) -> int:
        return self._bind_port

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=3)
        with self._lock:
            for pc in self._pool.values():
                pc.cancelled.set()
                try:
                    pc.raw_sock.close()
                except OSError:
                    pass
            self._pool.clear()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                raw_sock, peer = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._admit(raw_sock, peer)

    def _admit(self, raw_sock: socket.socket, peer: tuple) -> None:
        with self._lock:
            self._next_conn_no += 1
            conn_no = self._next_conn_no
            evicted = None
            if len(self._pool) >= self._window_size:
                oldest_no = next(iter(self._pool))
                evicted = self._pool.pop(oldest_no)
            pc = _PooledConnection(conn_no=conn_no, raw_sock=raw_sock, peer=peer, accepted_at=time.time())
            self._pool[conn_no] = pc

        if evicted is not None:
            evicted.cancelled.set()
            try:
                evicted.raw_sock.close()
            except OSError:
                pass
            logger.info("Вытеснено соединение #%d (окно из %d заполнено)", evicted.conn_no, self._window_size)

        logger.info("Call-home: принято соединение #%d от %s", conn_no, peer)
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
            with self._lock:
                self._seen_serials.setdefault(pc.serial, time.time())
            logger.info("Call-home: соединение #%d опознано как счётчик %s", pc.conn_no, pc.serial)
        except (socket.timeout, OSError):
            return

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
                    transport, serial=serial, password=password, obis=obis
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
        raise GatewayError(
            f"Счётчик {serial} ещё не установил ни одного call-home соединения с Gateway"
        )
    if last_error is not None:
        raise last_error
    raise GatewayError(f"Не удалось прочитать регистр со счётчика {serial} за {max_wait_s}с")


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
            filtering_sock.settimeout(association_timeout_ms / 1000)
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
        raise GatewayError(
            f"Счётчик {serial} ещё не установил ни одного call-home соединения с Gateway"
        )
    if last_error is not None:
        raise last_error
    raise GatewayError(f"Не удалось прочитать профиль нагрузки со счётчика {serial} за {max_wait_s}с")
