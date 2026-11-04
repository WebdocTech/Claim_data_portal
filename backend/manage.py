"""Portal admin tasks.

    python manage.py init-db                                  create the claim_portal DB and tables
    python manage.py create-user <username> <role> "<Full Name>" [password]
    python manage.py reset-password <username> [password]
    python manage.py sync                                     refresh the digital_insurance replica now
    python manage.py screen                                   re-run the claim anomaly checks now
    python manage.py windows                                  show the entitlement period per category
    python manage.py set-window <category> <period|none>      month | year | quarter | half | week | day

A random password is generated and printed when none is given.
"""
import secrets
import sys
from pathlib import Path

from psycopg2 import sql

from app import config, replica, screening
from app.auth import hash_password
from app.db import portal_connection, portal_cursor

ROLES = ("admin", "finance", "claims")


def init_db():
    admin = portal_connection(dbname="postgres")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (config.PG_DATABASE,))
        if cur.fetchone():
            print(f"Database {config.PG_DATABASE} already exists")
        else:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(config.PG_DATABASE)))
            print(f"Created database {config.PG_DATABASE}")
    admin.close()

    here = Path(__file__).parent
    with portal_cursor() as cur:
        cur.execute((here / "schema.sql").read_text())
        cur.execute((here / "screening.sql").read_text())
    print("Schema applied")


def create_user(username, role, full_name, password=None):
    if role not in ROLES:
        sys.exit(f"role must be one of {ROLES}")
    password = password or secrets.token_urlsafe(9)
    with portal_cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, full_name, role, password_hash, password_plain) VALUES (%s, %s, %s, %s, %s)",
            (username, full_name, role, hash_password(password), password),
        )
    print(f"{role:8} {username:12} {password}")


def reset_password(username, password=None):
    password = password or secrets.token_urlsafe(9)
    with portal_cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s, password_plain = %s WHERE username = %s",
            (hash_password(password), password, username),
        )
        if cur.rowcount == 0:
            sys.exit(f"No user {username}")
    print(f"{username:12} {password}")


def windows():
    """Show the entitlement period each category is judged against."""
    with portal_cursor() as cur:
        cur.execute(
            """SELECT w.category_key, w.claim_period, count(p.service_key) AS services
                 FROM claim_category_windows w
                 LEFT JOIN claim_service_plans p
                        ON portal_norm_text(p.category) = w.category_key
                GROUP BY w.category_key, w.claim_period
                ORDER BY w.category_key"""
        )
        print(f"{'category':<32} {'one claim per':>14}  services")
        for r in cur.fetchall():
            shown = r["claim_period"] or "not set"
            print(f"{r['category_key']:<32} {shown:>14}  {r['services']}")
    print()
    print("Periods are calendar buckets: 'month' means once in September, so the 18th of")
    print("September and the 8th of October are two separate entitlements.")
    print("Not set means a repeat is reported with the gap, and never as a blocker.")


def set_window(category, period):
    """Set (or clear, with 'none') the calendar period one category is judged against."""
    allowed = ("day", "week", "month", "quarter", "half", "year")
    value = None if str(period).lower() in ("none", "null", "clear") else str(period).lower()
    if value is not None and value not in allowed:
        sys.exit(f"period must be one of {allowed}, or 'none'")
    with portal_cursor() as cur:
        cur.execute("SELECT portal_norm_text(%s) k", (category,))
        key = cur.fetchone()["k"]
        if not key:
            sys.exit("category must not be blank")
        cur.execute(
            """INSERT INTO claim_category_windows (category_key, claim_period, note, updated_at)
               VALUES (%s, %s, 'set with manage.py set-window', now())
               ON CONFLICT (category_key) DO UPDATE
                 SET claim_period = EXCLUDED.claim_period, updated_at = now()""",
            (key, value),
        )
    print(f"{key}: {'cleared' if value is None else 'one claim per ' + value}")
    print("Run 'python manage.py screen' to apply it to existing claims.")


if __name__ == "__main__":
    commands = {
        "init-db": init_db,
        "create-user": create_user,
        "reset-password": reset_password,
        "sync": lambda: print(replica.sync_once()),
        "screen": lambda: print(screening.screen_once()),
        "windows": windows,
        "set-window": set_window,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]](*sys.argv[2:])
