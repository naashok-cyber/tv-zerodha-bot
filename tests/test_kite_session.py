"""Tests for app/kite_session.py — all offline, KiteConnect fully mocked."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.kite_session import KiteSessionManager, TokenStaleError


# ── Test helpers ──────────────────────────────────────────────────────────────

def _settings(**kwargs) -> Settings:
    defaults: dict = {
        "SECRET_KEY": "test-secret-key-for-p0b-testing-xyz",
        "PBKDF2_ITERATIONS": 1,   # 1 iteration: fast for tests; never use in production
    }
    defaults.update(kwargs)
    return Settings(_env_file=None, **defaults)


def _make_manager(tmp_path: Path, **kwargs) -> KiteSessionManager:
    return KiteSessionManager(
        settings=_settings(**kwargs),
        token_file=tmp_path / "token.enc",
        salt_file=tmp_path / "token.salt",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Fernet key setup ──────────────────────────────────────────────────────────

def test_missing_secret_key_raises_on_init(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        KiteSessionManager(
            settings=Settings(_env_file=None, SECRET_KEY="", PBKDF2_ITERATIONS=1),
            token_file=tmp_path / "t.enc",
            salt_file=tmp_path / "t.salt",
        )


def test_salt_file_created_on_first_init(tmp_path: Path) -> None:
    _make_manager(tmp_path)
    assert (tmp_path / "token.salt").exists()


def test_salt_file_contains_valid_hex(tmp_path: Path) -> None:
    _make_manager(tmp_path)
    raw = (tmp_path / "token.salt").read_text().strip()
    assert all(c in "0123456789abcdef" for c in raw)
    assert len(bytes.fromhex(raw)) == 16


def test_same_salt_reused_across_instances(tmp_path: Path) -> None:
    _make_manager(tmp_path)
    salt_first = (tmp_path / "token.salt").read_text()
    _make_manager(tmp_path)
    salt_second = (tmp_path / "token.salt").read_text()
    assert salt_first == salt_second


# ── Token round-trip ──────────────────────────────────────────────────────────

def test_save_then_load_returns_same_token(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    now = _now()
    mgr._save_token("access-token-abc", now)

    result = mgr._load_token()
    assert result is not None
    token, created_at = result
    assert token == "access-token-abc"
    assert abs((created_at.astimezone(timezone.utc) - now).total_seconds()) < 1


def test_load_returns_none_when_file_absent(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    assert mgr._load_token() is None


def test_load_returns_none_on_corrupt_bytes(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    (tmp_path / "token.enc").write_bytes(b"not-valid-fernet-ciphertext")
    assert mgr._load_token() is None


def test_token_file_does_not_contain_plaintext(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._save_token("super-secret-token", _now())
    raw = (tmp_path / "token.enc").read_bytes()
    assert b"super-secret-token" not in raw


def test_wrong_secret_key_cannot_decrypt(tmp_path: Path) -> None:
    mgr_a = _make_manager(tmp_path)
    mgr_a._save_token("token-for-a", _now())

    mgr_b = KiteSessionManager(
        settings=_settings(SECRET_KEY="completely-different-secret-key-!!"),
        token_file=tmp_path / "token.enc",
        salt_file=tmp_path / "token.salt",   # reuse same salt
    )
    assert mgr_b._load_token() is None


# ── Token freshness ───────────────────────────────────────────────────────────

def test_fresh_token_does_not_raise(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._save_token("fresh", _now())
    token, _ = mgr._require_fresh_token()
    assert token == "fresh"


def test_stale_token_raises_token_stale_error(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, KITE_MAX_TOKEN_AGE_HOURS=20)
    mgr._save_token("old", _now() - timedelta(hours=21))
    with pytest.raises(TokenStaleError):
        mgr._require_fresh_token()


def test_token_exactly_at_age_limit_is_stale(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, KITE_MAX_TOKEN_AGE_HOURS=20)
    # Subtract 20h + a tiny buffer so age >= limit at check time
    mgr._save_token("boundary", _now() - timedelta(hours=20, seconds=1))
    with pytest.raises(TokenStaleError):
        mgr._require_fresh_token()


def test_missing_token_raises_token_stale_error(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    with pytest.raises(TokenStaleError):
        mgr._require_fresh_token()


def test_stale_error_message_mentions_configured_limit(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, KITE_MAX_TOKEN_AGE_HOURS=20)
    mgr._save_token("old", _now() - timedelta(hours=25))
    with pytest.raises(TokenStaleError, match="20h"):
        mgr._require_fresh_token()


def test_configurable_token_age_limit(tmp_path: Path) -> None:
    # With a 1-hour limit, a 2-hour-old token must fail.
    mgr = _make_manager(tmp_path, KITE_MAX_TOKEN_AGE_HOURS=1)
    mgr._save_token("tok", _now() - timedelta(hours=2))
    with pytest.raises(TokenStaleError):
        mgr._require_fresh_token()


# ── get_kite ──────────────────────────────────────────────────────────────────

def test_get_kite_creates_client_and_sets_token(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._save_token("live-token", _now())

    with patch("app.kite_session.KiteConnect") as MockKite:
        mock_instance = MagicMock()
        MockKite.return_value = mock_instance

        kite = mgr.get_kite()

        MockKite.assert_called_once_with(api_key="")
        mock_instance.set_access_token.assert_called_once_with("live-token")
        assert kite is mock_instance


def test_get_kite_raises_on_missing_token(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    with pytest.raises(TokenStaleError):
        mgr.get_kite()


def test_get_kite_raises_on_stale_token(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, KITE_MAX_TOKEN_AGE_HOURS=20)
    mgr._save_token("old", _now() - timedelta(hours=21))
    with pytest.raises(TokenStaleError):
        mgr.get_kite()


def test_get_kite_reuses_existing_client_instance(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._save_token("tok", _now())

    with patch("app.kite_session.KiteConnect") as MockKite:
        mock_instance = MagicMock()
        MockKite.return_value = mock_instance

        kite1 = mgr.get_kite()
        kite2 = mgr.get_kite()

        MockKite.assert_called_once()   # constructed only once, not twice
        assert kite1 is kite2


# ── OAuth callback ────────────────────────────────────────────────────────────

def test_callback_returns_access_token(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mock_kite = MagicMock()
    mock_kite.generate_session.return_value = {
        "access_token": "callback-token-xyz",
        "user_id": "AB1234",
    }
    mgr._kite = mock_kite

    result = mgr.handle_callback("request-token-abc")

    assert result == "callback-token-xyz"
    mock_kite.generate_session.assert_called_once_with(
        "request-token-abc", api_secret=""
    )
    mock_kite.set_access_token.assert_called_once_with("callback-token-xyz")


def test_callback_persists_token_to_disk(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mock_kite = MagicMock()
    mock_kite.generate_session.return_value = {"access_token": "persisted-token"}
    mgr._kite = mock_kite

    mgr.handle_callback("req-tok")

    loaded = mgr._load_token()
    assert loaded is not None
    assert loaded[0] == "persisted-token"


def test_callback_creates_kite_client_if_not_yet_initialised(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    assert mgr._kite is None

    with patch("app.kite_session.KiteConnect") as MockKite:
        mock_instance = MagicMock()
        MockKite.return_value = mock_instance
        mock_instance.generate_session.return_value = {"access_token": "tok"}

        mgr.handle_callback("req")

        MockKite.assert_called_once_with(api_key="")


def test_callback_saved_token_is_immediately_usable(tmp_path: Path) -> None:
    """Token written by handle_callback must pass _require_fresh_token without re-login."""
    mgr = _make_manager(tmp_path)
    mock_kite = MagicMock()
    mock_kite.generate_session.return_value = {"access_token": "fresh-after-callback"}
    mgr._kite = mock_kite

    mgr.handle_callback("req")

    # _require_fresh_token should succeed without raising TokenStaleError
    token, _ = mgr._require_fresh_token()
    assert token == "fresh-after-callback"
