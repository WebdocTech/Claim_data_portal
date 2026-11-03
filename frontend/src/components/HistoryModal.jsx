import { useEffect, useState } from "react";
import { api } from "../api.js";
import StatusBadge from "./StatusBadge.jsx";
import { fmtDateTime } from "../format.js";

export function HistoryList({ rows }) {
  if (!rows.length) return <p className="muted">No status changes yet.</p>;
  return (
    <table className="grid compact">
      <thead>
        <tr>
          <th>When</th>
          <th>From</th>
          <th>To</th>
          <th>Changed by</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((h) => (
          <tr key={h.id}>
            <td className="nowrap">{fmtDateTime(h.changed_at)}</td>
            <td>
              <StatusBadge status={h.old_status} />
            </td>
            <td>
              <StatusBadge status={h.new_status} />
            </td>
            <td>
              {h.changed_by_username} <span className="role">{h.changed_by_role}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function HistoryModal({ claim, onClose }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api.history(claim.claim_id).then(setRows).catch((e) => setError(e.message));
  }, [claim.claim_id]);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>Status history: {claim.claim_no}</h3>
          <button className="link" onClick={onClose}>
            Close
          </button>
        </div>
        {error && <p className="error">{error}</p>}
        {!rows && !error && <p className="muted">Loading…</p>}
        {rows && <HistoryList rows={rows} />}
      </div>
    </div>
  );
}
