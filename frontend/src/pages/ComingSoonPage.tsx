import { useSearchParams } from "react-router-dom";

// 2026-08-20: общая страница-заглушка для пунктов меню, добавленных по
// референсному менеджеру другого завода (sunrise.pdf), но ещё не
// реализованных в MMWS (нет backend). См. constants/menu.ts.
export function ComingSoonPage() {
  const [params] = useSearchParams();
  const title = params.get("title") ?? "Раздел";

  return (
    <div className="card">
      <div className="card-header">
        <h2>{title}</h2>
      </div>
      <p className="coming-soon-note">
        Пункт меню добавлен по референсному интерфейсу другого завода — backend для этого раздела в MMWS ещё не
        реализован.
      </p>
    </div>
  );
}
