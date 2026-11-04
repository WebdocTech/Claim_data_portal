// What a claim's checks found, named by the problem rather than by a severity word:
// Finance needs to read "Claimed twice", not "Blocker".
// `null` risk means nothing is open, shown as a quiet dash - a claim can also be
// unflagged because its product has no plan data.
const TONE = { blocker: "bad", warn: "warn", info: "info" };

export default function RiskBadge({ risk, label, count, title, onClick, acknowledgedOnly }) {
  if (!risk) {
    return acknowledgedOnly ? (
      <button className="link small-text" onClick={onClick} title="Every check on this claim has been acknowledged">
        reviewed
      </button>
    ) : (
      <span className="muted">—</span>
    );
  }

  const badge = (
    <span className={`badge ${TONE[risk] || "neutral"} ${risk === "blocker" ? "badge-loud" : ""}`}>
      {risk === "blocker" && "⚠ "}
      {label || risk}
      {count > 1 && <span className="badge-count">+{count - 1}</span>}
    </span>
  );

  return onClick ? (
    <button className="risk-button" onClick={onClick} title={title || "See what was found"}>
      {badge}
    </button>
  ) : (
    badge
  );
}
