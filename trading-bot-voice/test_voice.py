"""test_voice.py — Voice channel test suite.

All tests run fully offline — no Anthropic API, no Kite, no network.
Claude NLU and order execution functions are patched with mocks.

Run:  python -m pytest test_voice.py -v
"""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

# ── Set required env vars before importing app modules ────────────────────────
os.environ.setdefault("VOICE_AUTH_TOKEN", "test-voice-token")
os.environ.setdefault("ADMIN_AUTH_TOKEN", "test-admin-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("MAX_VOICE_LOTS", "5")

# ── Import after env setup ────────────────────────────────────────────────────
from flask import Flask
import voice_handler
from voice_handler import create_voice_blueprint, _load_config, _save_config, CONFIRM_TTL_SECONDS

INSTRUMENT_CONFIG = {
    "BANKNIFTY" : {"exchange": "NFO", "lot_size": 30,   "auto_options": True,  "mcx_futures": False},
    "NIFTY"     : {"exchange": "NFO", "lot_size": 65,   "auto_options": True,  "mcx_futures": False},
    "CRUDEOILM" : {"exchange": "MCX", "lot_size": 10,   "auto_options": True,  "mcx_futures": True},
}

VOICE_HEADERS = {
    "X-Voice-Auth-Token": "test-voice-token",
    "Content-Type"      : "application/json",
}
ADMIN_HEADERS = {
    "X-Admin-Token": "test-admin-token",
    "Content-Type" : "application/json",
}

# Mock NLU responses
NLU_BANKNIFTY_BUY = {
    "action": "BUY", "underlying": "BANKNIFTY", "quantity": 1,
    "option_type": "CE", "strike": None, "target_delta": 0.65,
    "options_mode": True, "exchange": "NFO", "current_price": 0,
    "target": None, "stoploss": None,
    "confidence": 0.98, "uncertain_fields": [], "action_type": "entry",
}
NLU_LOW_CONFIDENCE = {**NLU_BANKNIFTY_BUY, "confidence": 0.65, "uncertain_fields": ["action", "quantity"]}
NLU_INVALID_INSTRUMENT = {**NLU_BANKNIFTY_BUY, "underlying": "AAPL", "confidence": 0.0}
NLU_OVERSIZE_QTY = {**NLU_BANKNIFTY_BUY, "quantity": 10}
NLU_EXIT_ALL = {
    "action": "EXIT_ALL", "underlying": "BANKNIFTY", "quantity": 0,
    "option_type": None, "strike": None, "target_delta": 0.65,
    "options_mode": True, "exchange": "NFO", "current_price": 0,
    "target": None, "stoploss": None,
    "confidence": 0.96, "uncertain_fields": [], "action_type": "exit_all",
}

MOCK_ORDER_RESULT = (
    {"status": "success", "source": "voice_manual", "mode": "options",
     "order_id": "PAPER-123", "option_symbol": "BANKNIFTY26MAY52000CE",
     "strike": 52000, "real_delta": 0.65, "entry_price": 250.0,
     "real_ltp": 248.0, "real_iv": None, "sl_pct": "10%", "target_pct": "10%",
     "rr_ratio": "1:1", "monitoring_sl": 225.0, "monitoring_target": 275.0,
     "action": "BUY", "underlying": "BANKNIFTY", "quantity": 1,
     "orders_today": 1, "session_pnl": 0.0, "paper_trading": True,
     "paper_trades": 0},
    200,
)
MOCK_EXIT_RESULT = (
    {"status": "success", "source": "voice_manual", "underlying": "BANKNIFTY",
     "exits": [{"symbol": "BANKNIFTY26MAY52000CE", "order_id": "PAPER-124", "status": "exited"}],
     "paper_trading": True},
    200,
)


@pytest.fixture(autouse=True)
def reset_voice_state(tmp_path, monkeypatch):
    """Reset all voice_handler module-level state between tests."""
    voice_handler._pending.clear()
    voice_handler._voice_dedup.clear()
    voice_handler._history.clear()
    for q in voice_handler._rate_store.values():
        q.clear()
    voice_handler._rate_store.clear()

    # Point CONFIG_PATH to a temp file
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"voice_channel_enabled": false, "after_hours_mode": "reject"}')
    monkeypatch.setattr(voice_handler, "CONFIG_PATH", str(cfg_file))
    yield


