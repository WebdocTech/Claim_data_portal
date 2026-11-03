export const fmtDate = (v) => (v ? String(v).slice(0, 10) : "");

export function fmtDateTime(v) {
  if (!v) return "";
  const d = new Date(v);
  return isNaN(d) ? String(v).slice(0, 16) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export const fmtAmount = (v) => (v == null ? "" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }));
