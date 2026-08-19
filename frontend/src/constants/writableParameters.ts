// Этап 2 (ТЗ п.4.2.4): одиночные параметры из реестра WRITABLE_INT_PARAMETERS
// (Backend, app/services/write_parameters.py) — общий список подписей,
// используется и карточкой счётчика (запись одного параметра), и Этапом 4
// (схемы параметров, ТЗ п.4.2.5), где схема — набор пар этого же реестра.
export const WRITABLE_PARAMS: { key: string; label: string }[] = [
  { key: "settlement_no", label: "Текущий номер расчётного периода" },
  { key: "available_settlement_no", label: "Доступный номер расчётного периода" },
  { key: "load_profile_interval", label: "Интервал профиля нагрузки (мин)" },
  { key: "display_mode_count", label: "Количество режимов отображения" },
  { key: "weekend_rate_type", label: "Тип тарифа выходного дня" },
];

export const WRITABLE_PARAM_LABELS: Record<string, string> = Object.fromEntries(
  WRITABLE_PARAMS.map(({ key, label }) => [key, label])
);
