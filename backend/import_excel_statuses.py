"""Seed payment statuses for approved claims from the Finance workbook.

    python import_excel_statuses.py <claims_Payments.xlsx>            dry run: report only
    python import_excel_statuses.py <claims_Payments.xlsx> --apply    write statuses

Only the 'Customer Details' sheet is used. It has no claim number or amount, so rows are
matched to approved claims by the last 10 digits of mobile / account / WhatsApp number:
  - one claim, one or more rows for the number   -> the latest row (lowest in the sheet)
  - several claims for the same number           -> claims (by approved date) paired with
                                                    rows (sheet order); the newest claim
                                                    takes the latest row; claims left over
                                                    when there are fewer rows stay unmatched
  - no row for the number                        -> no match, claim stays at the default
Claims that already have a portal status are never touched, so re-running is safe.
Every match (row + customer name) is saved to excel_matches, refreshed on each --apply.
A match report is written next to the workbook either way.
"""
import re
import sys
from pathlib import Path

import openpyxl

from app import source
from app.db import portal_cursor
from app.statuses import STATUSES

IMPORT_USER = "excel-import"

# Workbook spellings -> portal statuses
STATUS_MAP = {
    "Discripent": "Discrepant",
    "Send Payment - Discripent": "Send Payment - Discrepant",
    "Send Payment - CSCP - 24hr": "Send Payment CSCP 24hr",
}


def last10(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else None


def read_sheet(wb, name):
    rows = wb[name].iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else None for h in next(rows)]
    return [({h: v for h, v in zip(header, r) if h}, n) for n, r in enumerate(rows, start=2)]


def load_workbook_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    for rec, sheet_row in read_sheet(wb, "Customer Details"):
        if not rec.get("Mobile Number") and not rec.get("Account Number"):
            continue
        status = str(rec.get("Status") or "").strip()
        out.append({
            "row": sheet_row,
            "keys": {k for k in (last10(rec.get("Mobile Number")), last10(rec.get("Account Number"))) if k},
            "status": STATUS_MAP.get(status, status),
            "raw_status": status,
            "name": rec.get("Name"),
            "service": rec.get("Service"),
            "month": rec.get("Month"),
        })
    return out


def match(claims, xl_rows):
    by_key = {}
    for idx, r in enumerate(xl_rows):
        for k in r["keys"]:
            by_key.setdefault(k, set()).add(idx)
    whatsapp = {
        r["Id"]: r["WhatsappNumber"]
        for r in source.source_query("SELECT Id, WhatsappNumber FROM dbo.InitialClaimRasings WHERE Status = 'Approved'")
    }

    picks, cands = {}, {}
    for c in claims:
        keys = {last10(c["mobile"]), last10(c["account_number"]), last10(whatsapp.get(c["claim_id"]))} - {None}
        rows = set().union(*(by_key.get(k, set()) for k in keys))
        if rows:
            cands[c["claim_id"]] = rows
        else:
            picks[c["claim_id"]] = (None, "no Excel row")

    # Claims that share any Excel row belong to one customer: merge them (union-find over rows).
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for cid, rows in cands.items():
        for r in rows:
            parent[find(("row", r))] = find(("claim", cid))
    by_id = {c["claim_id"]: c for c in claims}
    groups = {}
    for cid, rows in cands.items():
        g = groups.setdefault(find(("claim", cid)), ([], set()))
        g[0].append(by_id[cid])
        g[1].update(rows)

    # Pair the oldest claim with the earliest row; the newest claim takes the latest row
    # (re-payments come later in the sheet).
    for group, row_set in groups.values():
        rows = sorted(row_set, key=lambda i: xl_rows[i]["row"])
        group.sort(key=lambda c: c["approved_date"] or "")
        if len(group) == 1:
            how = "single row for number" if len(rows) == 1 else f"latest of {len(rows)} rows for number"
            picks[group[0]["claim_id"]] = (rows[-1], how)
            continue
        if len(rows) < len(group):
            # Fewer rows than claims: the newest claims are the ones not in the sheet yet.
            for c in group[len(rows):]:
                picks[c["claim_id"]] = (None, f"{len(group)} claims / {len(rows)} rows for number, newer than sheet")
            group = group[: len(rows)]
        for n, c in enumerate(group):
            idx = rows[n] if n < len(group) - 1 else rows[-1]
            picks[c["claim_id"]] = (idx, f"{len(group)} claims / {len(rows)} rows for number, paired by date")

    return [
        (c, xl_rows[picks[c["claim_id"]][0]] if picks[c["claim_id"]][0] is not None else None, picks[c["claim_id"]][1])
        for c in claims
    ]


