"""
Production Authentication and User Account Store for DataRefine.

Provides:
1. Modern Argon2id password hashing and constant-time verification via cryptography.
2. User registration, account verification, login, logout, and password reset.
3. Multi-tenant account records and authenticated user session token management.
4. Google OAuth 2.0 / OpenID Connect token verification and state validation.
5. Authenticated AES-128/256 encrypted account preference storage (never storing plaintext user API keys).
6. Multi-worker safe SQLite storage in WAL mode.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Tuple, Union

from cryptography.exceptions import InvalidKey
from cryptography.fernet import Fernet, MultiFernet
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

from session_store import get_session_storage_dir


# Security Constants & Configuration
DEFAULT_AUTH_TOKEN_HOURS = int(os.getenv("AUTH_TOKEN_HOURS", "72"))  # 3 days
PASSWORD_RESET_TOKEN_MINUTES = int(os.getenv("PASSWORD_RESET_TOKEN_MINUTES", "60"))  # 1 hour
OAUTH_STATE_TIMEOUT_MINUTES = int(os.getenv("OAUTH_STATE_TIMEOUT_MINUTES", "10"))  # 10 minutes
REQUIRE_EMAIL_VERIFICATION = os.getenv("REQUIRE_EMAIL_VERIFICATION", "false").lower() in ("true", "1")
DEFAULT_SECRET_KEY = os.getenv("SECRET_KEY", "datarefine_production_security_key_please_change_in_production")


# =====================================================================
# 1. CRYPTOGRAPHIC PASSWORD HASHING (ARGON2ID)
# =====================================================================

def hash_password(password: str) -> str:
    """
    Hash a user password using Argon2id with 16-byte random salt.
    Format: $argon2id$v=19$m=65536,t=3,p=4$<salt_b64>$<key_b64>
    """
    if not isinstance(password, str) or not password:
        raise ValueError("Password cannot be empty.")

    salt = secrets.token_bytes(16)
    kdf = Argon2id(
        salt=salt,
        length=32,
        iterations=3,
        lanes=4,
        memory_cost=65536,  # 64 MB
        ad=None,
        secret=None,
    )
    derived_key = kdf.derive(password.encode("utf-8"))
    salt_b64 = base64.b64encode(salt).decode("ascii")
    key_b64 = base64.b64encode(derived_key).decode("ascii")
    return f"$argon2id$v=19$m=65536,t=3,p=4${salt_b64}${key_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    """
    Verify a candidate password against an Argon2id hash using constant-time verification.
    """
    if not password or not password_hash:
        return False

    try:
        parts = password_hash.split("$")
        # Expected parts: ["", "argon2id", "v=19", "m=65536,t=3,p=4", salt_b64, key_b64]
        if len(parts) != 6 or parts[1] != "argon2id":
            return False

        param_dict = dict(item.split("=") for item in parts[3].split(","))
        m = int(param_dict.get("m", 65536))
        t = int(param_dict.get("t", 3))
        p = int(param_dict.get("p", 4))
        salt = base64.b64decode(parts[4])
        expected_key = base64.b64decode(parts[5])

        kdf = Argon2id(
            salt=salt,
            length=len(expected_key),
            iterations=t,
            lanes=p,
            memory_cost=m,
            ad=None,
            secret=None,
        )
        kdf.verify(password.encode("utf-8"), expected_key)
        return True
    except (InvalidKey, Exception):
        return False


def validate_password_strength(password: str) -> Tuple[bool, Optional[str]]:
    """
    Validate password complexity: minimum 8 chars, uppercase, lowercase, and digit.
    """
    if not isinstance(password, str) or len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if len(password) > 128:
        return False, "Password cannot exceed 128 characters."
    if not re.search(r"[a-z]", password):
        return False, "Password must contain at least one lowercase letter."
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter."
    if not re.search(r"\d", password):
        return False, "Password must contain at least one number."
    return True, None


# =====================================================================
# 2. ENCRYPTED PREFERENCES & API KEY STORAGE
# =====================================================================

def _derive_fernet_key(secret_str: str) -> bytes:
    """Derive a URL-safe 32-byte base64 Fernet key from arbitrary secret string."""
    raw_digest = hashlib.sha256(secret_str.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(raw_digest)


def get_fernet_suite() -> MultiFernet:
    """
    Construct MultiFernet encryption suite supporting zero-downtime key rotation.
    Primary key: os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY).
    Rotation fallback keys: os.getenv("SECRET_KEY_ROTATION_FALLBACKS", "").
    """
    primary = os.getenv("SECRET_KEY", DEFAULT_SECRET_KEY)
    keys = [_derive_fernet_key(primary)]
    fallbacks = os.getenv("SECRET_KEY_ROTATION_FALLBACKS", "")
    if fallbacks:
        for fb in fallbacks.split(","):
            fb = fb.strip()
            if fb:
                keys.append(_derive_fernet_key(fb))
    return MultiFernet([Fernet(k) for k in keys])


def encrypt_user_secret(plaintext: str) -> str:
    """Encrypt sensitive strings (e.g. user-provided API key overrides) using primary secret key."""
    if not plaintext:
        return ""
    suite = get_fernet_suite()
    return suite.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_user_secret(ciphertext: str) -> Optional[str]:
    """
    Decrypt sensitive strings. MultiFernet tries the primary key first,
    then rotates through historical fallback keys if a rotation occurred.
    """
    if not ciphertext:
        return None
    try:
        suite = get_fernet_suite()
        return suite.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception:
        return None


def rotate_user_secret(ciphertext: str) -> Optional[str]:
    """Re-encrypt ciphertext under the current primary key."""
    if not ciphertext:
        return None
    try:
        suite = get_fernet_suite()
        return suite.rotate(ciphertext.encode("ascii")).decode("ascii")
    except Exception:
        return None


# =====================================================================
# 3. AUTH STORE DATABASE MANAGER
# =====================================================================

class AuthStore:
    """
    Production-grade Authentication and Multi-Tenant Account Store.
    Manages user credentials, Bearer tokens, password resets, and account preferences.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if db_path:
            self.db_path = Path(db_path).resolve()
        else:
            storage_dir = get_session_storage_dir()
            self.db_path = (storage_dir / "sessions_catalog.db").resolve()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._init_sqlite()

    def _get_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=5000;")
        con.execute("PRAGMA foreign_keys=ON;")
        con.row_factory = sqlite3.Row
        return con

    def _init_sqlite(self) -> None:
        with self._lock, self._get_connection() as con:
            # 1. Users Table
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT,
                    full_name TEXT,
                    google_id TEXT UNIQUE,
                    avatar_url TEXT,
                    is_verified INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active',
                    created_at REAL NOT NULL,
                    last_login_at REAL,
                    preferences TEXT DEFAULT '{}'
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);")

            # 2. Authenticated Bearer Tokens Table
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    user_agent TEXT,
                    ip_address TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_user ON auth_tokens(user_id);")
            con.execute("CREATE INDEX IF NOT EXISTS idx_auth_tokens_expires ON auth_tokens(expires_at);")

            # 3. Password Reset Tokens Table
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS password_resets (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(user_id);")

            # 4. Email Verification Tokens Table
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS email_verifications (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used INTEGER DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
                );
                """
            )

            # 5. OAuth State CSRF Protection Table
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_token TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                """
            )

    # =================================================================
    # USER REGISTRATION & AUTHENTICATION
    # =================================================================

    def register_user(
        self,
        email: str,
        password: str,
        full_name: Optional[str] = None,
        is_verified: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Register a new user account with Argon2id password hashing."""
        clean_email = email.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean_email):
            raise ValueError("Invalid email address format.")

        valid_pw, pw_err = validate_password_strength(password)
        if not valid_pw:
            raise ValueError(pw_err)

        pw_hash = hash_password(password)
        user_id = f"usr_{secrets.token_hex(12)}"
        now = time.time()
        verified_val = 1 if (is_verified if is_verified is not None else not REQUIRE_EMAIL_VERIFICATION) else 0

        default_preferences = {
            "theme": "dark",
            "preferred_provider": "gemini",
            "preferred_model": "gemini-2.5-flash",
            "has_custom_api_key": False,
        }

        with self._lock, self._get_connection() as con:
            # Check unique email
            cur = con.execute("SELECT id FROM users WHERE email = ?", (clean_email,))
            if cur.fetchone():
                raise ValueError(f"An account with email '{clean_email}' already exists.")

            con.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, full_name, is_verified, status,
                    created_at, last_login_at, preferences
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    clean_email,
                    pw_hash,
                    full_name.strip() if full_name else None,
                    verified_val,
                    "active",
                    now,
                    now,
                    json.dumps(default_preferences),
                ),
            )

        return self.get_user_by_id(user_id)

    def authenticate_email_password(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        """Authenticate user by email and password using Argon2id."""
        clean_email = email.strip().lower()
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT * FROM users WHERE email = ?", (clean_email,))
            row = cur.fetchone()

        if not row:
            return None

        user_data = dict(row)
        if user_data.get("status") != "active":
            return None

        stored_hash = user_data.get("password_hash")
        if not stored_hash or not verify_password(password, stored_hash):
            return None

        # Update last login
        now = time.time()
        with self._lock, self._get_connection() as con:
            con.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_data["id"]))

        user_data["last_login_at"] = now
        user_data.pop("password_hash", None)
        return user_data

    def upsert_google_user(
        self,
        google_id: str,
        email: str,
        full_name: Optional[str] = None,
        avatar_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or link an account via validated Google OAuth identity."""
        clean_email = email.strip().lower()
        now = time.time()

        with self._lock, self._get_connection() as con:
            # 1. Check by google_id
            cur = con.execute("SELECT * FROM users WHERE google_id = ?", (google_id,))
            row = cur.fetchone()
            if row:
                user_id = row["id"]
                con.execute(
                    "UPDATE users SET last_login_at = ?, is_verified = 1, avatar_url = COALESCE(?, avatar_url) WHERE id = ?",
                    (now, avatar_url, user_id),
                )
                return self.get_user_by_id(user_id)

            # 2. Check by email
            cur = con.execute("SELECT * FROM users WHERE email = ?", (clean_email,))
            row = cur.fetchone()
            if row:
                user_id = row["id"]
                con.execute(
                    "UPDATE users SET google_id = ?, is_verified = 1, last_login_at = ?, avatar_url = COALESCE(?, avatar_url) WHERE id = ?",
                    (google_id, now, avatar_url, user_id),
                )
                return self.get_user_by_id(user_id)

            # 3. Create new user
            user_id = f"usr_{secrets.token_hex(12)}"
            default_preferences = {
                "theme": "dark",
                "preferred_provider": "gemini",
                "preferred_model": "gemini-2.5-flash",
                "has_custom_api_key": False,
            }
            con.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, full_name, google_id, avatar_url,
                    is_verified, status, created_at, last_login_at, preferences
                ) VALUES (?, ?, NULL, ?, ?, ?, 1, 'active', ?, ?, ?)
                """,
                (
                    user_id,
                    clean_email,
                    full_name,
                    google_id,
                    avatar_url,
                    now,
                    now,
                    json.dumps(default_preferences),
                ),
            )

        return self.get_user_by_id(user_id)

    # =================================================================
    # AUTH TOKEN MANAGEMENT (BEARER TOKENS)
    # =================================================================

    def create_auth_token(
        self,
        user_id: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
        expires_hours: int = DEFAULT_AUTH_TOKEN_HOURS,
    ) -> str:
        """Issue a cryptographically secure 256-bit Bearer auth token."""
        raw_token = f"dref_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = time.time()
        expires_at = now + (expires_hours * 3600)

        with self._lock, self._get_connection() as con:
            con.execute(
                """
                INSERT INTO auth_tokens (
                    token_hash, user_id, created_at, expires_at, last_used_at, user_agent, ip_address
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, now, expires_at, now, user_agent, ip_address),
            )

        return raw_token

    def validate_auth_token(self, token: str, touch: bool = True) -> Optional[Dict[str, Any]]:
        """Validate a Bearer auth token and return the associated active user profile."""
        if not token or not isinstance(token, str):
            return None

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()

        with self._lock, self._get_connection() as con:
            cur = con.execute(
                """
                SELECT t.expires_at, u.*
                FROM auth_tokens t
                JOIN users u ON t.user_id = u.id
                WHERE t.token_hash = ? AND u.status = 'active'
                """,
                (token_hash,),
            )
            row = cur.fetchone()

            if not row:
                return None

            if row["expires_at"] < now:
                con.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
                return None

            if touch:
                con.execute("UPDATE auth_tokens SET last_used_at = ? WHERE token_hash = ?", (now, token_hash))

        user_data = dict(row)
        user_data.pop("password_hash", None)
        user_data.pop("expires_at", None)
        if isinstance(user_data.get("preferences"), str):
            try:
                user_data["preferences"] = json.loads(user_data["preferences"])
            except Exception:
                user_data["preferences"] = {}
        return user_data

    def revoke_auth_token(self, token: str) -> bool:
        """Revoke a specific Bearer auth token on logout."""
        if not token:
            return False
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock, self._get_connection() as con:
            cur = con.execute("DELETE FROM auth_tokens WHERE token_hash = ?", (token_hash,))
            return cur.rowcount > 0

    def revoke_all_user_tokens(self, user_id: str) -> int:
        """Revoke all active tokens for a user (e.g. after password reset)."""
        with self._lock, self._get_connection() as con:
            cur = con.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
            return cur.rowcount

    # =================================================================
    # PASSWORD RESET & EMAIL VERIFICATION WORKFLOWS
    # =================================================================

    def create_password_reset_token(self, email: str) -> Optional[str]:
        """Generate a single-use password reset token with expiration."""
        user = self.get_user_by_email(email)
        if not user:
            return None

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = time.time()
        expires_at = now + (PASSWORD_RESET_TOKEN_MINUTES * 60)

        with self._lock, self._get_connection() as con:
            con.execute(
                """
                INSERT INTO password_resets (token_hash, user_id, created_at, expires_at, used)
                VALUES (?, ?, ?, ?, 0)
                """,
                (token_hash, user["id"], now, expires_at),
            )

        return raw_token

    def verify_password_reset_token(self, token: str) -> Optional[str]:
        """Verify password reset token validity. Returns user_id if valid, else None."""
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()

        with self._lock, self._get_connection() as con:
            cur = con.execute(
                "SELECT user_id, expires_at, used FROM password_resets WHERE token_hash = ?",
                (token_hash,),
            )
            row = cur.fetchone()
            if not row or row["used"] == 1 or row["expires_at"] < now:
                return None
            return row["user_id"]

    def reset_password_with_token(self, token: str, new_password: str) -> bool:
        """Update password using a validated reset token and revoke all existing sessions."""
        user_id = self.verify_password_reset_token(token)
        if not user_id:
            return False

        valid_pw, pw_err = validate_password_strength(new_password)
        if not valid_pw:
            raise ValueError(pw_err)

        new_hash = hash_password(new_password)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        with self._lock, self._get_connection() as con:
            con.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user_id))
            con.execute("UPDATE password_resets SET used = 1 WHERE token_hash = ?", (token_hash,))
            con.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
        return True

    def create_email_verification_token(self, user_id: str) -> str:
        """Create a token for email verification."""
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = time.time()
        expires_at = now + (24 * 3600)  # 24 hours

        with self._lock, self._get_connection() as con:
            con.execute(
                """
                INSERT INTO email_verifications (token_hash, user_id, created_at, expires_at, used)
                VALUES (?, ?, ?, ?, 0)
                """,
                (token_hash, user_id, now, expires_at),
            )
        return raw_token

    def verify_email_with_token(self, token: str) -> bool:
        """Mark user account as verified using token."""
        if not token:
            return False
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()

        with self._lock, self._get_connection() as con:
            cur = con.execute(
                "SELECT user_id, expires_at, used FROM email_verifications WHERE token_hash = ?",
                (token_hash,),
            )
            row = cur.fetchone()
            if not row or row["used"] == 1 or row["expires_at"] < now:
                return False
            user_id = row["user_id"]
            con.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
            con.execute("UPDATE email_verifications SET used = 1 WHERE token_hash = ?", (token_hash,))
            return True

    # =================================================================
    # OAUTH STATE MANAGEMENT (CSRF MITIGATION)
    # =================================================================

    def create_oauth_state(self) -> str:
        """Create a temporary state parameter for Google OAuth CSRF protection."""
        state = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + (OAUTH_STATE_TIMEOUT_MINUTES * 60)

        with self._lock, self._get_connection() as con:
            con.execute(
                "INSERT INTO oauth_states (state_token, created_at, expires_at) VALUES (?, ?, ?)",
                (state, now, expires_at),
            )
        return state

    def validate_and_consume_oauth_state(self, state: str) -> bool:
        """Validate that state exists, has not expired, and delete it immediately (single-use)."""
        if not state:
            return False
        now = time.time()

        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT expires_at FROM oauth_states WHERE state_token = ?", (state,))
            row = cur.fetchone()
            if not row:
                return False
            con.execute("DELETE FROM oauth_states WHERE state_token = ?", (state,))
            return row["expires_at"] >= now

    # =================================================================
    # USER QUERIES & PREFERENCES
    # =================================================================

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve user record by user ID."""
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        if not row:
            return None
        data = dict(row)
        data.pop("password_hash", None)
        data["oauth_provider"] = "google" if data.get("google_id") else "email"
        if isinstance(data.get("preferences"), str):
            try:
                data["preferences"] = json.loads(data["preferences"])
            except Exception:
                data["preferences"] = {}
        return data

    def get_raw_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve raw user record including password_hash (for internal auditing/testing)."""
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Retrieve user record by email."""
        clean_email = email.strip().lower()
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT * FROM users WHERE email = ?", (clean_email,))
            row = cur.fetchone()
        if not row:
            return None
        data = dict(row)
        data.pop("password_hash", None)
        data["oauth_provider"] = "google" if data.get("google_id") else "email"
        if isinstance(data.get("preferences"), str):
            try:
                data["preferences"] = json.loads(data["preferences"])
            except Exception:
                data["preferences"] = {}
        return data

    def update_user_preferences(self, user_id: str, new_preferences: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update user preferences. Encrypts custom API keys before saving and never exposes
        raw plaintext credentials.
        """
        user = self.get_user_by_id(user_id)
        if not user:
            raise ValueError(f"User '{user_id}' not found.")

        current_prefs = user.get("preferences", {})

        # Provider-isolated custom API keys
        if "encrypted_api_keys" not in current_prefs or not isinstance(current_prefs["encrypted_api_keys"], dict):
            current_prefs["encrypted_api_keys"] = {}
        if "api_key_previews" not in current_prefs or not isinstance(current_prefs["api_key_previews"], dict):
            current_prefs["api_key_previews"] = {}

        provider_for_key = (
            new_preferences.get("api_key_provider")
            or new_preferences.get("provider")
            or new_preferences.get("preferred_provider")
            or new_preferences.get("default_provider")
            or "gemini"
        ).lower()

        # Handle API key encryption if provided
        custom_key = new_preferences.get("custom_api_key")
        if custom_key:
            clean_key = str(custom_key).strip()
            # Enforce cross-provider key validation
            try:
                from llm_providers import validate_api_key_format_for_provider
                validate_api_key_format_for_provider(provider_for_key, clean_key)
            except ImportError:
                pass
            enc_key = encrypt_user_secret(clean_key)
            preview = f"{clean_key[:6]}...{clean_key[-4:]}" if len(clean_key) > 10 else "configured"

            # Per-provider storage
            current_prefs["encrypted_api_keys"][provider_for_key] = enc_key
            current_prefs["api_key_previews"][provider_for_key] = preview

            # Maintain legacy compatibility fields
            current_prefs["encrypted_api_key"] = enc_key
            current_prefs["api_key_provider"] = provider_for_key
            current_prefs["has_custom_api_key"] = True
            current_prefs["custom_api_key_preview"] = preview

        elif new_preferences.get("clear_api_key"):
            target_prov = new_preferences.get("api_key_provider") or new_preferences.get("provider")
            if target_prov:
                t = str(target_prov).lower()
                current_prefs["encrypted_api_keys"].pop(t, None)
                current_prefs["api_key_previews"].pop(t, None)
                if current_prefs.get("api_key_provider") == t:
                    current_prefs.pop("encrypted_api_key", None)
                    current_prefs.pop("custom_api_key_preview", None)
                    current_prefs.pop("api_key_provider", None)
            else:
                current_prefs["encrypted_api_keys"] = {}
                current_prefs["api_key_previews"] = {}
                current_prefs.pop("encrypted_api_key", None)
                current_prefs.pop("custom_api_key_preview", None)
                current_prefs.pop("api_key_provider", None)

            current_prefs["has_custom_api_key"] = bool(current_prefs["encrypted_api_keys"] or current_prefs.get("encrypted_api_key"))

        # Merge standard preferences
        for k in ("theme", "preferred_provider", "preferred_model", "default_provider", "default_model"):
            if k in new_preferences:
                current_prefs[k] = new_preferences[k]

        with self._lock, self._get_connection() as con:
            con.execute(
                "UPDATE users SET preferences = ? WHERE id = ?",
                (json.dumps(current_prefs), user_id),
            )

        return self.get_user_preferences(user_id)

    def get_user_preferences(self, user_id: str) -> Dict[str, Any]:
        """Get safe user preferences dictionary (without exposing raw secrets or ciphertexts)."""
        user = self.get_user_by_id(user_id)
        if not user:
            return {}
        prefs = dict(user.get("preferences", {}))
        # Remove internal ciphertext before returning to API or frontend
        prefs.pop("encrypted_api_key", None)
        prefs.pop("encrypted_api_keys", None)
        return prefs

    def get_encrypted_user_api_key(self, user_id: str, provider: Optional[str] = None) -> Optional[str]:
        """Retrieve user's raw encrypted custom API key blob from storage."""
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT preferences FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        if not row or not row["preferences"]:
            return None
        try:
            prefs = json.loads(row["preferences"])
            if provider:
                p = str(provider).lower()
                enc_keys = prefs.get("encrypted_api_keys", {})
                if isinstance(enc_keys, dict) and p in enc_keys:
                    return enc_keys[p]
                if prefs.get("api_key_provider") == p:
                    return prefs.get("encrypted_api_key")
                return None
            return prefs.get("encrypted_api_key")
        except Exception:
            return None

    def get_decrypted_user_api_key(self, user_id: str, provider: Optional[str] = None) -> Optional[str]:
        """Internal helper to retrieve user's decrypted custom API key for authorized AI queries."""
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT preferences FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        if not row or not row["preferences"]:
            return None
        try:
            prefs = json.loads(row["preferences"])
            if provider:
                p = str(provider).lower()
                enc_keys = prefs.get("encrypted_api_keys", {})
                if isinstance(enc_keys, dict) and p in enc_keys:
                    return decrypt_user_secret(enc_keys[p])
                if prefs.get("api_key_provider") == p and prefs.get("encrypted_api_key"):
                    return decrypt_user_secret(prefs["encrypted_api_key"])
                # Strictly isolate: never return another provider's key!
                return None
            enc_key = prefs.get("encrypted_api_key")
            return decrypt_user_secret(enc_key) if enc_key else None
        except Exception:
            return None

    def prune_expired_tokens(self) -> Dict[str, int]:
        """Prune expired auth tokens, password resets, and oauth states."""
        now = time.time()
        with self._lock, self._get_connection() as con:
            c1 = con.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (now,)).rowcount
            c2 = con.execute("DELETE FROM password_resets WHERE expires_at < ? OR used = 1", (now,)).rowcount
            c3 = con.execute("DELETE FROM email_verifications WHERE expires_at < ? OR used = 1", (now,)).rowcount
            c4 = con.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,)).rowcount
        return {
            "pruned_auth_tokens": c1,
            "pruned_password_resets": c2,
            "pruned_email_verifications": c3,
            "pruned_oauth_states": c4,
        }

    def rotate_all_user_keys(self) -> int:
        """
        Re-encrypt all user custom API keys under the latest primary SECRET_KEY.
        Uses MultiFernet rotation to transition historical keys to current primary.
        """
        count = 0
        with self._lock, self._get_connection() as con:
            cur = con.execute("SELECT id, preferences FROM users WHERE preferences LIKE '%encrypted_api_key%'")
            rows = cur.fetchall()
            for r in rows:
                try:
                    prefs = json.loads(r["preferences"])
                    updated = False
                    enc = prefs.get("encrypted_api_key")
                    if enc:
                        new_enc = rotate_user_secret(enc)
                        if new_enc and new_enc != enc:
                            prefs["encrypted_api_key"] = new_enc
                            updated = True
                    enc_dict = prefs.get("encrypted_api_keys", {})
                    if isinstance(enc_dict, dict):
                        for prov, prov_enc in enc_dict.items():
                            new_prov_enc = rotate_user_secret(prov_enc)
                            if new_prov_enc and new_prov_enc != prov_enc:
                                enc_dict[prov] = new_prov_enc
                                updated = True
                    if updated:
                        con.execute(
                            "UPDATE users SET preferences = ? WHERE id = ?",
                            (json.dumps(prefs), r["id"]),
                        )
                        count += 1
                except Exception:
                    continue
        return count
