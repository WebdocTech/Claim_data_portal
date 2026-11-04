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

## Claim screening

Approved claims are screened for duplicates and false claims before Finance pays them.
Flags appear in the **Risk** column of every claims view and in the CSV exports; opening one
shows what was found, the claims it conflicts with, and lets Finance acknowledge it with a
reason (append-only, in `claim_flag_reviews`). Screening never blocks a payment on its own —
it informs the reviewer.

Rules (`backend/app/screening.py`, one set-based query each over the replica):

| Rule | Flags |
|------|-------|
| `DUP_SERVICE_CLAIM` | The same subscription was already paid for this service in this entitlement period |
| `DUP_SERVICE_CROSS_SUB` | Same number already paid for this service in this period under another subscription |
| `DUP_IMEI` | A well-formed IMEI (length + Luhn checksum) on more than one approved claim |
| `SHARED_PAYOUT_ACCOUNT` | One payout account paid for several numbers whose claimant names disagree |
| `SHARED_SUBSCRIPTION` | One subscription raising approved claims from several mobile numbers |
| `PLACEHOLDER_IDENTIFIER` | Payout account or IMEI is filler rather than a real value |
| `AMOUNT_OVER_COVERAGE` | Approved amount above the product's coverage |
| `BLACKLIST_MATCH` | Claimant matches `TestClaimBlacklists` by number or name |
| `MISSING_CLAIMANT_NAME` | Approved payout with no usable claimant name |

Screening reads the `digital_insurance` replica inside `claim_portal`, so the source SQL Server
is never queried, and it runs automatically after each replica sync. Run it by hand with
`python manage.py screen`.

**Entitlement periods.** A duplicate means *the same subscription was PAID more than
once for one service inside one entitlement period*. Three things follow from that:

- Periods are **calendar buckets, not rolling days**. With `month`, 18 September and
  8 October are 20 days apart but fall in two different months, so they are two
  entitlements and nothing is reported.
- The earlier claim must have been **Paid**. A claim that was rejected and then
  re-submitted and approved is one entitlement settled once, so rejected claims are
  never counterparts.
- Billing frequency is never used as the period - a "Daily" plan is charged daily, it
  does not entitle a claim per day - so periods start unset. Unset means a repeat is
  reported with the day gap and never as a blocker.

```powershell
python manage.py windows                      # the period per category
python manage.py set-window Health month       # one paid Health claim per calendar month
python manage.py set-window Mobile year        # one per calendar year
python manage.py set-window Health none        # back to reporting without judging
python manage.py screen                        # apply to existing claims
```

Valid periods: `day`, `week`, `month`, `quarter`, `half`, `year`.

**Product catalogue.** Plans and coverage come from `claim_service_plans`, imported from
the MySQL catalogue (`digital_insurance_products`), then the products workbook, then the
billing period spelled out in the service name:

```powershell
python import_catalogue.py [DIGIINsurance_Products.xlsx]   # --dry-run to preview
```

The import prints the services it could not find a plan for. Those keep a warning-level
duplicate rule rather than being judged against a guessed period. A service plan edited by
hand (`source = 'manual'`) is never overwritten by a re-import, and `claim_window_days` is
preserved on every row.

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
