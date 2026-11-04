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
  // The problems actually present across every approved claim, named and counted, so the
  // page says what is wrong before any tab is opened. Worst severity first, then volume.
  const flaggedCount = useMemo(() => (rows || []).filter((r) => r.risk).length, [rows]);
  const topProblems = useMemo(() => {
    const byRule = new Map();
    for (const r of rows || []) {
      for (const code of r.rules || []) {
        const prev = byRule.get(code) || { count: 0, severity: "info" };
        // A rule can be a blocker on one claim and a warning on another; show the worst.
        const worse = ["blocker", "warn", "info"].indexOf(r.risk) <
                      ["blocker", "warn", "info"].indexOf(prev.severity);
        byRule.set(code, { count: prev.count + 1, severity: worse ? r.risk : prev.severity });
      }
    }
    const rank = { blocker: 0, warn: 1, info: 2 };
    return [...byRule.entries()]
      .map(([code, v]) => [code, v.count, v.severity])
      .sort((a, b) => rank[a[2]] - rank[b[2]] || b[1] - a[1]);
  }, [rows]);
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
      {rows && topProblems.length > 0 && (
        <div className="risk-bar">
          <strong className="risk-bar-title">
            {flaggedCount.toLocaleString()} of {rows.length.toLocaleString()} approved claims have a problem
          </strong>
          <div className="risk-chips">
            {topProblems.map(([code, count, severity]) => (
              <span key={code} className={`badge ${severity === "blocker" ? "bad" : severity === "warn" ? "warn" : "info"}`}>
                {count} · {session.screening_rules?.[code]?.title || code}
              </span>
            ))}
          </div>
          <span className="small-text muted">Click a claim's Risk badge for the detail.</span>
        </div>
      )}
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
          canReview={session.can_review_flags}
          onFlagsChanged={reload}
          rules={session.screening_rules}
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
