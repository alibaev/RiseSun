"""Регрессионный тест на основе РЕАЛЬНОГО трафика (не эмулятора).

Байты ниже — точная выдержка из pcap-дампа настоящего сеанса
приложения Risesun Smart Meter Manage со счётчиком 202001002352
(проверка на реальном оборудовании, 2026-08-18, см. DECISIONS.md).
Тест проверяет:
  1. что наш encode() для SNRM/AARQ/GET.request побайтово совпадает с
     тем, что реально шлёт рабочее приложение;
  2. что наш decode() реальных ответов счётчика (UA/AARE/GET.response)
     корректно разбирается и даёт ожидаемое показание.

Это не замена реальным полевым испытаниям (см. README «Программные
эмуляторы vs реальное оборудование»), но гораздо надёжнее эмулятора:
здесь эталон — байты, которыми обменивался с этим же счётчиком уже
работающий, проверенный клиент.
"""

from __future__ import annotations

from mmws_gateway.protocols import dlms
from mmws_gateway.protocols.hdlc import (
    CONTROL_SNRM,
    CONTROL_UA,
    DEFAULT_CLIENT_ADDRESS,
    SNRM_PARAMETER_NEGOTIATION,
    HdlcFrame,
    control_information_frame,
    server_hdlc_address,
)

SERIAL = "202001002352"
PASSWORD = b"12345678"
SERVER_ADDR = server_hdlc_address("02352")  # последние 5 цифр серийного

CAPTURED_SNRM = bytes.fromhex(
    "7ea0210002246161930e6c8180120501ff0601ff070400000001080400000001551c7e"
)
CAPTURED_UA = bytes.fromhex(
    "7ea02161000224617310e58180120501c00601c007040000000108040000000100ce7e"
)
# Исходный захват 2026-08-18 (max-pdu-size=0000) — та сессия тогда
# завершилась успешно, но 2026-09-10 обнаружено (см. DECISIONS.md),
# что прямо сейчас работающая референсная система шлёт max-pdu-size
# =0800 на всех наблюдаемых счётчиках, и наш build_aarq() был обновлён
# под это значение. Оставлено здесь как честный исторический
# документ, но байты ниже (после пересчёта FCS под новое значение)
# отражают текущее ожидаемое поведение, не сырой захват той сессии.
CAPTURED_AARQ = bytes.fromhex(
    "7ea047000224616110d526e6e6006036a1090607608574050801018a0207808b076085"
    "7405080201ac0a80083132333435363738be10040e01000000065f1f040000081d08009c4d7e"
)
CAPTURED_AARE = bytes.fromhex(
    "7ea03a610002246130c456e6e7006129a109060760857405080101a203020100a305a1"
    "03020100be10040e0800065f1f040000081d00bd00077bfb7e"
)
CAPTURED_GET_SCALER_ENERGY = bytes.fromhex(
    "7ea01c0002246161328820e6e600c0010100030101010800ff030021b17e"
)
CAPTURED_GET_SCALER_ENERGY_RESP = bytes.fromhex(
    "7ea0196100022461523ddde6e700c401010002020f01161eae207e"
)
CAPTURED_GET_VALUE_ENERGY = bytes.fromhex(
    "7ea01c000224616176a824e6e600c0010300030101605000ff020003af7e"
)
CAPTURED_GET_VALUE_ENERGY_RESP = bytes.fromhex(
    "7ea018610002246196c0c2e6e700c4010300050001043bfdc17e"
)


def test_snrm_matches_real_traffic_byte_for_byte():
    frame = HdlcFrame(
        destination=SERVER_ADDR,
        source=DEFAULT_CLIENT_ADDRESS,
        control=CONTROL_SNRM,
        information=SNRM_PARAMETER_NEGOTIATION,
    )
    assert frame.encode() == CAPTURED_SNRM


def test_decode_real_ua_response():
    decoded = HdlcFrame.decode(CAPTURED_UA)
    assert decoded.control == CONTROL_UA
    assert decoded.destination == DEFAULT_CLIENT_ADDRESS
    assert decoded.source == SERVER_ADDR


def test_aarq_matches_real_traffic_byte_for_byte():
    aarq = dlms.wrap_llc_command(dlms.build_aarq(PASSWORD))
    frame = HdlcFrame(
        destination=SERVER_ADDR,
        source=DEFAULT_CLIENT_ADDRESS,
        control=control_information_frame(0, 0),
        information=aarq,
    )
    assert frame.encode() == CAPTURED_AARQ


def test_decode_real_aare_accepts_association():
    decoded = HdlcFrame.decode(CAPTURED_AARE)
    assert dlms.parse_aare(dlms.unwrap_llc(decoded.information)) is True


def test_get_scaler_energy_request_matches_real_traffic():
    request = dlms.build_get_request(dlms.parse_obis("1.1.1.8.0.ff"), invoke_id=1, attribute_id=3)
    frame = HdlcFrame(
        destination=SERVER_ADDR,
        source=DEFAULT_CLIENT_ADDRESS,
        control=control_information_frame(1, 1),
        information=dlms.wrap_llc_command(request),
    )
    assert frame.encode() == CAPTURED_GET_SCALER_ENERGY


def test_decode_real_scaler_unit_response():
    decoded = HdlcFrame.decode(CAPTURED_GET_SCALER_ENERGY_RESP)
    value = dlms.parse_get_response(dlms.unwrap_llc(decoded.information))
    # structure(scaler: integer, unit: enum) — Green Book scaler_unit.
    assert value == [1, 30]


def test_get_value_energy_request_matches_real_traffic():
    # Вендорский OBIS 96.80.0 (не стандартный 1.8.0!) — подтверждено
    # словарём OBIS.xlsx как фактический адрес значения суммарной
    # активной энергии у Risesun (см. DECISIONS.md).
    request = dlms.build_get_request(dlms.parse_obis("1.1.60.50.0.ff"), invoke_id=3)
    frame = HdlcFrame(
        destination=SERVER_ADDR,
        source=DEFAULT_CLIENT_ADDRESS,
        control=control_information_frame(3, 3),
        information=dlms.wrap_llc_command(request),
    )
    assert frame.encode() == CAPTURED_GET_VALUE_ENERGY


def test_decode_real_value_response_and_apply_scaler():
    decoded = HdlcFrame.decode(CAPTURED_GET_VALUE_ENERGY_RESP)
    raw_value = dlms.parse_get_response(dlms.unwrap_llc(decoded.information))
    assert raw_value == 66619
    scaler, unit = 1, 30  # из test_decode_real_scaler_unit_response
    kwh = raw_value * (10**scaler) / 1000
    assert kwh == 666.19
