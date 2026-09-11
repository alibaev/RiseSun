#!/usr/bin/env python3
"""Диагностика гипотезы «счётчик отвечает AES-зашифрованным APDU» (2026-09-10).

Контекст (см. DECISIONS.md, запись «AES-шифрование APDU во второй
референсной программе»): в легаси-инструменте ``IECMeterManage.exe``
(``ver2.zip``), которым, по словам пользователя, корректно и без
проблем настраивали и обслуживали наш парк счётчиков, обнаружен
рабочий слой AES-128-ECB/PKCS7 поверх всего информационного поля
HDLC-кадра (после заголовка адресов/control, до контрольной суммы).
Найденный в архиве настоящий (боевой — содержит реальный IP нашего
GPRS-сервера) ``Config.ini`` в сочетании с логикой ``login.cs``
(персистентный выбор типа счётчика "3P Smart Meter" при входе
принудительно включает AES независимо от секции ``[AES]``) указывает,
что шифрование, вероятно, было АКТИВНО хотя бы для части парка, ключом
``0ABC12DEF3456789``. Наш Gateway никогда не шифровал/расшифровывал
APDU — если гипотеза верна, ответ такого счётчика выглядит для нас как
случайный мусор (не собирается в валидный DLMS/ACSE).

Эта утилита НЕ встроена в боевой путь чтения — чистая диагностика.
Берёт сырые байты (как их логирует ``callhome.py::_log_captured_on_failure``
— непрерывная hex-строка без пробелов) и пытается:

1. Найти в них валидные HDLC-кадры (``0x7E ... 0x7E``, тот же формат,
   что и наш собственный ``protocols.hdlc.HdlcFrame``).
2. Информационное поле каждого найденного кадра расшифровать AES-128
   в режиме ECB с PKCS7-паддингом, ключом ``0ABC12DEF3456789``
   (список кандидатов-ключей расширяем — см. ``CANDIDATE_KEYS``).
3. Проверить, не начинается ли результат с LLC-заголовка ответа
   (``E6 E7 00``) и тега AARE (``0x61``) или GET.response (``0xC4``)
   — если да, гипотеза подтверждена этим конкретным захватом.

Реализация AES-128 — самостоятельная, на чистом Python, БЕЗ внешних
зависимостей (в контейнере gateway установлены только grpcio/protobuf,
пакета ``cryptography`` там нет, а тянуть его в прод-образ ради разовой
диагностики нежелательно). Корректность самой реализации проверяется
при каждом запуске по официальному тестовому вектору FIPS-197
(``_selftest_fips197``) — если он не проходит, скрипт останавливается
до попытки разбора реальных данных.

Запуск:
    python3 gateway/scripts/aes_decrypt_diagnostic.py <hex-строка>
    echo "<hex-строка>" | python3 gateway/scripts/aes_decrypt_diagnostic.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mmws_gateway.protocols.dlms import (  # noqa: E402
    AARE_TAG,
    GET_RESPONSE_TAG,
    LLC_RESPONSE_HEADER,
)
from mmws_gateway.protocols.hdlc import FLAG, HdlcFrame  # noqa: E402
from mmws_gateway.errors import GatewayError  # noqa: E402

# Ключ, восстановленный из боевого Config.ini легаси-инструмента
# ver2.zip (секция [AES], AesKey=...) — см. докстринг модуля.
CANDIDATE_KEYS: list[bytes] = [
    b"0ABC12DEF3456789",
]


# --- Самостоятельная реализация AES-128 (только расшифровка) ---------------
# Таблицы и алгоритм — FIPS-197. Нужна ТОЛЬКО расшифровка (диагностика
# читает то, что реально прислал счётчик), шифрование сознательно не
# реализовано — меньше кода, меньше риска ошибки в написанном вручную
# крипто-примитиве.

def _gmul(a: int, b: int) -> int:
    """Умножение в GF(2^8) по модулю неприводимого многочлена 0x11B."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p & 0xFF


def _gf256_inverse_table() -> bytes:
    """Мультипликативная инверсия в GF(2^8) (модуль 0x11B) для всех 256
    значений; ``0`` по определению AES отображается в ``0``."""
    inv = [0] * 256
    for a in range(1, 256):
        for b in range(1, 256):
            if _gmul(a, b) == 1:
                inv[a] = b
                break
    return bytes(inv)


def _rotl8(byte: int, shift: int) -> int:
    return ((byte << shift) | (byte >> (8 - shift))) & 0xFF


