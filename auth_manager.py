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


def _hash_pwd(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


class AuthManager:
    def __init__(self):
        self._cache: Dict[str, Any] = {}
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
            "password_hash": _hash_pwd(default_pwd, salt),
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

    def verify_credentials(self, username: str, password: str) -> bool:
        """Verify username and password."""
        stored_user = self._cache.get("username", DEFAULT_USER).lower()
        input_user = (username or "").strip().lower()

        # Match stored username or default admin
        user_matches = (input_user == stored_user) or (not self._cache.get("custom_password_set") and input_user == "admin")
        if not user_matches:
            return False

        salt = self._cache.get("salt", "")
        stored_hash = self._cache.get("password_hash", "")

        # If custom password hasn't been set by user yet, also allow default fallback passwords
        if not self._cache.get("custom_password_set"):
            if password in DEFAULT_PASSWORDS:
                return True

        return _hash_pwd(password, salt) == stored_hash

    def create_token(self, username: str, remember: bool = True) -> str:
        """Generate a session token for the user."""
        token = secrets.token_urlsafe(32)
        tokens = self._cache.setdefault("tokens", {})

        now = int(time.time())
        # If remember=True, valid for 180 days; otherwise valid for 24 hours
        expires_at = (now + 180 * 86400) if remember else (now + 86400)

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
        if not new_pwd or len(new_pwd.strip()) < 3:
            return False, "新密码长度至少需要 3 位字符"

        # Check old password
        if not self.verify_credentials(self._cache.get("username", DEFAULT_USER), old_pwd):
            return False, "当前原密码输入不正确"

        new_salt = secrets.token_hex(16)
        self._cache["salt"] = new_salt
        self._cache["password_hash"] = _hash_pwd(new_pwd.strip(), new_salt)
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
