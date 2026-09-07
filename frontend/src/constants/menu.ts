// 2026-08-20: структура верхнего меню сверена с референсным менеджером
// другого завода (Sunrise, файл sunrise.pdf, 9 скриншотов интерфейса) —
// по решению пользователя добавлены ВСЕ пункты меню, которые видны на
// скриншотах, даже там, где у MMWS ещё нет соответствующего backend'а.
// Пункты без backend ведут на общую страницу-заглушку ComingSoonPage
// (`implemented: false`) — там же указано, что раздел ждёт реализации.
// Список всех пунктов без backend — см. обсуждение с пользователем
// в сессии, где заведён этот файл (не хранится отдельно в репозитории,
// т.к. это рабочий список для обсуждения, а не проектное решение).
export interface MenuLeaf {
  label: string;
  to: string;
  implemented: boolean;
}

export interface MenuCategory {
  label: string;
  items: MenuLeaf[];
}

function stub(label: string): MenuLeaf {
  return { label, to: `/soon?title=${encodeURIComponent(label)}`, implemented: false };
}

export const MENU: MenuCategory[] = [
  {
    label: "Архив",
    items: [
      stub("Энергосеть (иерархия)"),
      stub("Абоненты"),
      stub("SIM-карты"),
      { label: "Счётчики", to: "/meters", implemented: true },
      stub("Точки учёта (POC)"),
      stub("Концентраторы (DCU)"),
    ],
  },
  {
    label: "Параметры",
    items: [
      stub("Поставщики"),
      stub("Типы счётчиков"),
      stub("Типы DCU"),
      stub("Планы опроса"),
      { label: "Схемы параметров", to: "/schemes", implemented: true },
      stub("Планы прошивок"),
      stub("Модели связи"),
      stub("Карта OBIS-кодов"),
      stub("Коды событий"),
    ],
  },
  {
    label: "Обслуживание",
    items: [
      stub("Работа со счётчиком (общая)"),
      stub("Опрос по GPRS"),
      stub("Опрос DCU"),
      stub("Опрос по PLC/RS485"),
      { label: "Расписания опроса", to: "/scheduled-jobs", implemented: true },
      stub("Задачи параметров (Param Task Setting)"),
      stub("Обновление прошивки"),
      stub("Анализ сети счётчиков"),
      stub("Результат настройки тарифа"),
    ],
  },
  {
    label: "Анализ",
    items: [
      stub("События счётчиков"),
      stub("События DCU"),
      stub("Суточный биллинг"),
      stub("Месячный биллинг"),
      stub("Профиль нагрузки 1"),
      stub("Профиль нагрузки 2"),
      stub("Потребление"),
      stub("Процент сбора данных (Acquisition Rate)"),
      stub("Потери в сети (Lineloss Rate)"),
      stub("Процент онлайн (Online Rate)"),
      stub("Анализ напряжения"),
      stub("Анализ тока"),
    ],
  },
  {
    label: "Отчёты",
    items: [stub("Отчёты (подменю на скриншотах не раскрыто)")],
  },
  {
    label: "Журнал",
    items: [
      { label: "Журнал аудита", to: "/audit-log", implemented: true },
      stub("Журнал операций (Operating Log)"),
      stub("Журнал входов (Login Log)"),
    ],
  },
  {
    label: "Монтаж",
    items: [stub("Инструмент установки (подменю на скриншотах не раскрыто)")],
  },
  {
    label: "Система",
    items: [
      stub("Настройки"),
      stub("Роли"),
      { label: "Пользователи", to: "/users", implemented: true },
      stub("Группы пользователей"),
      stub("Мультиязычность"),
      stub("Шаблоны"),
      stub("Правила уведомлений"),
      stub("Подписки"),
      { label: "API-ключи биллинга", to: "/billing-keys", implemented: true },
      { label: "Gateway", to: "/gateways", implemented: true },
    ],
  },
];
