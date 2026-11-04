import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Portal settings (JWT secret, portal DB name) live in backend/.env.
# DB credentials are read from the shared easypaisa/.env so they exist in one place.
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(os.getenv("SHARED_ENV_FILE", BACKEND_DIR.parent.parent / "easypaisa" / ".env"))

SQL_HOST = os.getenv("WEBDOC_SQL2_HOST")
SQL_PORT = os.getenv("WEBDOC_SQL2_PORT", "1433")
SQL_USER = os.getenv("WEBDOC_SQL2_USER")
SQL_PASSWORD = os.getenv("WEBDOC_SQL2_PASSWORD")
SQL_DATABASE = os.getenv("WEBDOC_SQL2_DATABASE", "Digital_Insurance")
SQL_DRIVER = os.getenv("WEBDOC_SQL2_DRIVER", "SQL Server")

PG_HOST = os.getenv("DB_HOST_EP")
PG_PORT = os.getenv("DB_PORT_EP", "5432")
PG_USER = os.getenv("DB_USER_EP")
PG_PASSWORD = os.getenv("DB_PASSWORD_EP")
PG_DATABASE = os.getenv("PORTAL_DB_NAME", "claim_portal")

# Product catalogue (MySQL): digital_insurance_products.* holds the live product and
# policy-rule tables the screening catalogue is imported from. Read-only, and only
# read by import_catalogue.py - never during a request.
CATALOGUE_HOST = os.getenv("claims_db_host")
CATALOGUE_PORT = int(os.getenv("claims_db_port", "3306"))
CATALOGUE_USER = os.getenv("claims_db_user")
CATALOGUE_PASSWORD = os.getenv("claims_db_password")

JWT_SECRET = os.getenv("JWT_SECRET")
JWT_HOURS = int(os.getenv("JWT_HOURS", "12"))

REPLICA_SYNC_MINUTES = int(os.getenv("REPLICA_SYNC_MINUTES", "30"))  # 0 disables the in-process sync

CORS_ORIGINS =[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set; add it to claims/backend/.env")
