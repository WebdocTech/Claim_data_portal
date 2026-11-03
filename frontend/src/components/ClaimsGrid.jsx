import { useEffect, useMemo, useRef, useState } from "react";
import StatusControl, { canChange } from "./StatusControl.jsx";
import HistoryModal from "./HistoryModal.jsx";
import { fmtAmount, fmtDate } from "../format.js";

const PAGE_SIZE = 50;
const DATE_KEYS = new Set(["approved_date", "claim_date"]);
const AMOUNT_KEYS = new Set(["approved_amount", "claim_amount", "suggest_amount"]);

function cellText(row, key) {
  const v = row[key];
  if (DATE_KEYS.has(key)) return fmtDate(v);
  if (AMOUNT_KEYS.has(key)) return fmtAmount(v);
  return v ?? "";
}

// Searchable, filterable, paged claims table shared by the Finance and Claims pages.
export default function ClaimsGrid({ rows, columns, role, allowed, statuses, pending, setPending, extraFilters, toolbar }) {
  const [q, setQ] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(0);
  const [historyFor, setHistoryFor] = useState(null);
  const [sort, setSort] = useState(null); // { key, dir: "asc" | "desc" } on a date column

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const out = rows.filter((r) => {
      if (statusFilter && r.payment_status !== statusFilter) return false;
      if (extraFilters && !extraFilters(r)) return false;
      if (!needle) return true;
      return columns.some(([key]) => String(r[key] ?? "").toLowerCase().includes(needle));
    });
    if (sort && columns.some(([key]) => key === sort.key)) {
      // Dates are "YYYY-MM-DD HH:MM:SS" text, so string order is date order; blanks go last.
      const sign = sort.dir === "asc" ? 1 : -1;
      out.sort((a, b) => {
        const x = a[sort.key], y = b[sort.key];
        if (!x || !y) return x ? -1 : y ? 1 : 0;
        return x < y ? -sign : x > y ? sign : 0;
      });
    }
    return out;
  }, [rows, q, statusFilter, columns, extraFilters, sort]);

  function toggleSort(key) {
    setSort((s) => ({ key, dir: s?.key === key && s.dir === "asc" ? "desc" : "asc" }));
    setPage(0);
  }

  // Selection covers rows whose status this user can change, across all pages of the
  // current filter. Changing the filter clears it so hidden rows are never changed by accident.
  const [selected, setSelected] = useState(() => new Set());
  const selectable = role !== "admin" && allowed.length > 0;
  const selectableIds = useMemo(
    () => filtered.filter((r) => canChange(r, role, allowed)).map((r) => r.claim_id),
    [filtered, role, allowed]
  );
  useEffect(() => setSelected(new Set()), [q, statusFilter, extraFilters]);
  // Drop ids that are no longer changeable (e.g. just marked Paid and moved to another tab).
  useEffect(() => {
    const ok = new Set(selectableIds);
    setSelected((s) => ([...s].every((id) => ok.has(id)) ? s : new Set([...s].filter((id) => ok.has(id)))));
  }, [selectableIds]);
  const selectedIds = useMemo(() => [...selected], [selected]);
  const allSelected = selectableIds.length > 0 && selected.size === selectableIds.length;
  const headerBox = useRef(null);
  useEffect(() => {
    if (headerBox.current) headerBox.current.indeterminate = selected.size > 0 && !allSelected;
  }, [selected, allSelected]);

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(selectableIds));
  }

  // Picking a status on a selected row drafts it for the whole selection.
  function pick(claim, status) {
    if (selected.has(claim.claim_id) && selected.size > 1) {
      setPending(selectedIds, status);
      setSelected(new Set());
    } else {
      setPending([claim.claim_id], status);
    }
  }

  function toggleOne(id) {
    setSelected((s) => {
      const next = new Set(s);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const current = Math.min(page, pages - 1);
  const visible = filtered.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);

  return (
    <>
      <div className="toolbar">
        <input
          className="search"
          placeholder="Search claim no, mobile, account, name, service…"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setPage(0);
          }}
        />
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value);
            setPage(0);
          }}
        >
          <option value="">All payment statuses</option>
          {statuses.map((s) => (
            <option key={s}>{s}</option>
          ))}
        </select>
        {toolbar?.(statusFilter)}
        <span className="muted count">{filtered.length.toLocaleString()} claims</span>
      </div>

      {selected.size > 0 && (
        <div className="selection-bar">
          <strong>{selected.size.toLocaleString()} selected</strong>
          <span className="muted">Change the status on any selected row to apply it to all of them, then Save changes.</span>
          <button className="link" onClick={() => setSelected(new Set())}>
            Clear selection
          </button>
        </div>
      )}

      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              {selectable && (
                <th className="check">
                  <input
                    type="checkbox"
                    ref={headerBox}
                    checked={allSelected}
                    disabled={!selectableIds.length}
                    onChange={toggleAll}
                    title={`Select all ${selectableIds.length} changeable claims`}
                  />
                </th>
              )}
              {columns.map(([key, label]) => (
                <th key={key} className={AMOUNT_KEYS.has(key) ? "num" : ""}>
                  {DATE_KEYS.has(key) ? (
                    <button className="sort-head" onClick={() => toggleSort(key)} title="Sort by date">
                      {label}
                      <span className="sort-arrow">
                        {sort?.key === key ? (sort.dir === "asc" ? "▲" : "▼") : "↕"}
                      </span>
                    </button>
                  ) : (
                    label
                  )}
                </th>
              ))}
              <th>History</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => (
              <tr
                key={r.claim_id}
                className={[
                  r.is_paid && "paid-row",
                  pending.has(r.claim_id) && "draft-row",
                  selected.has(r.claim_id) && "selected-row",
                ]
                  .filter(Boolean)
                  .join(" ")}
              >
                {selectable && (
                  <td className="check">
                    {canChange(r, role, allowed) && (
                      <input type="checkbox" checked={selected.has(r.claim_id)} onChange={() => toggleOne(r.claim_id)} />
                    )}
                  </td>
                )}
                {columns.map(([key]) => (
                  <td key={key} className={AMOUNT_KEYS.has(key) ? "num" : ""}>
                    {key === "payment_status" ? (
                      <StatusControl
                        claim={r}
                        role={role}
                        allowed={allowed}
                        draft={pending.get(r.claim_id)}
                        bulkCount={selected.has(r.claim_id) ? selected.size : 0}
                        onPick={(status) => pick(r, status)}
                      />
                    ) : (
                      cellText(r, key)
                    )}
                  </td>
                ))}
                <td>
                  <button className="link" onClick={() => setHistoryFor(r)}>
                    View
                  </button>
                </td>
              </tr>
            ))}
            {!visible.length && (
              <tr>
                <td colSpan={columns.length + (selectable ? 2 : 1)} className="muted center-text">
                  No claims match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {pages > 1 && (
        <div className="pager">
          <button disabled={current === 0} onClick={() => setPage(current - 1)}>
            ‹ Prev
          </button>
          <span>
            Page {current + 1} of {pages}
          </span>
          <button disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
            Next ›
          </button>
        </div>
      )}

      {historyFor && <HistoryModal claim={historyFor} onClose={() => setHistoryFor(null)} />}
    </>
  );
}
