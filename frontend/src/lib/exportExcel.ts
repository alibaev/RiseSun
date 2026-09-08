import * as XLSX from "xlsx";

// По просьбе пользователя (2026-09-08) — экспорт таблиц в .xlsx.
// Генерация полностью на клиенте (SheetJS/xlsx) — не требует
// отдельного backend-эндпоинта, т.к. экспортируемые данные уже
// загружены на страницу.
export function exportToExcel(filename: string, sheetName: string, rows: Record<string, unknown>[]): void {
  const worksheet = XLSX.utils.json_to_sheet(rows);
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, sheetName);
  XLSX.writeFile(workbook, filename.endsWith(".xlsx") ? filename : `${filename}.xlsx`);
}
