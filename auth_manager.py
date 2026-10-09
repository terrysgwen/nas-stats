"""Authentication manager for NAS Stats.

Stores auth configuration, credentials, and active tokens in data/auth.json.
Completely decoupled from data/nas-stats.db to guarantee zero impact on system metrics and services.
"""

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

AUTH_FILE = Path(__file__).parent / "data" / "auth.json"

DEFAULT_USER = "admin"
DEFAULT_PASSWORDS = ["admin123", "admin"]
PBKDF2_ROUNDS = 100_000


def _hash_pbkdf2(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256 password hashing with 100,000 iterations."""
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS)
    return key.hex()


def _hash_legacy_sha256(password: str, salt: str) -> str:
    """Legacy single-round SHA256 for backward compatibility."""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


class AuthManager:
    def __init__(self):
        self._cache: Dict[str, Any] = {}
        # In-memory failed attempts tracker: {ip_str: {"count": int, "locked_until": float}}
        self._failed_attempts: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        if AUTH_FILE.exists():
            try:
                with open(AUTH_FILE, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                    return
            except Exception as e:
                print(f"[Auth] Warning: Could not read {AUTH_FILE}: {e}")

        # Initialize default config
        salt = secrets.token_hex(16)
        default_pwd = "admin123"
        self._cache = {
            "enabled": True,
            "username": DEFAULT_USER,
            "salt": salt,
            "algo": "pbkdf2_sha256",
            "password_hash": _hash_pbkdf2(default_pwd, salt),
            "custom_password_set": False,
            "tokens": {}
        }
        self._save()

    def _save(self):
        try:
            with open(AUTH_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[Auth] Error saving {AUTH_FILE}: {e}")

    def is_ip_locked(self, client_ip: str) -> Tuple[bool, int]:
        """Check if an IP is temporarily locked due to too many failed attempts."""
        if not client_ip:
            return False, 0
        now = time.time()
        record = self._failed_attempts.get(client_ip)
        if not record:
            return False, 0
        locked_until = record.get("locked_until", 0)
        if now < locked_until:
            remain_sec = int(locked_until - now)
            return True, max(1, remain_sec)
        # Lock expired
        if locked_until > 0 and now >= locked_until:
            del self._failed_attempts[client_ip]
        return False, 0

    def record_login_failure(self, client_ip: str) -> Tuple[int, int]:
        """Record a failed login. Locks after 5 attempts for 15 minutes (900s)."""
        if not client_ip:
            return 1, 0
        now = time.time()
        record = self._failed_attempts.setdefault(client_ip, {"count": 0, "locked_until": 0})
        record["count"] += 1
        if record["count"] >= 5:
            record["locked_until"] = now + 900  # 15 minutes
            return record["count"], 900
        return record["count"], 0

    def record_login_success(self, client_ip: str):
        """Clear failed login attempts on successful login."""
        if client_ip in self._failed_attempts:
            del self._failed_attempts[client_ip]

    def verify_credentials(self, username: str, password: str) -> bool:
        """Verify username and password, with automatic migration to PBKDF2."""
        stored_user = self._cache.get("username", DEFAULT_USER).lower()
        input_user = (username or "").strip().lower()

        # Match stored username or default admin
        user_matches = (input_user == stored_user) or (not self._cache.get("custom_password_set") and input_user == "admin")
        if not user_matches:
            return False

        salt = self._cache.get("salt", "")
        stored_hash = self._cache.get("password_hash", "")
        algo = self._cache.get("algo", "sha256")

        # If custom password hasn't been set by user yet, also allow default fallback passwords
        if not self._cache.get("custom_password_set"):
            if password in DEFAULT_PASSWORDS:
                return True

        valid = False
        if algo == "pbkdf2_sha256":
            valid = secrets.compare_digest(_hash_pbkdf2(password, salt), stored_hash)
        else:
            # Legacy sha256 check
            if secrets.compare_digest(_hash_legacy_sha256(password, salt), stored_hash):
                valid = True
                # Migrate to PBKDF2 seamlessly
                new_salt = secrets.token_hex(16)
                self._cache["salt"] = new_salt
                self._cache["algo"] = "pbkdf2_sha256"
                self._cache["password_hash"] = _hash_pbkdf2(password, new_salt)
                self._save()

        return valid

    def create_token(self, username: str, remember: bool = True) -> str:
        """Generate a session token for the user."""
        token = secrets.token_urlsafe(32)
        tokens = self._cache.setdefault("tokens", {})

        now = int(time.time())
        # Remember mode valid for 30 days; normal session valid for 24 hours
        expires_at = (now + 30 * 86400) if remember else (now + 86400)

        # Clean expired tokens (>100 tokens max)
        if len(tokens) > 100:
            tokens.clear()

        tokens[token] = {
            "username": username,
            "created_at": now,
            "expires_at": expires_at,
            "remember": remember
        }
        self._save()
        return token

    def validate_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Check if token is valid and not expired."""
        if not token:
            return None
        tokens = self._cache.get("tokens", {})
        data = tokens.get(token)
        if not data:
            return None

        expires_at = data.get("expires_at", 0)
        now = int(time.time())
        if expires_at and now > expires_at:
            del tokens[token]
            self._save()
            return None

        return data

    def revoke_token(self, token: str) -> bool:
        """Revoke an active token."""
        tokens = self._cache.get("tokens", {})
        if token in tokens:
            del tokens[token]
            self._save()
            return True
        return False

    def change_password(self, old_pwd: str, new_pwd: str) -> Tuple[bool, str]:
        """Change the dashboard access password."""
        if not new_pwd or len(new_pwd.strip()) < 8:
            return False, "新密码长度至少需要 8 位字符（建议包含字母与数字）"

        # Check old password
        if not self.verify_credentials(self._cache.get("username", DEFAULT_USER), old_pwd):
            return False, "当前原密码输入不正确"

        new_salt = secrets.token_hex(16)
        self._cache["salt"] = new_salt
        self._cache["algo"] = "pbkdf2_sha256"
        self._cache["password_hash"] = _hash_pbkdf2(new_pwd.strip(), new_salt)
        self._cache["custom_password_set"] = True
        self._save()
        return True, "密码已成功修改"

    def get_status(self) -> Dict[str, Any]:
        """Get auth status."""
        return {
            "enabled": self._cache.get("enabled", True),
            "username": self._cache.get("username", DEFAULT_USER),
            "custom_password_set": self._cache.get("custom_password_set", False)
        }


auth_mgr = AuthManager()
