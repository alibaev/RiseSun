import { useEffect, useState } from "react";

export const DEFAULT_PAGE_SIZE = 50;

// Пагинация на клиенте (по прямому указанию пользователя, 2026-09-17,
// после жалобы на подтормаживание списков с тысячами строк — рендер
// одной страницы в 50 строк вместо всего списка снимает основную
// нагрузку на DOM). `resetDeps` — значения фильтров/поиска, при смене
// которых текущая страница сбрасывается на первую (иначе можно
// остаться на пустой странице №40 после нового поиска).
export function usePagination<T>(rows: T[], resetDeps: unknown[] = [], pageSize: number = DEFAULT_PAGE_SIZE) {
  const [page, setPage] = useState(1);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setPage(1), resetDeps);

  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  const currentPage = Math.min(page, pageCount);
  const start = (currentPage - 1) * pageSize;
  const pageRows = rows.slice(start, start + pageSize);

  return { page: currentPage, setPage, pageCount, pageRows, pageSize, total: rows.length, start };
}
