const TONE = {
  "Send Payment": "info",
  "Send Payment CSCP 24hr": "info",
  "Send Payment - Discrepant": "warn",
  Uploaded: "info",
  Discrepant: "warn",
  "Payment Error": "bad",
  Paid: "good",
  Approved: "good",
  Rejected: "bad",
};

export default function StatusBadge({ status }) {
  if (!status) return <span className="muted">—</span>;
  return (
    <span className={`badge ${TONE[status] || "neutral"}`}>
      {status === "Paid" && "🔒 "}
      {status}
    </span>
  );
}
