"""Accounts, password storage and sign-in sessions for HomeStream."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from db import open_db

SESSION_COOKIE = "homestream_session"
SESSION_SECONDS = 30 * 24 * 60 * 60  # stay signed in for 30 days

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9._-]{3,32}")

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 10 * 60

# Paths that need a signed-in user. Everything else (the web page itself,
# the CSS and JavaScript) is public, because the login screen has to load first.
PROTECTED_PREFIXES = ("/api/", "/hls/", "/thumbs/")
PUBLIC_API_PATHS = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup"}


# ---------- Passwords ----------
# We never store the password. We store a "hash": the result of a slow one-way
# calculation. Nobody can turn a hash back into the password, so even someone who
# steals the database file can't read the passwords. scrypt is deliberately slow
# and memory hungry, which makes guessing millions of passwords very expensive.

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = os.urandom(16)  # a random salt makes two identical passwords hash differently
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    # compare_digest takes the same time however many characters match,
    # so an attacker can't learn anything from how fast the answer comes back
    return hmac.compare_digest(actual, expected)


# Used when a username doesn't exist, so "unknown user" takes as long as "wrong password"
DUMMY_HASH = hash_password("this-is-not-a-real-password")


# ---------- Sessions ----------
# After a correct login we give the browser a long random token in a cookie.
# The browser sends it back automatically on every request, including the
# requests a <video> tag and the HLS player make, which can't carry custom headers.
# The database keeps only a hash of the token, so a stolen database can't be used to sign in.

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(row) -> dict:
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with open_db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))  # tidy up old ones
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_hash_token(token), user_id, now + SESSION_SECONDS),
        )
    return token


def get_user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    with open_db() as conn:
        row = conn.execute(
            """SELECT users.id, users.username, users.is_admin
               FROM sessions JOIN users ON users.id = sessions.user_id
               WHERE sessions.token_hash = ? AND sessions.expires_at > ?""",
            (_hash_token(token), int(time.time())),
        ).fetchone()
    return _public_user(row) if row else None


def set_session_cookie(response: Response, token: str, request: Request):
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_SECONDS,
        httponly=True,                          # JavaScript on the page can't read it
        samesite="lax",                         # other websites can't make your browser use it
        secure=request.url.scheme == "https",   # on https, only ever send it over https
        path="/",
    )


# ---------- The gatekeeper ----------

async def auth_middleware(request: Request, call_next):
    """Runs before every request: find out who is asking, and block strangers."""
    token = request.cookies.get(SESSION_COOKIE)
    request.state.user = await run_in_threadpool(get_user_for_token, token) if token else None

    path = request.url.path
    needs_login = path.startswith(PROTECTED_PREFIXES) and path not in PUBLIC_API_PATHS
    if needs_login and request.state.user is None:
        return JSONResponse({"detail": "Please sign in."}, status_code=401)

    return await call_next(request)


# ---------- Slowing down password guessing ----------

_failed_logins: dict[str, list[float]] = {}
_failed_lock = threading.Lock()


def _recent_failures(key: str) -> list[float]:
    cutoff = time.time() - LOCKOUT_SECONDS
    return [t for t in _failed_logins.get(key, []) if t > cutoff]


def is_locked_out(key: str) -> bool:
    with _failed_lock:
        return len(_recent_failures(key)) >= MAX_FAILED_LOGINS


def record_failure(key: str):
    with _failed_lock:
        _failed_logins[key] = _recent_failures(key) + [time.time()]


def clear_failures(key: str):
    with _failed_lock:
        _failed_logins.pop(key, None)


# ---------- Checking what people type ----------

def check_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(400, "Usernames need 3 to 32 characters: letters, numbers, dots, dashes or underscores.")
    return username


def check_password(password: str):
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"Use at least {MIN_PASSWORD_LENGTH} characters for the password.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(400, f"Use at most {MAX_PASSWORD_LENGTH} characters for the password.")


def create_user(username: str, password: str, is_admin: bool) -> int:
    try:
        with open_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (username, hash_password(password), int(is_admin)),
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "That username is already taken.")


def is_local_request(request: Request) -> bool:
    return request.client is not None and request.client.host in ("127.0.0.1", "::1", "localhost")


def require_admin(request: Request) -> dict:
    user = request.state.user
    if user is None or not user["is_admin"]:
        raise HTTPException(403, "Only the admin can do that.")
    return user


# ---------- The routes ----------

class Credentials(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=1000)
    new_password: str = Field(max_length=1000)


router = APIRouter(prefix="/api")


@router.get("/auth/status")
def auth_status():
    """Public: lets the web page know whether to show 'create the first account'."""
    with open_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {"setup_needed": count == 0}


@router.post("/auth/setup")
def setup(credentials: Credentials, request: Request, response: Response):
    with open_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count > 0:
        raise HTTPException(403, "Setup is already done. Sign in instead.")
    if not is_local_request(request):
        raise HTTPException(403, "Create the first account on the laptop itself, by opening http://localhost and your port number.")

    username = check_username(credentials.username)
    check_password(credentials.password)
    user_id = create_user(username, credentials.password, is_admin=True)

    set_session_cookie(response, create_session(user_id), request)
    return {"id": user_id, "username": username, "is_admin": True}


@router.post("/auth/login")
def login(credentials: Credentials, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    key = f"{client_ip}|{credentials.username.strip().lower()}"

    if is_locked_out(key):
        raise HTTPException(429, "Too many wrong attempts. Wait 10 minutes and try again.")

    with open_db() as conn:
        row = conn.execute(
            "SELECT id, username, is_admin, password_hash FROM users WHERE username = ?",
            (credentials.username.strip(),),
        ).fetchone()

    # Always do the slow password check, even for unknown users, and give the same
    # message either way, so nobody can use the login form to find out who has an account.
    stored_hash = row["password_hash"] if row else DUMMY_HASH
    password_ok = verify_password(credentials.password, stored_hash)

    if row is None or not password_ok:
        record_failure(key)
        raise HTTPException(401, "Wrong username or password.")

    clear_failures(key)
    set_session_cookie(response, create_session(row["id"]), request)
    return _public_user(row)


@router.post("/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with open_db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request):
    return request.state.user


@router.post("/auth/password")
def change_password(body: PasswordChange, request: Request):
    user = request.state.user
    with open_db() as conn:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()

    # 400 instead of 401 on purpose: 401 means "your session ended" to the web page
    if not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(400, "Your current password is wrong.")
    check_password(body.new_password)

    keep = _hash_token(request.cookies.get(SESSION_COOKIE, ""))
    with open_db() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hash_password(body.new_password), user["id"]))
        # Sign out every other device, in case the old password was the problem
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user["id"], keep))
    return {"ok": True}


@router.get("/users")
def list_users(request: Request):
    require_admin(request)
    with open_db() as conn:
        rows = conn.execute("SELECT id, username, is_admin FROM users ORDER BY id").fetchall()
    return {"users": [_public_user(row) for row in rows]}


@router.post("/users")
def add_user(credentials: Credentials, request: Request):
    require_admin(request)
    username = check_username(credentials.username)
    check_password(credentials.password)
    user_id = create_user(username, credentials.password, is_admin=False)
    return {"id": user_id, "username": username, "is_admin": False}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request):
    admin = require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(400, "You can't delete your own account.")
    with open_db() as conn:
        deleted = conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount
    if deleted == 0:
        raise HTTPException(404, "That account doesn't exist.")
    return {"ok": True}
