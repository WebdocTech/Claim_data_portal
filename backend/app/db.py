import re
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
import pyodbc

from . import config


def source_connection():
    """Read-only connection to Digital_Insurance (SQL Server). Never write through this."""
    return pyodbc.connect(
        f"DRIVER={{{config.SQL_DRIVER}}};"
        f"SERVER={config.SQL_HOST},{config.SQL_PORT};"
        f"DATABASE={config.SQL_DATABASE};"
        f"UID={config.SQL_USER};PWD={config.SQL_PASSWORD};"
        "Encrypt=no;TrustServerCertificate=yes;",
        timeout=30,
        readonly=True,
    )


# The legacy "SQL Server" ODBC driver returns datetime2 as text with 7 fractional digits.
DATETIME2_TEXT = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+$")


def _clean(value):
    if isinstance(value, str) and DATETIME2_TEXT.match(value):
        return value[:19]
    return value


def source_query(sql, *params):
    """Run a SELECT on the source and return a list of dicts."""
    with source_connection() as cn:
        cur = cn.cursor()
        cur.execute(sql, *params)
        cols = [d[0] for d in cur.description]
        return [{c: _clean(v) for c, v in zip(cols, row)} for row in cur.fetchall()]


def portal_connection(dbname=None):
    return psycopg2.connect(
        host=config.PG_HOST,
        port=config.PG_PORT,
        dbname=dbname or config.PG_DATABASE,
        user=config.PG_USER,
        password=config.PG_PASSWORD,
    )


@contextmanager
def portal_cursor():
    """Cursor on the claim_portal DB; commits on success, rolls back on error."""
    cn = portal_connection()
    try:
        with cn, cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
    finally:
        cn.close()


def catalogue_connection():
    """Read-only connection to the MySQL product catalogue (digital_insurance_products).

    Imported lazily: the API process never opens this, only the catalogue import does,
    so a missing driver or an unreachable host can't stop the portal from serving.
    """
    import pymysql

    if not config.CATALOGUE_HOST:
        raise RuntimeError("claims_db_host is not set in backend/.env")
    return pymysql.connect(
        host=config.CATALOGUE_HOST,
        port=config.CATALOGUE_PORT,
        user=config.CATALOGUE_USER,
        password=config.CATALOGUE_PASSWORD,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
        read_timeout=120,
    )
