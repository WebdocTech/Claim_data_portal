import { useCallback, useMemo, useState } from "react";
import { api } from "../api.js";
import ClaimsGrid from "../components/ClaimsGrid.jsx";
import SaveBar from "../components/SaveBar.jsx";
import useClaims from "../useClaims.js";

const COLUMNS = [
  ["claim_no", "Claim No"],
  ["mobile", "Mobile Number"],
  ["name", "Name"],
  ["account_number", "Account Number"],
  ["bank_name", "Bank"],
  ["service", "Service"],
  ["category", "Category"],
  ["company", "Company"],
  ["claim_status", "Claim Status"],
  ["claim_date", "Claim Date"],
  ["approved_amount", "Approved Amount"],
  ["payment_status", "Payment Status"],
];

export default function ClaimsPage({ session }) {
  const claims = useClaims(api.allClaims);
  const { rows, error, reload, pending, setPending } = claims;
  const [claimStatus, setClaimStatus] = useState("");
  const readOnly = session.user.role === "admin";

  const claimStatuses = useMemo(() => [...new Set((rows || []).map((r) => r.claim_status))].sort(), [rows]);
  const extraFilters = useCallback((r) => !claimStatus || r.claim_status === claimStatus, [claimStatus]);
  const paidCount = useMemo(() => (rows || []).filter((r) => r.is_paid).length, [rows]);

  return (
    <section>
      <div className="page-head">
        <h2>All claims</h2>
        {readOnly && <span className="badge neutral">Read-only</span>}
        {rows && (
          <span className="muted">
            {rows.length.toLocaleString()} claims · {paidCount.toLocaleString()} paid
          </span>
        )}
        <button
          className="link"
          onClick={() => (!pending.size || window.confirm(`Discard ${pending.size} unsaved changes and reload?`)) && reload()}
        >
          Refresh
        </button>
      </div>
      <SaveBar {...claims} />
      {error && <p className="error">{error}</p>}
      {!rows && !error && <p className="muted">Loading claims from Digital_Insurance…</p>}
      {rows && (
        <ClaimsGrid
          rows={rows}
          columns={COLUMNS}
          role={session.user.role}
          allowed={session.allowed_statuses}
          statuses={session.statuses}
          pending={pending}
          setPending={setPending}
          extraFilters={extraFilters}
          toolbar={() => (
            <select value={claimStatus} onChange={(e) => setClaimStatus(e.target.value)}>
              <option value="">All claim statuses</option>
              {claimStatuses.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          )}
        />
      )}
    </section>
  );
}
