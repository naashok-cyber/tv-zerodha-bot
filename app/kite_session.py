from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from kiteconnect import KiteConnect

from app.config import Settings, get_settings


class TokenStaleError(Exception):
    """Access token is missing, corrupt, or older than KITE_MAX_TOKEN_AGE_HOURS.

    Callers must catch this and refuse to place orders until re-auth completes.
    """


class KiteSessionManager:
    """Manages Kite Connect auth, encrypted token persistence, and the KiteConnect client.

    Salt choice: a random 16-byte salt is generated once per installation and stored
    alongside the encrypted token file (data/access_token.salt).  A per-install random
    salt is preferable to a config-controlled static salt because it prevents key-
    derivation attacks if SECRET_KEY leaks.  Losing the salt only forces a re-login,
    which is already required daily.
    """

    def __init__(
        self,
        settings: Settings,
        token_file: Path | None = None,
        salt_file: Path | None = None,
    ) -> None:
        self._settings = settings
        self._token_file = token_file or Path(settings.KITE_ACCESS_TOKEN_FILE)
        self._salt_file = salt_file or self._token_file.with_suffix(".salt")
        self._fernet = self._make_fernet()
        self._kite: KiteConnect | None = None

    # ── Key derivation ────────────────────────────────────────────────────────

    def _get_or_create_salt(self) -> bytes:
        self._salt_file.parent.mkdir(parents=True, exist_ok=True)
        if self._salt_file.exists():
            return bytes.fromhex(self._salt_file.read_text().strip())
        salt = os.urandom(16)
        self._salt_file.write_text(salt.hex())
        return salt

    def _make_fernet(self) -> Fernet:
        if not self._settings.SECRET_KEY:
            raise ValueError(
                "SECRET_KEY must be set in .env — required for access token encryption"
            )
        salt = self._get_or_create_salt()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=self._settings.PBKDF2_ITERATIONS,
        )
        key = base64.urlsafe_b64encode(kdf.derive(self._settings.SECRET_KEY.encode()))
        return Fernet(key)

    # ── Token persistence ─────────────────────────────────────────────────────

    def _save_token(self, access_token: str, created_at: datetime) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "access_token": access_token,
            "created_at": created_at.isoformat(),
        }).encode()
        self._token_file.write_bytes(self._fernet.encrypt(payload))

    def _load_token(self) -> tuple[str, datetime] | None:
        if not self._token_file.exists():
            return None
        try:
            decrypted = self._fernet.decrypt(self._token_file.read_bytes())
            data: dict[str, Any] = json.loads(decrypted)
            created_at = datetime.fromisoformat(data["created_at"])
            return data["access_token"], created_at
        except (InvalidToken, KeyError, ValueError, json.JSONDecodeError):
            return None

    # ── Token validation ──────────────────────────────────────────────────────

    def _require_fresh_token(self) -> tuple[str, datetime]:
        result = self._load_token()
        if result is None:
            raise TokenStaleError(
                "No access token found. Complete the daily Kite login at /kite/login."
            )
        token, created_at = result
        # Normalise to UTC before subtracting; created_at may carry +05:30 or +00:00.
        age = datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)
        limit = timedelta(hours=self._settings.KITE_MAX_TOKEN_AGE_HOURS)
        if age >= limit:
            raise TokenStaleError(
                f"Access token is {age.total_seconds() / 3600:.1f}h old; "
                f"limit is {self._settings.KITE_MAX_TOKEN_AGE_HOURS}h. Re-login required."
            )
        return token, created_at

    # ── KiteConnect client ────────────────────────────────────────────────────

    def get_kite(self) -> KiteConnect:
        """Return a KiteConnect instance with a validated, fresh access token set.

        Raises TokenStaleError if the token is missing or past KITE_MAX_TOKEN_AGE_HOURS.
        Never call place_order without going through this method first.
        """
        token, _ = self._require_fresh_token()
        if self._kite is None:
            self._kite = KiteConnect(api_key=self._settings.KITE_API_KEY)
        self._kite.set_access_token(token)
        return self._kite

    def handle_callback(self, request_token: str) -> str:
        """Exchange a Kite OAuth request_token for an access_token.

        Called by the /kite/callback endpoint after the user completes manual login.
        Saves the encrypted token to disk and sets it on the internal KiteConnect client.
        Returns the plain-text access_token (do not log or store the return value).
        """
        if self._kite is None:
            self._kite = KiteConnect(api_key=self._settings.KITE_API_KEY)
        data: dict[str, Any] = self._kite.generate_session(
            request_token, api_secret=self._settings.KITE_API_SECRET
        )
        access_token: str = data["access_token"]
        self._save_token(access_token, datetime.now(timezone.utc))
        self._kite.set_access_token(access_token)
        return access_token


_session_manager: KiteSessionManager | None = None


def get_session_manager() -> KiteSessionManager:
    """Lazy singleton — use this everywhere outside of tests."""
    global _session_manager
    if _session_manager is None:
        _session_manager = KiteSessionManager(get_settings())
    return _session_manager
