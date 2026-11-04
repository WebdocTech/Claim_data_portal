import { useEffect, useState } from "react";
import { api } from "../api.js";
import RiskBadge from "./RiskBadge.jsx";
import { fmtDateTime } from "../format.js";

// Detail keys are rule-specific, so they are labelled here rather than in the rules.
// Anything not listed still shows, with its key spaced out.
const LABELS = {
  service: "Service",
  entitlement_period: "Allowed",
  entitlement_period_source: "Rule set at",
  period_of_this_claim: "This claim falls in",
  repeats_in_this_period: "Other approved claims in that period",
  claims_in_this_period: "Claims in that period",
  this_subscription: "This subscription",
  other_subscriptions: "Earlier subscription",
  already_paid_in_this_period: "Already paid in that period",
  already_paid_claim_nos: "Already paid",
  days_since_paid_claim: "Days since the paid claim",
  other_approved_claims: "Other approved claims (all time)",
  nearest_gap_days: "Nearest repeat",
  other_claim_nos: "Other claims",
  billing_plan: "Billing plan",
  billing_period_days: "Billing period",
  imei: "IMEI",
  claims: "Claims",
  approved_claims: "Approved claims",
  account: "Payout account",
  distinct_mobiles: "Different mobile numbers",
  mobile_numbers: "Mobile numbers",
  claimant_names: "Names on these claims",
  names_agree: "Names are the same person",
  subscription_id: "Subscription",
  account_is_placeholder: "Account is filler",
  imei_is_invalid: "IMEI is invalid",
  payable_amount: "Approved amount",
  coverage_amount: "Product coverage",
  excess_amount: "Over coverage by",
  matched_on: "Matched on",
  blacklist_names: "Blacklist entry",
  claimant_name: "Claimant name",
};

const DAY_KEYS = new Set(["nearest_gap_days", "billing_period_days", "days_since_paid_claim"]);

function showValue(key, value) {
  if (value === null || value === undefined || value === "") return null;
  if (Array.isArray(value)) return value.length ? value.join(", ") : null;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (key === "entitlement_period") return `once per ${value}`;
  if (DAY_KEYS.has(key)) return `${value} days`;
  return String(value);
}

function FlagCard({ flag, canReview, onReviewed, ruleTitle }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const entries = Object.entries(flag.detail || {})
    .map(([k, v]) => [k, showValue(k, v)])
    .filter(([, v]) => v !== null);

  async function submit(action) {
    if (reason.trim().length < 3) {
      setError("Give a short reason so the next reviewer knows why.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.reviewFlag(flag.claim_id, flag.rule_code, action, reason.trim());
      setReason("");
      setOpen(false);
      onReviewed();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`flag ${flag.acknowledged ? "flag-ack" : ""}`}>
      <div className="flag-head">
        <RiskBadge risk={flag.severity} label={ruleTitle} />
      </div>
      <p className="flag-summary">{flag.summary}</p>

      {entries.length > 0 && (
        <dl className="kv flag-kv">
          {entries.map(([k, v]) => (
            <div key={k}>
              <dt>{LABELS[k] || k.replace(/_/g, " ")}</dt>
              <dd>{v}</dd>
            </div>
          ))}
        </dl>
      )}

      {flag.acknowledged ? (
        <p className="small-text muted">
          Acknowledged by {flag.reviewed_by_username} on {fmtDateTime(flag.reviewed_at)} — “{flag.review_reason}”
        </p>
      ) : (
        <p className="small-text muted">Flagged since {fmtDateTime(flag.first_seen_at)}</p>
      )}

      {canReview && (
        <div className="flag-actions">
          {open ? (
            <>
              <input
                className="search"
                placeholder="Reason for this decision"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                autoFocus
              />
              <button
                className="primary small"
                disabled={busy}
                onClick={() => submit(flag.acknowledged ? "reopened" : "acknowledged")}
              >
                {busy ? "Saving…" : flag.acknowledged ? "Re-open" : "Acknowledge"}
              </button>
              <button className="small" disabled={busy} onClick={() => setOpen(false)}>
                Cancel
              </button>
            </>
          ) : (
            <button className="small" onClick={() => setOpen(true)}>
              {flag.acknowledged ? "Re-open this flag" : "Acknowledge"}
            </button>
          )}
          {error && <span className="error small-text">{error}</span>}
        </div>
      )}
    </div>
  );
}

export default function FlagsModal({ claim, canReview, onClose, onChanged, rules = {} }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  function load() {
    api
      .flags(claim.claim_id)
      .then(setData)
      .catch((e) => setError(e.message));
  }

  useEffect(load, [claim.claim_id]);

  function reviewed() {
    load();
    onChanged?.();
  }

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal wide-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h3>Checks on {claim.claim_no}</h3>
          <button className="link" onClick={onClose}>
            Close
          </button>
        </div>

        {error && <p className="error">{error}</p>}
        {!data && !error && <p className="muted">Loading…</p>}

        {data && data.flags.length === 0 && <p className="muted">No checks flagged this claim.</p>}

        {data?.flags.map((f) => (
          <FlagCard
            key={f.rule_code}
            flag={{ ...f, claim_id: data.claim_id }}
            canReview={canReview}
            onReviewed={reviewed}
            ruleTitle={rules[f.rule_code]?.title || f.rule_code}
          />
        ))}

        {data?.reviews.length > 0 && (
          <div className="block">
            <h4>Review trail</h4>
            <table className="grid compact">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Problem</th>
                  <th>Action</th>
                  <th>Reason</th>
                  <th>By</th>
                </tr>
              </thead>
              <tbody>
                {data.reviews.map((r, i) => (
                  <tr key={i}>
                    <td className="nowrap">{fmtDateTime(r.reviewed_at)}</td>
                    <td>{rules[r.rule_code]?.title || r.rule_code}</td>
                    <td>{r.action}</td>
                    <td>{r.reason}</td>
                    <td>
                      {r.reviewed_by_username} <span className="role">{r.reviewed_by_role}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