def _build_sbox() -> bytes:
    """Строит прямой AES S-box по определению FIPS-197 §5.1.1: мультипликативная
    инверсия в GF(2^8), затем аффинное преобразование над GF(2). Построение
    (а не транскрипция готовой таблицы руками) исключает риск опечатки в
    256-байтной константе."""
    inv_table = _gf256_inverse_table()
    sbox = bytearray(256)
    for a in range(256):
        b = inv_table[a]
        s = b ^ _rotl8(b, 1) ^ _rotl8(b, 2) ^ _rotl8(b, 3) ^ _rotl8(b, 4) ^ 0x63
        sbox[a] = s
    return bytes(sbox)


_SBOX = _build_sbox()
_INV_SBOX = bytes(_SBOX.index(v) for v in range(256))

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _key_expansion_128(key: bytes) -> list[bytes]:
    assert len(key) == 16
    nk, nb, nr = 4, 4, 10
    w: list[bytes] = [key[4 * i : 4 * i + 4] for i in range(nk)]
    for i in range(nk, nb * (nr + 1)):
        temp = w[i - 1]
        if i % nk == 0:
            rotated = temp[1:] + temp[:1]
            subbed = bytes(_SBOX[b] for b in rotated)
            temp = bytes([subbed[0] ^ _RCON[i // nk - 1]]) + subbed[1:]
        w.append(bytes(a ^ b for a, b in zip(w[i - nk], temp)))
    return w


def _add_round_key(state: bytearray, round_words: list[bytes]) -> None:
    round_key = b"".join(round_words)
    for i in range(16):
        state[i] ^= round_key[i]


def _inv_sub_bytes(state: bytearray) -> None:
    for i in range(16):
        state[i] = _INV_SBOX[state[i]]


def _inv_shift_rows(state: bytearray) -> None:
    # state — column-major: byte i живёт в строке i%4, столбце i//4.
    rows = [[state[r + 4 * c] for c in range(4)] for r in range(4)]
    for r in range(1, 4):
        rows[r] = rows[r][-r:] + rows[r][:-r]
    for c in range(4):
        for r in range(4):
            state[r + 4 * c] = rows[r][c]


def _inv_mix_columns(state: bytearray) -> None:
    for c in range(4):
        a0, a1, a2, a3 = state[4 * c : 4 * c + 4]
        state[4 * c + 0] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        state[4 * c + 1] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        state[4 * c + 2] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        state[4 * c + 3] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)


def aes128_decrypt_block(block: bytes, key: bytes) -> bytes:
    """Расшифровывает ровно один 16-байтный блок AES-128 (без паддинга/режима)."""
    if len(block) != 16:
        raise ValueError(f"Блок AES должен быть ровно 16 байт, получено {len(block)}")
    w = _key_expansion_128(key)
    nr = 10
    state = bytearray(block)
    _add_round_key(state, w[4 * nr : 4 * nr + 4])
    for rnd in range(nr - 1, 0, -1):
        _inv_shift_rows(state)
        _inv_sub_bytes(state)
        _add_round_key(state, w[4 * rnd : 4 * rnd + 4])
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _inv_sub_bytes(state)
    _add_round_key(state, w[0:4])
    return bytes(state)


def aes128_ecb_decrypt(data: bytes, key: bytes) -> bytes | None:
    """ECB: независимая расшифровка каждого 16-байтного блока. ``None``, если
    длина не кратна 16 (заведомо не может быть валидным ECB-шифротекстом)."""
    if len(data) == 0 or len(data) % 16 != 0:
        return None
    return b"".join(aes128_decrypt_block(data[i : i + 16], key) for i in range(0, len(data), 16))


def pkcs7_unpad(data: bytes) -> bytes | None:
    if not data:
        return None
    pad_len = data[-1]
    if pad_len == 0 or pad_len > 16 or pad_len > len(data):
        return None
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return None
    return data[:-pad_len]


def _selftest_fips197() -> None:
    """FIPS-197, приложение C.1 — официальный известный ответ для AES-128."""
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    ciphertext = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    expected_plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
    actual = aes128_decrypt_block(ciphertext, key)
    if actual != expected_plaintext:
        raise AssertionError(
            "Самопроверка AES-128 (FIPS-197 KAT) ПРОВАЛЕНА — реализация неверна, "
            f"ожидали {expected_plaintext.hex()}, получили {actual.hex()}"
        )


# --- Разбор захваченных байт -------------------------------------------------


@dataclass
class FrameAttempt:
    frame_hex: str
    decode_error: str | None = None
    information_hex: str | None = None
    decrypt_results: list["DecryptAttempt"] = field(default_factory=list)


@dataclass
class DecryptAttempt:
    key: bytes
    plaintext_hex: str | None
    looks_like_dlms: bool
    note: str


def _looks_like_dlms_response(plaintext: bytes) -> tuple[bool, str]:
    if len(plaintext) < 4:
        return False, "слишком коротко после снятия паддинга"
    if plaintext[0:3] != LLC_RESPONSE_HEADER:
        return False, f"нет LLC-заголовка ответа (E6 E7 00), есть {plaintext[0:3].hex()}"
    tag = plaintext[3]
    if tag == AARE_TAG:
        return True, "похоже на AARE (тег 0x61) после LLC-заголовка"
    if tag == GET_RESPONSE_TAG:
        return True, "похоже на GET.response (тег 0xC4) после LLC-заголовка"
    return False, f"LLC-заголовок есть, но неизвестный тег после него: 0x{tag:02X}"


def _find_frame_slices(raw: bytes) -> list[bytes]:
    """Находит все подстроки между 0x7E, включая оба флага — по одной на
    каждую пару флагов подряд (не сканирует внутрь уже найденного кадра)."""
    slices = []
    start = None
    for i, b in enumerate(raw):
        if b != FLAG:
            continue
        if start is None:
            start = i
        else:
            slices.append(raw[start : i + 1])
            start = i  # закрывающий флаг кадра N — потенциальный открывающий для N+1
    return slices


def analyze_capture(raw: bytes, keys: list[bytes] | None = None) -> list[FrameAttempt]:
    keys = keys if keys is not None else CANDIDATE_KEYS
    attempts: list[FrameAttempt] = []
    for candidate in _find_frame_slices(raw):
        attempt = FrameAttempt(frame_hex=candidate.hex())
        try:
            frame = HdlcFrame.decode(candidate)
        except GatewayError as exc:
            attempt.decode_error = str(exc)
            attempts.append(attempt)
            continue
        attempt.information_hex = frame.information.hex()
        for key in keys:
            ciphertext = frame.information
            decrypted = aes128_ecb_decrypt(ciphertext, key)
            if decrypted is None:
                attempt.decrypt_results.append(
                    DecryptAttempt(
                        key=key,
                        plaintext_hex=None,
                        looks_like_dlms=False,
                        note=f"длина информационного поля ({len(ciphertext)}) не кратна 16 — не ECB-шифротекст",
                    )
                )
                continue
            unpadded = pkcs7_unpad(decrypted)
            if unpadded is None:
                attempt.decrypt_results.append(
                    DecryptAttempt(
                        key=key,
                        plaintext_hex=decrypted.hex(),
                        looks_like_dlms=False,
                        note="расшифровано, но PKCS7-паддинг невалиден (веский признак: не тот ключ/не AES)",
                    )
                )
                continue
            looks_valid, note = _looks_like_dlms_response(unpadded)
            attempt.decrypt_results.append(
                DecryptAttempt(key=key, plaintext_hex=unpadded.hex(), looks_like_dlms=looks_valid, note=note)
            )
        attempts.append(attempt)
    return attempts


def format_report(attempts: list[FrameAttempt]) -> str:
    if not attempts:
        return "В переданных байтах не найдено ни одной пары флагов 0x7E — искать нечего."
    lines = []
    for i, attempt in enumerate(attempts, 1):
        lines.append(f"=== Кадр #{i} ({len(attempt.frame_hex) // 2} байт) ===")
        lines.append(f"  сырые байты: {attempt.frame_hex}")
        if attempt.decode_error:
            lines.append(f"  HDLC-разбор НЕ УДАЛСЯ: {attempt.decode_error}")
            continue
        lines.append(f"  информационное поле: {attempt.information_hex}")
        for res in attempt.decrypt_results:
            verdict = "ПОХОЖЕ НА ВАЛИДНЫЙ DLMS!" if res.looks_like_dlms else "не похоже"
            lines.append(f"  ключ {res.key.hex()} -> {verdict} ({res.note})")
            if res.plaintext_hex:
                lines.append(f"    расшифровано: {res.plaintext_hex}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    _selftest_fips197()
    if len(argv) > 1:
        hex_input = argv[1]
    else:
        hex_input = sys.stdin.read()
    hex_input = "".join(hex_input.split())
    if not hex_input:
        print("Использование: aes_decrypt_diagnostic.py <hex-строка захваченных байт>", file=sys.stderr)
        return 2
    raw = bytes.fromhex(hex_input)
    attempts = analyze_capture(raw)
    print(format_report(attempts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
