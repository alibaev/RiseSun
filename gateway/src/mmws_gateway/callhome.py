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
3. Счётчик перестаёт открывать новые соединения, как только на ОДНОМ
   из держащихся начинается настоящий обмен (Gateway получает
   осмысленный ответ на SNRM). Для этого попытки SNRM повторяются на
   каждом держащемся соединении с интервалом в несколько секунд — один
   единственный SNRM сразу после подключения ответа не получает
   (подтверждено многократно), только повторные попытки.

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

DEFAULT_WINDOW_SIZE = 10
DEFAULT_RETRY_INTERVAL_S = 4.0
DEFAULT_IDENTIFY_TIMEOUT_S = 5.0

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
    байты этого кадра отдаются без какой-либо фильтрации — режим
    "поиска" нужно явно включить заново перед следующей попыткой через
    ``reset_seeking()``.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._seeking = True

    def reset_seeking(self) -> None:
        """Включает фильтрацию заново перед следующей попыткой чтения
        кадра — вызывать перед КАЖДОЙ новой попыткой SNRM, не только
        один раз при подключении (см. docstring класса)."""
        self._seeking = True

    def settimeout(self, timeout_s: float) -> None:
        self._sock.settimeout(timeout_s)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def close(self) -> None:
        self._sock.close()

    def _raw_recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
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
            return self._sock.recv(bufsize)
        while True:
            first = self._sock.recv(1)
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
    ``window_size`` кандидатов, отдаёт уже установленные (но ещё не
    прошедшие SNRM) соединения по запросу ``claim`` для конкретного
    серийного номера.
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
            logger.info("Call-home: соединение #%d опознано как счётчик %s", pc.conn_no, pc.serial)
        except (socket.timeout, OSError):
            return

    def claim(self, serial: str) -> _PooledConnection | None:
        """Забирает из пула одно held-соединение для данного серийного
        номера (если есть) — оно больше не подлежит вытеснению по
        скользящему окну, ответственность за него переходит вызывающему."""
        with self._lock:
            for conn_no, pc in list(self._pool.items()):
                if pc.serial == serial:
                    del self._pool[conn_no]
                    return pc
        return None

    def pending_count(self, serial: str | None = None) -> int:
        with self._lock:
            if serial is None:
                return len(self._pool)
            return sum(1 for pc in self._pool.values() if pc.serial == serial)


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
    per_attempt_timeout_ms: int = 3000,
) -> object:
    """Пытается прочитать регистр через уже установленные (call-home)
    соединения для данного счётчика — перебирает все, что есть в пуле,
    на каждом повторяя SNRM+полный обмен, пока один не даст успех либо
    не истечёт ``max_wait_s`` (нужны повторные попытки: единственный
    SNRM сразу после подключения ответа не даёт, см. docstring модуля).
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
        pc = pool.claim(serial)
        if pc is None:
            time.sleep(0.5)
            continue
        tried_any = True
        filtering_sock = DlT645FilteringSocket(pc.raw_sock)
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            filtering_sock.reset_seeking()
            transport = TcpServerTransport.from_accepted_socket(
                filtering_sock, peer_host=pc.peer[0], peer_port=pc.peer[1], timeout_ms=per_attempt_timeout_ms
            )
            try:
                value = hdlc_dlms.read_register(transport, serial=serial, password=password, obis=obis)
                logger.info(
                    "Call-home: чтение %s удалось на соединении #%d, попытка %d",
                    serial, pc.conn_no, attempt,
                )
                return value
            except GatewayError as exc:
                last_error = exc
                if exc.code == "AUTH_FAILED":
                    raise  # не связано с проблемой соединения — повторять бессмысленно
            except (ConnectionError, OSError):
                break  # это соединение мертво, пробуем следующее held-соединение (если появится)
            time.sleep(retry_interval_s)
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
