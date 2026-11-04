"""Build claim_service_plans: one row per claim ServiceName, with its plan and coverage.

    python import_catalogue.py                      MySQL catalogue + name keywords
    python import_catalogue.py <products.xlsx>      also fall back to the workbook
    python import_catalogue.py --dry-run            report without writing

Resolution order per service name, best source first:
  1. mysql    digital_insurance_products.products (+ product_policy_rules) - live and authoritative
  2. excel    the DIGIINsurance_Products workbook, when a path is given
  3. keyword  the billing period spelled out in the service name itself
              ('Jazz Mobile Insurance-Daily-Gold' -> daily)
  4. unknown  no plan; the duplicate rule stays a warning for these

Values a human has set are never overwritten: a row whose source is 'manual' is left
alone entirely, and claim_period is preserved on every row because it is an
entitlement decision, not catalogue data.
"""
import re
import sys
from pathlib import Path

from app.db import catalogue_connection, portal_cursor

# Canonical plans and the billing period each implies, in days.
PERIOD_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "quarterly": 90,
    "semi_annual": 182,
    "annual": 365,
    "one_time": None,
}

# Everything the two catalogues spell differently, mapped onto the canonical plans.
PLAN_ALIASES = {
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "quaterly": "quarterly",            # workbook spelling
    "semi_annual": "semi_annual",
    "semiannual": "semi_annual",
    "semiannually": "semi_annual",
    "sami annual": "semi_annual",
    "annual": "annual",
    "annually": "annual",
    "yearly": "annual",
    "one time service": "one_time",
    "one time": "one_time",
}

# Checked longest-first so 'semiannual' wins over 'annual' inside the same name.
NAME_KEYWORDS = ["semiannual", "semi annual", "quarterly", "quaterly", "annually",
                 "annual", "yearly", "monthly", "weekly", "daily"]


def canon_plan(value):
    key = " ".join(str(value or "").lower().replace("-", " ").replace("_", " ").split())
    return PLAN_ALIASES.get(key)


def plan_from_name(service_name):
    """The billing period spelled out in the product name, if any."""
    squashed = " ".join(str(service_name or "").lower().replace("-", " ").split())
    for kw in NAME_KEYWORDS:
        if kw in squashed or kw.replace(" ", "") in squashed.replace(" ", ""):
            return canon_plan(kw)
    return None


def norm_key(value):
    """Same rule as the portal_norm_text() SQL function, applied without a round trip.

    Both must agree exactly: the service keys in claim_service_plans are produced by
    the SQL function, and the catalogue is matched against them with this one.
    """
    key = re.sub(r"[^a-z0-9]+", " ", str(value if value is not None else "").lower()).strip()
    return key or None


def load_mysql_catalogue():
    """{normalised product name: {plan, coverage, duration, allow_duplicate, category}}"""
    cn = catalogue_connection()
    try:
        with cn.cursor() as cur:
            # One row per product. A product can have several policy rules over time, so
            # take the newest active one rather than letting the join multiply rows.
            cur.execute(
                """
                SELECT p.product_name, p.price_plan, p.charge_interval_days, p.coverage,
                       cat.category_name,
                       r.policy_duration_days, r.allow_duplicate_request
                FROM digital_insurance_products.products p
                LEFT JOIN digital_insurance_products.product_categories cat
                       ON cat.category_id = p.category_id
                LEFT JOIN (
                    SELECT pr.*
                    FROM digital_insurance_products.product_policy_rules pr
                    JOIN (
                        SELECT product_id, MAX(id) AS id
                        FROM digital_insurance_products.product_policy_rules
                        WHERE is_active = 1
                        GROUP BY product_id
                    ) newest ON newest.id = pr.id
                ) r ON r.product_id = p.product_id
                WHERE p.product_name IS NOT NULL
                """
            )
            return cur.fetchall()
    finally:
        cn.close()