def make_app(enabled: bool = True):
    """Create a fresh Flask test app with voice channel in the given state."""
    mock_execute = MagicMock(return_value=MOCK_ORDER_RESULT)
    mock_exit    = MagicMock(return_value=MOCK_EXIT_RESULT)

    app = Flask(__name__)
    app.config["TESTING"] = True
    bp  = create_voice_blueprint(mock_execute, mock_exit, INSTRUMENT_CONFIG)
    app.register_blueprint(bp)

    # Enable / disable via config
    cfg = _load_config()
    cfg["voice_channel_enabled"] = enabled
    _save_config(cfg)

    return app, mock_execute, mock_exit


# ══════════════════════════════════════════════════════════════════════════════
# Auth / channel gate tests
# ══════════════════════════════════════════════════════════════════════════════

class TestGating:
    def test_disabled_channel_returns_403(self):
        app, _, _ = make_app(enabled=False)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "buy BANKNIFTY call"}),
                       headers=VOICE_HEADERS)
        assert r.status_code == 403
        assert "disabled" in r.get_json()["error"].lower()

    def test_missing_voice_token_returns_401(self):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "buy BANKNIFTY call"}),
                       content_type="application/json")
        assert r.status_code == 401

    def test_wrong_voice_token_returns_401(self):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "buy BANKNIFTY call"}),
                       headers={**VOICE_HEADERS, "X-Voice-Auth-Token": "wrong"},
                       content_type="application/json")
        assert r.status_code == 401

    def test_missing_admin_token_returns_401(self):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.get("/admin/voice/status", content_type="application/json")
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Transcribe — clear commands
# ══════════════════════════════════════════════════════════════════════════════

class TestTranscribeClearCommand:
    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_returns_pending_with_token(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "Buy one lot of BANKNIFTY ATM call"}),
                       headers=VOICE_HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["status"] == "pending_confirmation"
        assert "confirmation_token" in data
        assert len(data["confirmation_token"]) == 36    # UUID
        assert data["confidence"] == 0.98
        assert data["low_confidence"] is False
        assert data["double_confirm_required"] is False

    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_summary_contains_instrument(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "buy banknifty call"}),
                       headers=VOICE_HEADERS)
        data = r.get_json()
        assert "BANKNIFTY" in data["summary"]


# ══════════════════════════════════════════════════════════════════════════════
# Transcribe — rejection cases
# ══════════════════════════════════════════════════════════════════════════════

class TestTranscribeRejections:
    @patch("voice_handler._call_nlu", return_value=NLU_INVALID_INSTRUMENT)
    def test_invalid_instrument_returns_400(self, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "buy AAPL call"}),
                       headers=VOICE_HEADERS)
        assert r.status_code == 400
        assert "whitelist" in r.get_json()["error"].lower()

    @patch("voice_handler._call_nlu", return_value=NLU_OVERSIZE_QTY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_oversize_quantity_returns_400(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "buy 10 lots banknifty call"}),
                       headers=VOICE_HEADERS)
        assert r.status_code == 400
        assert "MAX_VOICE_LOTS" in r.get_json()["error"]

    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=False)
    def test_after_hours_rejected_by_default(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "buy banknifty call"}),
                       headers=VOICE_HEADERS)
        assert r.status_code == 400
        assert "closed" in r.get_json()["error"].lower()

    def test_audio_without_openai_key_returns_400(self):
        app, _, _ = make_app(enabled=True)
        # TRANSCRIPTION_MODE defaults to "text_only"
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data={"audio": (b"fake_audio_bytes", "audio.wav")},
                       headers={"X-Voice-Auth-Token": "test-voice-token"},
                       content_type="multipart/form-data")
        assert r.status_code == 400
        assert "TRANSCRIPTION_MODE" in r.get_json()["error"]

    def test_no_text_no_audio_returns_400(self):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({}),
                       headers=VOICE_HEADERS)
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# Low confidence flow
# ══════════════════════════════════════════════════════════════════════════════

