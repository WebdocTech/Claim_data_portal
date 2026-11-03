import csv
import io
import logging
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, replica, source, statuses
from .auth import authenticate, current_user, issue_token, require_roles
from .db import portal_cursor

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app):
    # Keep claim_portal.digital_insurance.* in step with the source.
    if config.REPLICA_SYNC_MINUTES > 0:
        replica.start_scheduler(config.REPLICA_SYNC_MINUTES)
    yield


app = FastAPI(title="Claims & Payments Portal", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Finance tabs, mirroring the "Customer Details" and "Payment Details" sheets of
# claims_Payments.xlsx. Excel columns with no source field (CNIC, Account Type,
# Date of Joining, Description, Default, Partner, Month, Batch, Transaction id,
# Paid Date) are left out. "paid" says which claims a tab holds: unpaid work, or Paid only.
FINANCE_VIEWS = {
    "customer": {
        "title": "Customer Details",
        "paid": False,
        "columns": [
            ("claim_no", "Claim No"),
            ("name", "Name"),
            ("mobile", "Mobile Number"),
            ("account_number", "Account Number"),
            ("bank_code", "Bank Code"),
            ("bank_name", "Bank Name"),
            ("category", "Category"),
            ("service", "Service"),
            ("approved_date", "Approved Date"),
            ("payment_status", "Status"),
            ("company", "Company"),
        ],
    },
    "payment": {
        "title": "Payment Details",
        "paid": False,
        "columns": [
            ("claim_no", "Claim No"),
            ("name", "Name"),
            ("mobile", "Mobile Number"),
            ("bank_code", "Bank Code"),
            ("account_number", "Account Number"),
            ("approved_amount", "Net Payable"),
            ("approved_date", "Approved Date"),
            ("payment_status", "Status"),
            ("company", "Company"),
            ("service", "Service"),
        ],
    },
    "paid": {
        "title": "Paid",
        "paid": True,
        "columns": [
            ("claim_no", "Claim No"),
            ("name", "Name"),
            ("mobile", "Mobile Number"),
            ("account_number", "Account Number"),
            ("bank_code", "Bank Code"),
            ("bank_name", "Bank Name"),
            ("approved_amount", "Net Payable"),
            ("approved_date", "Approved Date"),
            ("service", "Service"),
            ("company", "Company"),
            ("status_updated_by", "Marked Paid By"),
            ("payment_status", "Status"),
        ],
    },
}


class LoginBody(BaseModel):
    username: str
    password: str


class StatusBody(BaseModel):
    status: str


class BulkStatusBody(BaseModel):
    claim_ids: list[int] = Field(min_length=1, max_length=5000)
    status: str


def excel_names():
    with portal_cursor() as cur:
        cur.execute("SELECT claim_id, name FROM excel_matches WHERE name IS NOT NULL")
        return {r["claim_id"]: r["name"] for r in cur.fetchall()}


def with_status(claims):
    current = statuses.current_statuses()
    names = excel_names()
    for c in claims:
        # Source name first; most claims have none, so fall back to the Finance workbook.
        c["name"] = (c["name"] or "").strip() or names.get(c["claim_id"])
        s = current.get(c["claim_id"])
        c["payment_status"] = statuses.effective_status(c["claim_status"], s and s["status"])
        c["status_updated_by"] = s["updated_by"] if s else None
        c["status_updated_at"] = s["updated_at"] if s else None
        c["is_paid"] = c["payment_status"] == statuses.PAID
    return claims


@app.post("/api/auth/login")
def login(body: LoginBody):
    user = authenticate(body.username.strip(), body.password)
    if not user:
        raise HTTPException(401, "Invalid username or password")
    return {"token": issue_token(user), "user": user}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {
        "user": user,
        "statuses": statuses.STATUSES,
        "allowed_statuses": [s for s in statuses.STATUSES if s in statuses.ALLOWED_TARGETS[user["role"]]],
        "finance_views": FINANCE_VIEWS,
    }


@app.get("/api/finance/claims")
def finance_claims(user=Depends(require_roles("finance", "admin"))):
    return with_status(source.list_claims(approved_only=True))


@app.get("/api/finance/export")
def finance_export(
    view: str = Query("payment", pattern="^(customer|payment|paid)$"),
    status: str | None = None,
    user=Depends(require_roles("finance", "admin")),
):
    spec = FINANCE_VIEWS[view]
    rows = [r for r in with_status(source.list_claims(approved_only=True)) if r["is_paid"] == spec["paid"]]
    if status:
        rows = [r for r in rows if r["payment_status"] == status]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([label for _, label in spec["columns"]])
    for r in rows:
        writer.writerow(["" if r.get(key) is None else r.get(key) for key, _ in spec["columns"]])
    filename = f"{spec['title'].replace(' ', '_')}_{date.today():%Y-%m-%d}.csv"
    # BOM so Excel opens the file as UTF-8.
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/claims")
def all_claims(user=Depends(require_roles("claims", "admin"))):
    return with_status(source.list_claims())


@app.post("/api/claims/status/bulk")
def set_status_bulk(body: BulkStatusBody, user=Depends(require_roles("finance", "claims"))):
    return statuses.change_statuses(body.claim_ids, body.status, user)


@app.post("/api/claims/{claim_id}/status")
def set_status(claim_id: int, body: StatusBody, user=Depends(require_roles("finance", "claims"))):
    return statuses.change_status(claim_id, body.status, user)


@app.get("/api/claims/{claim_id}/history")
def status_history(claim_id: int, user=Depends(current_user)):
    return statuses.history(claim_id)


@app.get("/api/admin/search")
def admin_search(q: str = Query(..., min_length=1), user=Depends(require_roles("admin"))):
    results = source.search_full(q)
    current = statuses.current_statuses()
    names = excel_names()
    for r in results:
        cid = r["claim"]["Id"]
        r["excel_name"] = names.get(cid)
        s = current.get(cid)
        r["payment_status"] = statuses.effective_status(r["claim"]["Status"], s and s["status"])
        r["status_history"] = statuses.history(cid)
    return results


# Serve the built frontend (frontend/dist) from the same port when present.
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        return FileResponse(DIST / "index.html")
