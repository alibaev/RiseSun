interface PaginationProps {
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
  total: number;
  start: number;
  pageSize: number;
}

export function Pagination({ page, pageCount, onPageChange, total, start, pageSize }: PaginationProps) {
  if (pageCount <= 1) return null;

  const rangeEnd = Math.min(start + pageSize, total);

  return (
    <div className="pagination">
      <span className="hint">
        {start + 1}–{rangeEnd} из {total}
      </span>
      <div className="btn-group">
        <button className="secondary" onClick={() => onPageChange(1)} disabled={page <= 1}>
          « Первая
        </button>
        <button className="secondary" onClick={() => onPageChange(page - 1)} disabled={page <= 1}>
          ‹ Назад
        </button>
        <span>
          Стр. {page} из {pageCount}
        </span>
        <button className="secondary" onClick={() => onPageChange(page + 1)} disabled={page >= pageCount}>
          Вперёд ›
        </button>
        <button className="secondary" onClick={() => onPageChange(pageCount)} disabled={page >= pageCount}>
          Последняя »
        </button>
      </div>
    </div>
  );
}
