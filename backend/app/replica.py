"""Copies the Digital_Insurance tables the portal reads into claim_portal.digital_insurance.*

Source is only SELECTed from. Each table is fully refreshed (they are small) inside one
Postgres transaction, so readers see either the previous copy or the new one, never half.
Columns added at the source are added to the replica automatically.
"""
import json
import logging
import threading
import time

import psycopg2.extras
from psycopg2 import sql

from .db import portal_connection, source_connection

log = logging.getLogger("replica")

SCHEMA = "digital_insurance"
TABLES = [
    "InitialClaimRasings",
    "ProcessedClaims",
    "ClaimIntimationRemarks",
    "ClaimProcessRemarks",
    "PaymentDetail",
    "ClaimAuditLogs",
    "ClaimBenefitTransactions",
    "TestClaimBlacklists",  # screened against by the BLACKLIST_MATCH rule
]

PG_TYPES = {
    "int": "integer", "bigint": "bigint", "smallint": "smallint", "tinyint": "smallint",
    "bit": "boolean", "decimal": "numeric", "numeric": "numeric", "money": "numeric",
    "float": "double precision", "real": "real",
    "datetime": "timestamp", "datetime2": "timestamp", "smalldatetime": "timestamp",
    "date": "date", "time": "time", "datetimeoffset": "timestamptz",
    "uniqueidentifier": "text",
}

SETUP = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.sync_runs (
    id          bigserial PRIMARY KEY,
    started_at  timestamptz NOT NULL,
    finished_at timestamptz,
    row_counts  jsonb,
    error       text
);
"""


def _source_columns(cur, table):
    cur.execute(
        """SELECT c.COLUMN_NAME, c.DATA_TYPE,
                  CASE WHEN k.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS is_pk
           FROM INFORMATION_SCHEMA.COLUMNS c
           LEFT JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS t
                  ON t.TABLE_NAME = c.TABLE_NAME AND t.CONSTRAINT_TYPE = 'PRIMARY KEY'
           LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
                  ON k.CONSTRAINT_NAME = t.CONSTRAINT_NAME AND k.COLUMN_NAME = c.COLUMN_NAME
           WHERE c.TABLE_NAME = ? ORDER BY c.ORDINAL_POSITION""",
        table,
    )
    return [(name, PG_TYPES.get(dtype, "text"), bool(pk)) for name, dtype, pk in cur.fetchall()]


def _ensure_table(pg, table, columns):
    ident = sql.Identifier(SCHEMA, table)
    defs = [sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(t)) for n, t, _ in columns]
    pks = [sql.Identifier(n) for n, _, pk in columns if pk]
    if pks:
        defs.append(sql.SQL("PRIMARY KEY ({})").format(sql.SQL(", ").join(pks)))
    pg.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(ident, sql.SQL(", ").join(defs)))
    for n, t, _ in columns:
        pg.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(ident, sql.Identifier(n), sql.SQL(t)))


def sync_once():
    """Refresh every replica table. Returns {table: row_count}."""
    counts = {}
    pg_cn = portal_connection()
    try:
        with pg_cn, pg_cn.cursor() as pg:
            pg.execute(SETUP)
            pg.execute(f"INSERT INTO {SCHEMA}.sync_runs (started_at) VALUES (now()) RETURNING id")
            run_id = pg.fetchone()[0]
        try:
            with source_connection() as src_cn:
                src = src_cn.cursor()
                snapshot = {}
                for table in TABLES:
                    columns = _source_columns(src, table)
                    col_sql = ", ".join(f"[{n}]" for n, _, _ in columns)
                    rows = [tuple(r) for r in src.execute(f"SELECT {col_sql} FROM dbo.[{table}]").fetchall()]
                    snapshot[table] = (columns, rows)

            with pg_cn, pg_cn.cursor() as pg:
                for table, (columns, rows) in snapshot.items():
                    _ensure_table(pg, table, columns)
                    ident = sql.Identifier(SCHEMA, table)
                    pg.execute(sql.SQL("TRUNCATE {}").format(ident))
                    if rows:
                        insert = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
                            ident, sql.SQL(", ").join(sql.Identifier(n) for n, _, _ in columns)
                        )
                        psycopg2.extras.execute_values(pg, insert.as_string(pg), rows, page_size=500)
                    counts[table] = len(rows)
                pg.execute(
                    f"UPDATE {SCHEMA}.sync_runs SET finished_at = now(), row_counts = %s WHERE id = %s",
                    (json.dumps(counts), run_id),
                )
        except Exception as exc:
            with pg_cn, pg_cn.cursor() as pg:
                pg.execute(
                    f"UPDATE {SCHEMA}.sync_runs SET finished_at = now(), error = %s WHERE id = %s",
                    (str(exc)[:2000], run_id),
                )
            raise
    finally:
        pg_cn.close()
    return counts


def start_scheduler(interval_minutes):
    """Sync now, then every interval_minutes, on a daemon thread inside the API process.

    Screening runs straight after each sync, because its rules read the replica and
    would otherwise report on the previous copy. A screening failure is logged and
    does not stop the next sync.
    """

    def loop():
        while True:
            try:
                log.info("replica synced: %s", sync_once())
            except Exception:
                log.exception("replica sync failed")
            else:
                try:
                    from . import screening

                    log.info("claims screened: %s", screening.screen_once())
                except Exception:
                    log.exception("claim screening failed")
            time.sleep(interval_minutes * 60)

    threading.Thread(target=loop, name="replica-sync", daemon=True).start()
