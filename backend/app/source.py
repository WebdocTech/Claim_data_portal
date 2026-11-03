"""Live reads from Digital_Insurance. All queries are SELECT-only."""
import re

from .db import source_query

# One row per claim. ProcessedClaims has at most one row per claim in practice,
# but OUTER APPLY TOP 1 guarantees it so a stray duplicate can't double a claim.
CLAIM_SELECT = """
SELECT
    i.Id                                    AS claim_id,
    i.ClaimNumber                           AS claim_no,
    i.CustomerName                          AS name,
    i.MobileNumber                          AS mobile,
    i.AccountNumber                         AS account_number,
    i.BankName                              AS bank_name,
    i.BankCode                              AS bank_code,
    i.NetworkName                           AS network,
    LTRIM(RTRIM(i.ServiceName))             AS service,
    i.Category                              AS category,
    UPPER(ISNULL(i.Source, 'DIGI'))         AS company,
    i.Status                                AS claim_status,
    CONVERT(varchar(19), i.ClaimDateTime, 120)    AS claim_date,
    CONVERT(varchar(19), i.ApprovedDateTime, 120) AS approved_date,
    -- amounts are nvarchar at source; junk text becomes NULL
    TRY_CAST(p.InitialAmount AS decimal(18,2)) AS claim_amount,
    TRY_CAST(p.ExpenseAmount AS decimal(18,2)) AS suggest_amount,
    TRY_CAST(p.PayableAmount AS decimal(18,2)) AS approved_amount
FROM dbo.InitialClaimRasings i
OUTER APPLY (
    SELECT TOP 1 * FROM dbo.ProcessedClaims pc
    WHERE pc.InitialClaimRasingId = i.Id ORDER BY pc.id DESC
) p
"""


def list_claims(approved_only=False):
    where = "WHERE i.Status = 'Approved'" if approved_only else ""
    return source_query(f"{CLAIM_SELECT} {where} ORDER BY i.Id DESC")


def get_claim(claim_id):
    rows = source_query(f"{CLAIM_SELECT} WHERE i.Id = ?", claim_id)
    return rows[0] if rows else None


def get_claims(claim_ids):
    """{claim_id: claim} for the given ids (chunked to stay under SQL Server's parameter limit)."""
    ids = list(dict.fromkeys(int(i) for i in claim_ids))
    out = {}
    for n in range(0, len(ids), 1000):
        chunk = ids[n:n + 1000]
        for row in source_query(f"{CLAIM_SELECT} WHERE i.Id IN ({','.join('?' * len(chunk))})", *chunk):
            out[row["claim_id"]] = row
    return out


def _search_filter(term):
    """Turn an MSISDN / claim number / account number into a WHERE clause."""
    term = term.strip()
    claim = re.fullmatch(r"(?i)(?:CL-?)?(\d{1,7})", term)
    digits = re.sub(r"\D", "", term)
    clauses, params = [], []
    if claim:
        clauses.append("i.ClaimNumber = ?")
        params.append(f"CL-{int(claim.group(1))}")
    if len(digits) >= 10:
        # 923001234567, 03001234567 and 3001234567 all end in the same 10 digits
        last10 = digits[-10:]
        clauses.append("RIGHT(i.MobileNumber, 10) = ? OR RIGHT(i.WhatsappNumber, 10) = ? OR RIGHT(i.AccountNumber, 10) = ?")
        params += [last10, last10, last10]
    if not clauses:
        clauses.append("i.AccountNumber = ?")
        params.append(term)
    return " OR ".join(f"({c})" for c in clauses), params


def search_full(term):
    """Everything the source holds for claims matching an MSISDN / claim no / account no."""
    where, params = _search_filter(term)
    claims = source_query(
        f"SELECT i.* FROM dbo.InitialClaimRasings i WHERE {where} ORDER BY i.Id DESC", *params
    )
    if not claims:
        return []
    ids = [c["Id"] for c in claims]
    marks = ",".join("?" * len(ids))

    def by_claim(sql, key="InitialClaimRasingId"):
        out = {}
        for row in source_query(sql.format(marks=marks), *ids):
            out.setdefault(row[key], []).append(row)
        return out

    processed = by_claim("SELECT * FROM dbo.ProcessedClaims WHERE InitialClaimRasingId IN ({marks}) ORDER BY id")
    intimation = by_claim(
        "SELECT * FROM dbo.ClaimIntimationRemarks WHERE InitialClaimRasingId IN ({marks}) ORDER BY DateTime"
    )
    process_remarks = by_claim(
        "SELECT * FROM dbo.ClaimProcessRemarks WHERE InitialClaimRasingId IN ({marks}) ORDER BY DateTime"
    )
    payments = by_claim("SELECT * FROM dbo.PaymentDetail WHERE InitialClaimRasingId IN ({marks}) ORDER BY Id")
    audit = by_claim("SELECT * FROM dbo.ClaimAuditLogs WHERE ClaimId IN ({marks}) ORDER BY DateTime", key="ClaimId")
    benefits = by_claim(
        "SELECT * FROM dbo.ClaimBenefitTransactions WHERE ClaimId IN ({marks}) ORDER BY CreatedAt", key="ClaimId"
    )

    return [
        {
            "claim": c,
            "processed": processed.get(c["Id"], []),
            "intimation_remarks": intimation.get(c["Id"], []),
            "process_remarks": process_remarks.get(c["Id"], []),
            "source_payment_details": payments.get(c["Id"], []),
            "audit_logs": audit.get(c["Id"], []),
            "benefit_transactions": benefits.get(c["Id"], []),
        }
        for c in claims
    ]