class TestLowConfidence:
    @patch("voice_handler._call_nlu", return_value=NLU_LOW_CONFIDENCE)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_low_confidence_flagged(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/voice/transcribe",
                       data=json.dumps({"text": "banknifty"}),
                       headers=VOICE_HEADERS)
        data = r.get_json()
        assert r.status_code == 200
        assert data["low_confidence"] is True

    @patch("voice_handler._call_nlu", return_value=NLU_LOW_CONFIDENCE)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_confirm_without_override_blocked(self, _mock_mkt, _mock_nlu):
        app, mock_exec, _ = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "banknifty"}),
                        headers=VOICE_HEADERS)
            token = tr.get_json()["confirmation_token"]
            cr = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": True}),
                        headers=VOICE_HEADERS)
        assert cr.status_code == 200
        assert cr.get_json()["status"] == "low_confidence_confirmation_required"
        mock_exec.assert_not_called()

    @patch("voice_handler._call_nlu", return_value=NLU_LOW_CONFIDENCE)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_confirm_with_override_executes(self, _mock_mkt, _mock_nlu):
        app, mock_exec, _ = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "banknifty"}),
                        headers=VOICE_HEADERS)
            token = tr.get_json()["confirmation_token"]
            cr = c.post("/voice/confirm",
                        data=json.dumps({
                            "confirmation_token": token,
                            "approved": True,
                            "low_confidence_override": True,
                        }),
                        headers=VOICE_HEADERS)
        assert cr.status_code == 200
        assert cr.get_json()["status"] == "approved_executed"
        mock_exec.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Confirm — normal flow
# ══════════════════════════════════════════════════════════════════════════════

class TestConfirm:
    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_approve_calls_execute_order(self, _mock_mkt, _mock_nlu):
        app, mock_exec, _ = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "buy banknifty call"}),
                        headers=VOICE_HEADERS)
            token = tr.get_json()["confirmation_token"]
            cr = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": True}),
                        headers=VOICE_HEADERS)
        assert cr.status_code == 200
        assert cr.get_json()["status"] == "approved_executed"
        mock_exec.assert_called_once()
        call_kwargs = mock_exec.call_args
        assert call_kwargs[1]["source"] == "voice_manual" or \
               (len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "voice_manual")

    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_reject_does_not_execute(self, _mock_mkt, _mock_nlu):
        app, mock_exec, _ = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "buy banknifty call"}),
                        headers=VOICE_HEADERS)
            token = tr.get_json()["confirmation_token"]
            cr = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": False}),
                        headers=VOICE_HEADERS)
        assert cr.status_code == 200
        assert cr.get_json()["status"] == "rejected"
        mock_exec.assert_not_called()

    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_token_consumed_on_confirm(self, _mock_mkt, _mock_nlu):
        """Confirming a token twice should 404 on the second attempt."""
        app, mock_exec, _ = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "buy banknifty call"}),
                        headers=VOICE_HEADERS)
            token = tr.get_json()["confirmation_token"]
            c.post("/voice/confirm",
                   data=json.dumps({"confirmation_token": token, "approved": True}),
                   headers=VOICE_HEADERS)
            r2 = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": True}),
                        headers=VOICE_HEADERS)
        assert r2.status_code == 404

    def test_expired_token_returns_404(self):
        """Manually insert an already-expired entry and try to confirm it."""
        app, _, _ = make_app(enabled=True)
        token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        voice_handler._pending[token] = {
            "_expires"        : time.monotonic() - 1,   # already expired
            "_token"          : token,
            "_source_ip"      : "127.0.0.1",
            "_transcript"     : "test",
            "_confidence"     : 0.99,
            "_low_confidence" : False,
            "_is_exit"        : False,
            "_confirm_step"   : 1,
            "_option_type"    : "CE",
            "_strike"         : None,
            "_uncertain_fields": [],
            "action_type"     : "entry",
            "action"          : "BUY",
            "underlying"      : "BANKNIFTY",
            "quantity"        : 1,
            "options_mode"    : True,
            "target_delta"    : 0.65,
            "exchange"        : "NFO",
            "current_price"   : 0,
            "target"          : None,
            "stoploss"        : None,
            "symbol"          : "",
        }
        with app.test_client() as c:
            r = c.post("/voice/confirm",
                       data=json.dumps({"confirmation_token": token, "approved": True}),
                       headers=VOICE_HEADERS)
        assert r.status_code == 404

    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_channel_disabled_at_confirm_blocks_execution(self, _mock_mkt, _mock_nlu):
        app, mock_exec, _ = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "buy banknifty call"}),
                        headers=VOICE_HEADERS)
            token = tr.get_json()["confirmation_token"]
            # Disable the channel between parse and confirm
            cfg = _load_config()
            cfg["voice_channel_enabled"] = False
            _save_config(cfg)
            cr = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": True}),
                        headers=VOICE_HEADERS)
        assert cr.status_code == 403
        mock_exec.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# EXIT_ALL double-confirm
