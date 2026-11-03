import StatusBadge from "./StatusBadge.jsx";

export const CSCP = "Send Payment CSCP 24hr";

// Whether this role can change this claim's status at all (mirrors the backend rules).
export function canChange(claim, role, allowed) {
  if (role === "admin" || !allowed.length || claim.is_paid || claim.claim_status !== "Approved") return false;
  return role !== "claims" || claim.payment_status !== CSCP;
}

// Status cell. Picking a status only drafts it (`draft`); nothing is saved until the
// page's Save changes button. Finance gets a dropdown, Claims a single CSCP 24hr button,
// Admin only the badge. The backend enforces the same rules on save.
export default function StatusControl({ claim, role, allowed, draft, bulkCount, onPick }) {
  const current = claim.payment_status;
  if (!canChange(claim, role, allowed)) return <StatusBadge status={current} />;

  const was = draft && <div className="small-text muted">was: {current}</div>;

  if (role === "claims") {
    return (
      <div className="status-cell">
        <StatusBadge status={draft || current} />
        {draft ? (
          <>
            {was}
            <button className="link small-text" onClick={() => onPick(current)}>
              Undo
            </button>
          </>
        ) : (
          <button className="small" onClick={() => onPick(CSCP)}>
            {bulkCount > 1 ? `Set CSCP 24hr (${bulkCount})` : "Set CSCP 24hr"}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="status-cell">
      <select className={draft ? "changed" : ""} value={draft || current} onChange={(e) => onPick(e.target.value)}>
        {allowed.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      {was}
    </div>
  );
}