def write_report(path, results):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Match report"
    ws.append(["Claim No", "Mobile", "Service", "Approved Date", "Match", "Excel Row",
               "Excel Name", "Excel Service", "Excel Month", "Excel Status", "Portal Status", "Action"])
    for c, x, how, action in results:
        ws.append([c["claim_no"], c["mobile"], c["service"], c["approved_date"], how,
                   x and x["row"], x and x["name"], x and x["service"], x and str(x["month"] or ""),
                   x and x["raw_status"], x and x["status"], action])
    wb.save(path)


def main(path, apply):
    xl_rows = load_workbook_rows(path)
    claims = source.list_claims(approved_only=True)
    matched = match(claims, xl_rows)

    with portal_cursor() as cur:
        cur.execute("SELECT claim_id FROM payment_status")
        existing = {r["claim_id"] for r in cur.fetchall()}

    plan, report = [], []
    for c, x, how in matched:
        if x is None:
            action = "default Send Payment"
        elif x["status"] not in STATUSES:
            action = f"skipped: unknown status '{x['raw_status']}'"
        elif c["claim_id"] in existing:
            action = "skipped: already has a portal status"
        else:
            action = f"set {x['status']}"
            plan.append((c, x))
        report.append((c, x, how, action))

    report_path = Path(path).with_name("excel_status_import_report.xlsx")
    write_report(report_path, report)

    from collections import Counter
    print(f"{len(claims)} approved claims, {len(xl_rows)} Excel rows")
    for k, v in Counter(how.split(" rows")[0] if how.startswith("latest") else how for _, _, how in matched).most_common():
        print(f"  {v:4}  {k}")
    print("statuses to set:", dict(Counter(x["status"] for _, x in plan)))
    print("report:", report_path)

    if not apply:
        print("dry run; re-run with --apply to write")
        return

    with portal_cursor() as cur:
        for c, x in plan:
            cur.execute(
                """INSERT INTO payment_status (claim_id, claim_no, status, updated_by)
                   VALUES (%s, %s, %s, NULL) ON CONFLICT (claim_id) DO NOTHING""",
                (c["claim_id"], c["claim_no"], x["status"]),
            )
            if cur.rowcount:
                cur.execute(
                    """INSERT INTO payment_status_history
                       (claim_id, claim_no, old_status, new_status, changed_by, changed_by_username, changed_by_role, note)
                       VALUES (%s, %s, NULL, %s, NULL, %s, 'system', %s)""",
                    (c["claim_id"], c["claim_no"], x["status"], IMPORT_USER,
                     f"Imported from {Path(path).name}, Customer Details row {x['row']}"),
                )
        for c, x, _ in matched:
            if x is None:
                continue
            name = str(x["name"]).strip() if x["name"] is not None else None
            cur.execute(
                """INSERT INTO excel_matches (claim_id, claim_no, excel_file, excel_row, name, excel_status)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (claim_id) DO UPDATE SET excel_file = EXCLUDED.excel_file,
                       excel_row = EXCLUDED.excel_row, name = EXCLUDED.name,
                       excel_status = EXCLUDED.excel_status, matched_at = now()""",
                (c["claim_id"], c["claim_no"], Path(path).name, x["row"], name or None, x["raw_status"]),
            )
    print(f"applied {len(plan)} statuses; saved {sum(1 for _, x, _ in matched if x)} Excel matches")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], "--apply" in sys.argv[2:])
