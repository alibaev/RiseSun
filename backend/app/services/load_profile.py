"""Профиль нагрузки (Этап 3, ТЗ п.4.2.3) — константы, общие для API-слоя
(app/api/meters.py) и воркера задач (app/services/job_worker.py).
"""

from __future__ import annotations

# Стандартный DLMS-адрес "Load Profile 1" (decimal 1-0:99.1.0.255,
# IEC 62056-6-2) в hex-нотации проекта (decimal C=99 -> hex "63",
# F=255 -> hex "ff", см. правило конвертации в DECISIONS.md, «Этап 2,
# итерация 3») — рабочая гипотеза, объект целиком отсутствует в словаре
# OBIS.xlsx, подлежит проверке на реальном оборудовании (см.
# DECISIONS.md, «Этап 3»). Эндпоинт POST /api/meters/{id}/read-load-profile
# принимает необязательный override ``obis`` именно на случай, если
# гипотеза не подтвердится и потребуется другой адрес без переразвёртывания.
DEFAULT_LOAD_PROFILE_OBIS = "1.0.63.1.0.ff"
