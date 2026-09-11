"""Тесты для scripts/aes_decrypt_diagnostic.py (см. DECISIONS.md, запись
«AES-шифрование APDU во второй референсной программе») — самостоятельная
реализация AES-128 и разбор захваченных байт для проверки гипотезы
«счётчик отвечает AES-зашифрованным APDU».

Фикстура ``SYNTHETIC_ENCRYPTED_FRAME_HEX`` сгенерирована ОДИН РАЗ
независимым путём — библиотекой ``cryptography`` (AES-128-ECB/PKCS7),
ключом ``0ABC12DEF3456789`` (восстановлен из боевого Config.ini
легаси-инструмента ``ver2.zip``), информационное поле обёрнуто нашим
собственным ``protocols.hdlc.HdlcFrame.encode()`` — это и есть образец
того, как выглядел бы реальный захваченный AES-зашифрованный ответ.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "aes_decrypt_diagnostic.py"
_spec = importlib.util.spec_from_file_location("aes_decrypt_diagnostic", _SCRIPT_PATH)
aes_decrypt_diagnostic = importlib.util.module_from_spec(_spec)
sys.modules["aes_decrypt_diagnostic"] = aes_decrypt_diagnostic
_spec.loader.exec_module(aes_decrypt_diagnostic)

# Сгенерировано: LLC-заголовок ответа + build_aare(accepted=True),
# дополнено PKCS7 до 16 байт, зашифровано AES-128-ECB ключом
# b"0ABC12DEF3456789" (через cryptography, независимо от кода в скрипте),
# обёрнуто в HdlcFrame(destination=0x30, source=0x01, control=0x00, ...).
SYNTHETIC_ENCRYPTED_FRAME_HEX = "7ea019610300886ae2ce515258d51bea88c59ec6b1c0c9df03dd7e"
EXPECTED_PLAINTEXT_HEX = "e6e7006105a203020100"


def test_fips197_known_answer_test():
    aes_decrypt_diagnostic._selftest_fips197()


def test_recovers_synthetic_encrypted_aare():
    raw = bytes.fromhex(SYNTHETIC_ENCRYPTED_FRAME_HEX)
    attempts = aes_decrypt_diagnostic.analyze_capture(raw)
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.decode_error is None
    assert len(attempt.decrypt_results) == 1
    result = attempt.decrypt_results[0]
    assert result.looks_like_dlms is True
    assert result.plaintext_hex == EXPECTED_PLAINTEXT_HEX


def test_garbage_bytes_report_no_frames_found():
    raw = bytes.fromhex("deadbeefcafebabe1122334455667788")
    attempts = aes_decrypt_diagnostic.analyze_capture(raw)
    assert attempts == []


def test_plaintext_unencrypted_frame_is_not_mistaken_for_encrypted():
    from mmws_gateway.protocols.dlms import LLC_RESPONSE_HEADER, build_aare
    from mmws_gateway.protocols.hdlc import HdlcFrame

    info = LLC_RESPONSE_HEADER + build_aare(accepted=True)
    frame = HdlcFrame(destination=0x30, source=0x01, control=0x00, information=info)
    attempts = aes_decrypt_diagnostic.analyze_capture(frame.encode())
    assert len(attempts) == 1
    result = attempts[0].decrypt_results[0]
    assert result.looks_like_dlms is False


@pytest.mark.parametrize(
    "key_hex, plaintext_hex",
    [
        ("000102030405060708090a0b0c0d0e0f", "00112233445566778899aabbccddeeff"),
    ],
)
def test_aes128_decrypt_block_matches_fips197(key_hex, plaintext_hex):
    ciphertext = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    result = aes_decrypt_diagnostic.aes128_decrypt_block(ciphertext, bytes.fromhex(key_hex))
    assert result == bytes.fromhex(plaintext_hex)


def test_pkcs7_unpad_rejects_invalid_padding():
    assert aes_decrypt_diagnostic.pkcs7_unpad(b"\x01\x02\x03\x00") is None
    assert aes_decrypt_diagnostic.pkcs7_unpad(b"abc\x02\x02") == b"abc"
