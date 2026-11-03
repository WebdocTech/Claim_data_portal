import { useState } from "react";
import { api } from "../api.js";
import StatusBadge from "../components/StatusBadge.jsx";
import { HistoryList } from "../components/HistoryModal.jsx";

// Splits InitialClaimRasings columns into readable groups; anything not listed lands in "Other".
const CLAIM_GROUPS = [
  ["Claim", ["ClaimNumber", "Id", "Status", "ClaimType", "Category", "ServiceName", "Source", "AdditionalNotes"]],
  ["Customer & payout account", ["CustomerName", "ExcelName","MobileNumber", "WhatsappNumber", "CustomerId", "ProfileId", "SubscriptionId", "AccountNumber", "BankName", "BankCode", "NetworkName"]],
  ["Timeline", ["ClaimDateTime", "IncompleteDateTime", "CompleteDateTime", "ProcessingDateTime", "ApprovedDateTime"]],
  ["Documents & checks", ["MissingDocumentStatus", "MissingDocumentNotes", "DocumentComplete", "PolicyActivation", "Verification", "CNICFront", "CNICBack", "AdmissionSlip", "DischargeSlip", "Bills", "TestSlip", "PrescriptionSlip"]],
  ["Call back", ["CallBackStatus", "CallBackRemarks"]],
];
const HIDDEN = new Set(["CategoryId"]);

const label = (k) => (k === "ExcelName" ? "Name" :k.replace(/([a-z])([A-Z])/g, "$1 $2"));
const show = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));

function KeyValues({ obj, keys }) {
  return (
    <dl className="kv">
      {keys.map((k) => (
        <div key={k}>
          <dt>{label(k)}</dt>
          <dd>{show(obj[k])}</dd>
        </div>
      ))}
    </dl>
  );
}

function RecordList({ title, rows }) {
  if (!rows.length) return null;
  return (
    <div className="block">
      <h4>{title}</h4>
      {rows.map((r, i) => (
        <KeyValues key={i} obj={r} keys={Object.keys(r).filter((k) => !/^(id|Id|InitialClaimRasingId|ClaimId)$/.test(k))} />
      ))}
    </div>
  );
}

function ClaimCard({ result }) {
  const c = { ...result.claim, ExcelName: result.excel_name };
  const grouped = new Set(CLAIM_GROUPS.flatMap(([, keys]) => keys));
  const other = Object.keys(c).filter((k) => !grouped.has(k) && !HIDDEN.has(k));
  return (
    <article className="card claim-card">
      <header className="claim-head">
        <h3>{c.ClaimNumber}</h3>
        {(c.CustomerName || result.excel_name) && <span>{c.CustomerName || result.excel_name}</span>}
        <StatusBadge status={c.Status} />
        <span className="muted">Payment:</span>
        <StatusBadge status={result.payment_status} />
      </header>
      {CLAIM_GROUPS.map(([title, keys]) => (
        <div className="block" key={title}>
          <h4>{title}</h4>
          <KeyValues obj={c} keys={keys.filter((k) => k in c)} />
        </div>
      ))}
      {other.length > 0 && (
        <div className="block">
          <h4>Other</h4>
          <KeyValues obj={c} keys={other} />
        </div>
      )}
      <RecordList title="Processing & approval" rows={result.processed} />
      <RecordList title="Intimation remarks" rows={result.intimation_remarks} />
      <RecordList title="Process remarks" rows={result.process_remarks} />
      <RecordList title="Payment details (source)" rows={result.source_payment_details} />
      <RecordList title="Benefit transactions" rows={result.benefit_transactions} />
      <RecordList title="Audit log (source)" rows={result.audit_logs} />
      <div className="block">
        <h4>Payment status history</h4>
        <HistoryList rows={result.status_history} />
      </div>
    </article>
  );
}

export default function AdminSearch() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState(null);
  const [searched, setSearched] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function search(e) {
    e.preventDefault();
    const term = q.trim();
    if (!term) return;
    setBusy(true);
    setError("");
    try {
      setResults(await api.adminSearch(term));
      setSearched(term);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <div className="page-head">
        <h2>Record search</h2>
      </div>
      <form className="toolbar" onSubmit={search}>
        <input
          className="search wide"
          placeholder="MSISDN (03xx / 923xx), claim number (CL-1234) or account number"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          required
          autoFocus
        />
        <button className="primary" disabled={busy}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {error && <p className="error">{error}</p>}
      {results && (
        <p className="muted">
          {results.length} claim{results.length === 1 ? "" : "s"} found for “{searched}”
        </p>
      )}
      {results?.map((r) => (
        <ClaimCard key={r.claim.Id} result={r} />
      ))}
    </section>
  );
}
