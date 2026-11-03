# Claims & Payments Portal

FastAPI backend + React/Vite frontend.

- **Source (read-only, live):** SQL Server `Digital_Insurance` (`InitialClaimRasings`, `ProcessedClaims`, remarks tables).
  Never written to.
- **Portal data:** Postgres `claim_portal` on 192.168.100.237 — `users`, `payment_status` (current status per claim,
  no row = "Send Payment" for an approved claim; non-approved claims have no payment status), `payment_status_history` (every change: from, to, who, role, when).

- **Replica:** schema `digital_insurance` in `claim_portal` holds full copies of the source tables the portal reads
  (`InitialClaimRasings`, `ProcessedClaims`, `ClaimIntimationRemarks`, `ClaimProcessRemarks`, `PaymentDetail`,
  `ClaimAuditLogs`, `ClaimBenefitTransactions`), same table and column names. Refreshed every 30 minutes while the
  backend runs (`REPLICA_SYNC_MINUTES` in `backend/.env`) or on demand with `python manage.py sync`. Each run is
  logged in `digital_insurance.sync_runs`.

DB credentials are read from `easypaisa/.env` (`WEBDOC_SQL2_*`, `*_EP`). Portal settings (`JWT_SECRET`,
`PORTAL_DB_NAME`) are in `backend/.env`.

## Roles (enforced in `backend/app/statuses.py` and route dependencies)

| Role    | Sees                                   | Can set status                                   |
|---------|----------------------------------------|--------------------------------------------------|
| Finance | Approved claims (2 tabs + Excel export) | Any of the 7 statuses, approved claims only       |
| Claims  | All claims                             | Only `Send Payment CSCP 24hr`, approved claims only |
| Admin   | Record search + Finance & Claims views | Nothing (view-only)                              |

Once a claim is `Paid` its status is locked for everyone.

## Run

```powershell
# backend (first time: pip install -r requirements.txt; python manage.py init-db)
cd claims\backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# frontend dev server (http://localhost:5173, proxies /api to :8000)
cd claims\frontend
npm install
npm run dev
```

For a single-port deployment run `npm run build`; the backend then serves `frontend/dist` on port 8000.

## Users

```powershell
cd claims\backend
python manage.py create-user <username> <admin|finance|claims> "Full Name" [password]
python manage.py reset-password <username> [password]
```
