"""Тесты политики повторов операции (session.run_with_retries)."""

import pytest

from mmws_gateway.errors import AuthFailedError, CrcError, MeterTimeoutError
from mmws_gateway.session import run_with_retries


def test_succeeds_without_retry():
    calls = []

    def op():
        calls.append(1)
        return 42

    assert run_with_retries(op, max_retries=3) == 42
    assert len(calls) == 1


def test_retries_on_crc_error_then_succeeds():
    calls = []

    def op():
        calls.append(1)
        if len(calls) < 2:
            raise CrcError("bad crc")
        return "ok"

    assert run_with_retries(op, max_retries=3) == "ok"
    assert len(calls) == 2


def test_auth_failed_never_retried():
    calls = []

    def op():
        calls.append(1)
        raise AuthFailedError("wrong password")

    with pytest.raises(AuthFailedError):
        run_with_retries(op, max_retries=3)
    assert len(calls) == 1


def test_exhausts_retries_and_raises_last_error():
    calls = []

    def op():
        calls.append(1)
        raise MeterTimeoutError("no response")

    with pytest.raises(MeterTimeoutError):
        run_with_retries(op, max_retries=2)
    assert len(calls) == 2
