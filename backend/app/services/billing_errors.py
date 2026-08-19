"""Единый формат ошибок сегмента /api/v1/billing/* (API.docx раздел 5,
Приложение А.2). Исключение перехватывается глобальным exception handler
в app/main.py — не используется вне billing-роутов."""

from __future__ import annotations


class BillingApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after
