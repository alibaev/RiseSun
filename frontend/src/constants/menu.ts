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
    // 2026-09-12, по прямому указанию пользователя — новый пункт меню
    // "Работа с счётчиками", выше "Архива". Не путать со справочником
    // счётчиков ("Архив → Счётчики" — карточка ОДНОГО счётчика,
    // параметры/история). Изначально была одна общая страница
    // "Групповые операции" — 2026-09-13 пользователь указал, что так
    // неудобно, и попросил разбить на 4 отдельных пункта: показания/
    // напряжение/ампер/реле+откл-подкл отдельно от запроса профиля
    // нагрузки, и один счётчик отдельно от группы.
    label: "Работа с счётчиками",
    items: [
      { label: "Опрос счётчика", to: "/meter-operations/poll", implemented: true },
      { label: "Опрос профиля", to: "/meter-operations/profile", implemented: true },
      { label: "Опрос группы счётчиков", to: "/meter-operations/poll-group", implemented: true },
      { label: "Опрос профиля группой", to: "/meter-operations/profile-group", implemented: true },
    ],
  },
  {
    label: "Архив",
    items: [
      stub("Энергосеть (иерархия)"),
      stub("Абоненты"),
      stub("SIM-карты"),
      { label: "Счётчики", to: "/meters", implemented: true },
      { label: "РЭСы и Объекты", to: "/res", implemented: true },
      stub("Точки учёта (POC)"),
    ],
  },
  {
    label: "Параметры",
    items: [
      stub("Поставщики"),
      stub("Типы счётчиков"),
      stub("Планы опроса"),
      { label: "Схемы параметров", to: "/schemes", implemented: true },
      stub("Планы прошивок"),
      stub("Модели связи"),
      { label: "Карта OBIS-кодов", to: "/obis-catalog", implemented: true },
      stub("Коды событий"),
    ],
  },
  {
    label: "Обслуживание",
    items: [
      stub("Работа со счётчиком (общая)"),
      stub("Опрос по GPRS"),
      { label: "Профили опроса", to: "/poll-profiles", implemented: true },
      { label: "Расписания опроса", to: "/scheduled-jobs", implemented: true },
      { label: "Экспорт профиля в ЕЭБД", to: "/eudb-export", implemented: true },
      stub("Задачи параметров (Param Task Setting)"),
      stub("Анализ сети счётчиков"),
    ],
  },
  {
    label: "Анализ",
    items: [
      stub("Суточный биллинг"),
      stub("Профиль нагрузки 1"),
      stub("Профиль нагрузки 2"),
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
      { label: "О программе", to: "/about", implemented: true },
    ],
  },
];
