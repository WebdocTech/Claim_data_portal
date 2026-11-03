"""Portal admin tasks.

    python manage.py init-db                                  create the claim_portal DB and tables
    python manage.py create-user <username> <role> "<Full Name>" [password]
    python manage.py reset-password <username> [password]
    python manage.py sync                                     refresh the digital_insurance replica now

A random password is generated and printed when none is given.
"""
import secrets
import sys
from pathlib import Path

from psycopg2 import sql

from app import config, replica
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

    with portal_cursor() as cur:
        cur.execute((Path(__file__).parent / "schema.sql").read_text())
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


if __name__ == "__main__":
    commands = {
        "init-db": init_db,
        "create-user": create_user,
        "reset-password": reset_password,
        "sync": lambda: print(replica.sync_once()),
    }
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]](*sys.argv[2:])
