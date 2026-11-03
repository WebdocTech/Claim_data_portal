"""Payment-status rules. These are the only place status changes are allowed or refused."""
from fastapi import HTTPException

from . import source
from .db import portal_cursor

SEND_PAYMENT = "Send Payment"
PAID = "Paid"
CSCP_24HR = "Send Payment CSCP 24hr"

# An approved claim with no stored status is "Send Payment"; other claims have no payment status.
DEFAULT_STATUS = SEND_PAYMENT

STATUSES = [
    SEND_PAYMENT,
    "Discrepant",
    "Uploaded",
    PAID,
    "Payment Error",
    "Send Payment - Discrepant",
    CSCP_24HR,
]

# Which target statuses each role may set. Admin is view-only.
ALLOWED_TARGETS = {
    "finance": set(STATUSES),
    "claims": {CSCP_24HR},
    "admin": set(),
}


def effective_status(claim_status, stored):
    if stored:
        return stored
    return DEFAULT_STATUS if claim_status == "Approved" else None


def current_statuses():
    """{claim_id: {status, updated_at, updated_by_username}} for every claim with a status."""
    with portal_cursor() as cur:
        cur.execute(
            # updated_by is NULL for statuses imported from the Finance workbook; shown as blank.
            """SELECT s.claim_id, s.status, s.updated_at, u.username AS updated_by
               FROM payment_status s LEFT JOIN users u ON u.id = s.updated_by"""
        )
        return {r["claim_id"]: r for r in cur.fetchall()}


def history(claim_id):
    with portal_cursor() as cur:
        cur.execute(
            # Import rows (changed_by NULL) stay in the table but are not shown in the portal.
            """SELECT id, old_status, new_status, changed_by_username, changed_by_role, changed_at
               FROM payment_status_history WHERE claim_id = %s AND changed_by IS NOT NULL
               ORDER BY changed_at DESC, id DESC""",
            (claim_id,),
        )
        return cur.fetchall()


def _check_target(new_status, role):
    if new_status not in STATUSES:
        raise HTTPException(400, f"Unknown status '{new_status}'")
    if new_status not in ALLOWED_TARGETS[role]:
        raise HTTPException(403, f"The {role} role cannot set status '{new_status}'")


def _apply(cur, claim_id, claim, new_status, user):
    """Change one claim's status inside an open transaction. Returns (http_code, message)."""
    if claim is None:
        return 404, "Claim not found in source database"
    # Payment statuses only apply to approved claims (the ones Finance sees), for every role.
    if claim["claim_status"] != "Approved":
        return 403, "Payment status can only be set on approved claims"

    # Serialise concurrent changes to the same claim, including its first status.
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (claim_id,))
    cur.execute("SELECT status FROM payment_status WHERE claim_id = %s", (claim_id,))
    row = cur.fetchone()
    old_status = row["status"] if row else DEFAULT_STATUS

    if old_status == PAID:
        return 409, "This claim is Paid; its status is locked"
    if old_status == new_status:
        return 409, f"Status is already '{new_status}'"

    cur.execute(
        """INSERT INTO payment_status (claim_id, claim_no, status, updated_by, updated_at)
           VALUES (%s, %s, %s, %s, now())
           ON CONFLICT (claim_id) DO UPDATE
           SET status = EXCLUDED.status, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (claim_id, claim["claim_no"], new_status, user["id"]),
    )
    cur.execute(
        """INSERT INTO payment_status_history
           (claim_id, claim_no, old_status, new_status, changed_by, changed_by_username, changed_by_role)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (claim_id, claim["claim_no"], old_status, new_status, user["id"], user["username"], user["role"]),
    )
    return 200, old_status


def change_status(claim_id, new_status, user):
    _check_target(new_status, user["role"])
    claim = source.get_claim(claim_id)
    with portal_cursor() as cur:
        code, message = _apply(cur, claim_id, claim, new_status, user)
    if code != 200:
        raise HTTPException(code, message)
    return {"claim_id": claim_id, "old_status": message, "status": new_status}


def change_statuses(claim_ids, new_status, user):
    """Apply one status to many claims with the same per-claim rules. A claim that can't be
    changed (Paid, not approved, already that status) is reported and the rest still apply."""
    _check_target(new_status, user["role"])
    claims = source.get_claims(claim_ids)
    results = []
    with portal_cursor() as cur:
        # Sorted so two overlapping bulk changes take the per-claim locks in the same order.
        for cid in sorted(set(claim_ids)):
            code, message = _apply(cur, cid, claims.get(cid), new_status, user)
            results.append({"claim_id": cid, "ok": code == 200, "error": None if code == 200 else message})
    return {"status": new_status, "results": results}
