"""Интерфейс командной строки Protocol Gateway (Этап 0 PoC).

Пример:
    gateway read --profile hdlc_dlms --host 127.0.0.1 --port 4059 \\
        --serial 202006003607 --obis 1.1.1.8.0.ff

Пароль передаётся через переменную окружения ``MMWS_METER_PASSWORD``,
а не аргументом командной строки — чтобы не попадать в историю оболочки
и список процессов (Promt_MMWS.md, раздел 3, принцип 4: секреты не в
открытом виде). Явный ``--password`` тоже поддержан для локальной
отладки с эмулятором, но не рекомендуется вне неё.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Sequence

from .addressing import HDLC_DLMS, MODE_C, MODE_E, PROFILES
from .errors import GatewayError
from .protocols import hdlc_dlms, mode_c, mode_e
from .session import run_with_retries
from .transport import DEFAULT_RETRIES, DEFAULT_TIMEOUT_MS, TcpTransport, TransportConfig

PASSWORD_ENV_VAR = "MMWS_METER_PASSWORD"

logger = logging.getLogger("mmws_gateway.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gateway", description="MMWS Protocol Gateway — PoC Этапа 0"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    read_parser = subparsers.add_parser("read", help="Прочитать одно значение со счётчика")
    read_parser.add_argument("--profile", required=True, choices=PROFILES)
    read_parser.add_argument("--host", required=True)
    read_parser.add_argument("--port", required=True, type=int)
    read_parser.add_argument("--serial", required=True, help="Серийный номер счётчика")
    read_parser.add_argument(
        "--obis",
        required=True,
        help="OBIS-код в 6-байтной hex-нотации A.B.C.D.E.F (например, 1.1.1.8.0.ff)",
    )
    read_parser.add_argument(
        "--password",
        default=None,
        help=f"Пароль доступа (LLS). Если не задан — берётся из {PASSWORD_ENV_VAR}",
    )
    read_parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    read_parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    return parser


def _resolve_password(args: argparse.Namespace) -> bytes:
    password = args.password if args.password is not None else os.environ.get(PASSWORD_ENV_VAR, "")
    if not password and args.profile in (MODE_E, HDLC_DLMS):
        raise SystemExit(
            f"Пароль доступа обязателен для профиля {args.profile}: задайте --password "
            f"или переменную окружения {PASSWORD_ENV_VAR}"
        )
    return password.encode("ascii")


def _do_read(args: argparse.Namespace) -> object:
    password = _resolve_password(args)
    # Внутренний повтор подключения в TcpTransport отключён (max_retries=1):
    # весь цикл подключение+обмен ретраится целиком через session.run_with_retries
    # (см. обоснование в docstring mmws_gateway.session).
    config = TransportConfig(host=args.host, port=args.port, timeout_ms=args.timeout_ms, max_retries=1)

    def operation() -> object:
        with TcpTransport(config) as transport:
            if args.profile == MODE_C:
                short_code = mode_c.obis6_to_short_code(args.obis)
                return mode_c.read_value(transport, serial=args.serial, obis_5=short_code)
            if args.profile == MODE_E:
                return mode_e.read_register(
                    transport, serial=args.serial, password=password, obis=args.obis
                )
            return hdlc_dlms.read_register(
                transport, serial=args.serial, password=password, obis=args.obis
            )

    return run_with_retries(operation, max_retries=args.retries)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "read":
        try:
            value = _do_read(args)
        except GatewayError as exc:
            print(f"ОШИБКА [{exc.code}]: {exc.message}", file=sys.stderr)
            if exc.is_partial:
                print(
                    "Внимание: часть данных была получена до обрыва (is_partial=True)",
                    file=sys.stderr,
                )
            return 1
        print(f"{args.obis} = {value}")
        return 0

    parser.error("Неизвестная команда")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
