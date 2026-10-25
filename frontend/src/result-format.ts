/** Normalize wire timestamps to UTC while retaining the source string in the UI. */
export function resultTime(value: string): string {
  const parts = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/i.exec(value);
  if (!parts) return "—";
  const timestamp = Date.parse(parts[1] + parts[3]);
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toISOString().slice(0, 19).replace("T", " ") + (parts[2] ?? "") : "—";
}
