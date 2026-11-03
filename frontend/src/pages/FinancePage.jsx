import { useMemo, useState } from "react";
import { api, downloadExport } from "../api.js";
import ClaimsGrid from "../components/ClaimsGrid.jsx";
import SaveBar from "../components/SaveBar.jsx";
import useClaims from "../useClaims.js";

export default function FinancePage({ session }) {
  const claims = useClaims(api.financeClaims);
  const { rows, error, reload, pending, setPending } = claims;
  const [view, setView] = useState("customer");
  const [exporting, setExporting] = useState(false);
  const views = session.finance_views;
  const readOnly = session.user.role === "admin";

  // Working tabs hold unpaid claims; the Paid tab holds the rest.
  const paidCount = useMemo(() => (rows || []).filter((r) => r.is_paid).length, [rows]);
  const viewRows = useMemo(
    () => (rows || []).filter((r) => r.is_paid === views[view].paid),
    [rows, view, views]
  );

  async function exportView(statusFilter) {
    setExporting(true);
    try {
      await downloadExport(view, statusFilter);
    } catch (e) {
      alert(e.message);
    } finally {
      setExporting(false);
    }
  }

  return (
    <section>
      <div className="page-head">
        <h2>Approved claims</h2>
        {readOnly && <span className="badge neutral">Read-only</span>}
        <button
          className="link"
          onClick={() => (!pending.size || window.confirm(`Discard ${pending.size} unsaved changes and reload?`)) && reload()}
        >
          Refresh
        </button>
      </div>
      <SaveBar {...claims} />
      <div className="subtabs">
        {Object.entries(views).map(([key, v]) => (
          <button key={key} className={view === key ? "tab active" : "tab"} onClick={() => setView(key)}>
            {v.title}
            {rows && (
              <span className="tab-count">
                {(v.paid ? paidCount : rows.length - paidCount).toLocaleString()}
              </span>
            )}
          </button>
        ))}
      </div>
      {error && <p className="error">{error}</p>}
      {!rows && !error && <p className="muted">Loading claims from Digital_Insurance…</p>}
      {rows && (
        <ClaimsGrid
          key={view}
          rows={viewRows}
          columns={views[view].columns}
          role={session.user.role}
          allowed={session.allowed_statuses}
          statuses={session.statuses}
          pending={pending}
          setPending={setPending}
          toolbar={(statusFilter) => (
            <button disabled={exporting} onClick={() => exportView(statusFilter)}>
              {exporting ? "Exporting…" : "Export CSV"}
            </button>
          )}
        />
      )}
    </section>
  );
}