def load_excel_catalogue(path):
    """{product name: {plan, coverage}} from every sheet of the products workbook."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    out = []
    # (sheet, name column, plan column, coverage column) - the workbook spells these
    # differently on the per-partner sheets and on the 'Product' table dump.
    sheets = [("Sheet1", "Product Name", "Price Plan", "Coverage"),
              ("EasyPaisa", "Product Name", "Price Plan", "Coverage"),
              ("Product", "ProductName", "PlanType", "Coverage")]
    for sheet, name_col, plan_col, cov_col in sheets:
        if sheet not in wb.sheetnames:
            continue
        rows = wb[sheet].iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows)]
        if name_col not in header:
            continue
        ix = {h: i for i, h in enumerate(header)}
        for row in rows:
            name = row[ix[name_col]] if ix[name_col] < len(row) else None
            if not name:
                continue
            def cell(col):
                i = ix.get(col)
                return row[i] if i is not None and i < len(row) else None
            out.append({"product_name": name, "price_plan": cell(plan_col),
                        "coverage": cell(cov_col)})
    return out


def as_amount(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    # The workbook uses '-' and 0 for "no coverage stated".
    return amount if amount > 0 else None


def main(excel_path=None, dry_run=False):
    mysql_rows = load_mysql_catalogue()
    excel_rows = load_excel_catalogue(excel_path) if excel_path else []
    print(f"catalogue sources: mysql={len(mysql_rows)} rows, excel={len(excel_rows)} rows")

    with portal_cursor() as cur:
        # Index both catalogues by the same normalised key the claims are matched on.
        mysql_by_key, excel_by_key = {}, {}
        for r in mysql_rows:
            mysql_by_key.setdefault(norm_key(r["product_name"]), r)
        for r in excel_rows:
            excel_by_key.setdefault(norm_key(r["product_name"]), r)

        # Services actually claimed against, with their most common category.
        cur.execute(
            """
            SELECT portal_norm_text("ServiceName") AS service_key,
                   min(btrim("ServiceName"))       AS service_name,
                   mode() WITHIN GROUP (ORDER BY "Category") AS category,
                   count(*)                        AS claims
            FROM digital_insurance."InitialClaimRasings"
            WHERE portal_norm_text("ServiceName") IS NOT NULL
            GROUP BY portal_norm_text("ServiceName")
            """
        )
        services = cur.fetchall()

        # Rows already configured by hand, which this import must not overwrite.
        cur.execute("SELECT service_key, source, claim_period FROM claim_service_plans")
        existing = {r["service_key"]: r for r in cur.fetchall()}

        resolved, counts = [], {"mysql": 0, "excel": 0, "keyword": 0, "unknown": 0, "manual": 0}
        for svc in services:
            key = svc["service_key"]
            prior = existing.get(key)
            if prior and prior["source"] == "manual":
                counts["manual"] += 1
                continue

            m, x = mysql_by_key.get(key), excel_by_key.get(key)
            plan = coverage = duration = allow_dup = None
            source = "unknown"

            if m and canon_plan(m["price_plan"]):
                source, plan = "mysql", canon_plan(m["price_plan"])
                coverage = as_amount(m["coverage"])
                duration = m["policy_duration_days"] or m["charge_interval_days"]
                allow_dup = None if m["allow_duplicate_request"] is None else bool(m["allow_duplicate_request"])
            elif x and canon_plan(x["price_plan"]):
                source, plan = "excel", canon_plan(x["price_plan"])
                coverage = as_amount(x["coverage"])
            elif plan_from_name(svc["service_name"]):
                source, plan = "keyword", plan_from_name(svc["service_name"])
            # Coverage can still come from the other catalogue even if the plan didn't.
            if coverage is None and m:
                coverage = as_amount(m["coverage"])
            if coverage is None and x:
                coverage = as_amount(x["coverage"])

            period = duration or PERIOD_DAYS.get(plan)
            counts[source] += 1
            resolved.append({
                "service_key": key,
                "service_name": svc["service_name"],
                "plan": plan,
                "period_days": period,
                # Preserve a period someone already set; never invent one from the billing plan.
                "claim_period": prior["claim_period"] if prior else None,
                "coverage_amount": coverage,
                "allow_duplicate": allow_dup,
                "category": svc["category"],
                "source": source,
                "claims": svc["claims"],
            })

        print(f"\n{len(services)} distinct claim services")
        for k in ("mysql", "excel", "keyword", "unknown", "manual"):
            n = counts[k]
            claims = sum(r["claims"] for r in resolved if r["source"] == k)
            print(f"  {k:<8} services={n:>3}  claims={claims:>5}")

        unknown = sorted((r for r in resolved if r["source"] == "unknown"),
                         key=lambda r: -r["claims"])
        if unknown:
            print(f"\nno plan data ({len(unknown)} services) - these keep a warning-level duplicate rule:")
            for r in unknown:
                print(f"  {r['claims']:>5} claims  [{r['category'] or '-'}]  {r['service_name']}")

        if dry_run:
            print("\ndry run; nothing written")
            return

        for r in resolved:
            cur.execute(
                """
                INSERT INTO claim_service_plans
                    (service_key, service_name, plan, period_days, claim_period,
                     coverage_amount, allow_duplicate, category, source, updated_at)
                VALUES (%(service_key)s, %(service_name)s, %(plan)s, %(period_days)s,
                        %(claim_period)s, %(coverage_amount)s, %(allow_duplicate)s,
                        %(category)s, %(source)s, now())
                ON CONFLICT (service_key) DO UPDATE SET
                    service_name    = EXCLUDED.service_name,
                    plan            = EXCLUDED.plan,
                    period_days     = EXCLUDED.period_days,
                    coverage_amount = EXCLUDED.coverage_amount,
                    allow_duplicate = EXCLUDED.allow_duplicate,
                    category        = EXCLUDED.category,
                    source          = EXCLUDED.source,
                    updated_at      = now()
                """,
                r,
            )

        # Make every category present in the data visible in the window table, so the
        # ones still needing a decision can be seen rather than guessed at.
        cur.execute(
            """
            INSERT INTO claim_category_windows (category_key, note)
            SELECT DISTINCT portal_norm_text("Category"),
                   'seeded from claim data; set window_days to enforce an entitlement period'
            FROM digital_insurance."InitialClaimRasings"
            WHERE portal_norm_text("Category") IS NOT NULL
            ON CONFLICT (category_key) DO NOTHING
            """
        )
        print(f"\nwrote {len(resolved)} service plans")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    path = args[0] if args else None
    if path and not Path(path).exists():
        sys.exit(f"workbook not found: {path}")
    main(path, "--dry-run" in sys.argv[1:])