# ══════════════════════════════════════════════════════════════════════════════

class TestDoubleConfirm:
    @patch("voice_handler._call_nlu", return_value=NLU_EXIT_ALL)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_exit_requires_two_confirmations(self, _mock_mkt, _mock_nlu):
        app, mock_exec, mock_exit = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "exit all banknifty positions"}),
                        headers=VOICE_HEADERS)
            data = tr.get_json()
            assert data["double_confirm_required"] is True
            token = data["confirmation_token"]

            # First confirm → should ask for second confirmation, NOT execute
            r1 = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": True}),
                        headers=VOICE_HEADERS)
            assert r1.status_code == 200
            assert r1.get_json()["status"] == "second_confirmation_required"
            mock_exit.assert_not_called()

            # Second confirm → should execute
            r2 = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": True}),
                        headers=VOICE_HEADERS)
            assert r2.status_code == 200
            assert r2.get_json()["status"] == "approved_executed"
            mock_exit.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# Cancel + pending
# ══════════════════════════════════════════════════════════════════════════════

class TestCancelAndPending:
    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_cancel_removes_token(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            tr = c.post("/voice/transcribe",
                        data=json.dumps({"text": "buy banknifty"}),
                        headers=VOICE_HEADERS)
            token = tr.get_json()["confirmation_token"]
            cr = c.post("/voice/cancel",
                        data=json.dumps({"confirmation_token": token}),
                        headers=VOICE_HEADERS)
            assert cr.status_code == 200
            # Confirm should now 404
            r2 = c.post("/voice/confirm",
                        data=json.dumps({"confirmation_token": token, "approved": True}),
                        headers=VOICE_HEADERS)
            assert r2.status_code == 404

    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_pending_lists_active_orders(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            c.post("/voice/transcribe",
                   data=json.dumps({"text": "buy banknifty call"}),
                   headers=VOICE_HEADERS)
            r = c.get("/voice/pending", headers=VOICE_HEADERS)
        assert r.status_code == 200
        data = r.get_json()
        assert data["count"] == 1
        assert data["pending_orders"][0]["underlying"] == "BANKNIFTY"


# ══════════════════════════════════════════════════════════════════════════════
# Rate limiting
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimit:
    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_31_requests_rate_limited(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            statuses = []
            for _ in range(32):
                # Reset dedup between calls
                voice_handler._voice_dedup.clear()
                r = c.post("/voice/transcribe",
                            data=json.dumps({"text": "buy banknifty call"}),
                            headers=VOICE_HEADERS)
                statuses.append(r.status_code)
        assert statuses.count(429) >= 1
        assert statuses[:30].count(200) == 30


# ══════════════════════════════════════════════════════════════════════════════
# Admin toggle
# ══════════════════════════════════════════════════════════════════════════════

class TestAdminToggle:
    def test_toggle_enables_channel(self):
        app, _, _ = make_app(enabled=False)
        with app.test_client() as c:
            r = c.post("/admin/voice/toggle",
                       data=json.dumps({"enabled": True}),
                       headers=ADMIN_HEADERS)
        assert r.status_code == 200
        assert r.get_json()["voice_channel_enabled"] is True

    def test_toggle_without_admin_token_returns_401(self):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r = c.post("/admin/voice/toggle",
                       data=json.dumps({"enabled": False}),
                       content_type="application/json")
        assert r.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Voice deduplication
# ══════════════════════════════════════════════════════════════════════════════

class TestVoiceDedup:
    @patch("voice_handler._call_nlu", return_value=NLU_BANKNIFTY_BUY)
    @patch("voice_handler._is_market_open", return_value=True)
    def test_same_command_twice_within_10s_rejected(self, _mock_mkt, _mock_nlu):
        app, _, _ = make_app(enabled=True)
        with app.test_client() as c:
            r1 = c.post("/voice/transcribe",
                        data=json.dumps({"text": "buy banknifty call"}),
                        headers=VOICE_HEADERS)
            r2 = c.post("/voice/transcribe",
                        data=json.dumps({"text": "buy banknifty call"}),
                        headers=VOICE_HEADERS)
        assert r1.status_code == 200
        assert r2.status_code == 429
        assert "duplicate" in r2.get_json()["error"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
