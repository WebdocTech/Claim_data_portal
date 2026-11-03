from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config
from .db import portal_cursor

bearer = HTTPBearer(auto_error=False)


def hash_password(password):
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def authenticate(username, password):
    with portal_cursor() as cur:
        cur.execute(
            "SELECT id, username, full_name, role, password_hash, is_active FROM users WHERE username = %s",
            (username,),
        )
        user = cur.fetchone()
    if not user or not user["is_active"] or not bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return None
    return {k: user[k] for k in ("id", "username", "full_name", "role")}


def issue_token(user):
    payload = {"sub": str(user["id"]), "exp": datetime.now(timezone.utc) + timedelta(hours=config.JWT_HOURS)}
    return jwt.encode(payload, config.JWT_SECRET, algorithm="HS256")


def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)):
    if creds is None:
        raise HTTPException(401, "Not signed in")
    try:
        payload = jwt.decode(creds.credentials, config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired, please sign in again")
    # Re-read the user every request so role changes and deactivation apply immediately.
    with portal_cursor() as cur:
        cur.execute(
            "SELECT id, username, full_name, role FROM users WHERE id = %s AND is_active",
            (int(payload["sub"]),),
        )
        user = cur.fetchone()
    if not user:
        raise HTTPException(401, "User is inactive or no longer exists")
    return dict(user)


def require_roles(*roles):
    def dep(user=Depends(current_user)):
        if user["role"] not in roles:
            raise HTTPException(403, "You do not have access to this page")
        return user

    return dep
