from __future__ import annotations

import logging
from typing import Callable
from urllib.parse import parse_qs, urlparse

import requests as _http

from app.config import Settings

log = logging.getLogger(__name__)


class KiteAutoLoginError(Exception):
    pass


def auto_kite_login(
    settings: Settings,
    on_success: Callable[[str, str], None] | None = None,
) -> str:
    """Headless Kite login: user_id + password + TOTP → access_token.

    Follows the Zerodha Connect OAuth redirect chain without a browser.
    Calls on_success(api_key, access_token) after persisting the encrypted token;
    callers use this to restart the OrderWatcher WebSocket with the fresh token.

    Raises KiteAutoLoginError on any failure so callers can log it cleanly.
    Requires PYOTP_AUTO_LOGIN=true, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET in .env.
    """
    try:
        import pyotp
    except ImportError as exc:
        raise KiteAutoLoginError("pyotp not installed — add it to requirements.txt") from exc

    for attr in ("KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET"):
        if not getattr(settings, attr, ""):
            raise KiteAutoLoginError(f"{attr} not set in .env")

    redirect_base = settings.KITE_REDIRECT_URL.split("?")[0]
    if not redirect_base:
        raise KiteAutoLoginError("KITE_REDIRECT_URL not set in .env")

    sess = _http.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0"})

    # Step 1: password login
    log.info("[auto_login] step 1 — password login for %s", settings.KITE_USER_ID)
    try:
        resp = sess.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": settings.KITE_USER_ID, "password": settings.KITE_PASSWORD},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        raise KiteAutoLoginError(f"password login request failed: {exc}") from exc

    body = resp.json()
    if body.get("status") != "success":
        raise KiteAutoLoginError(f"password login rejected: {body.get('message', body)}")
    request_id: str = body["data"]["request_id"]

    # Step 2: TOTP — generate code from secret at the moment of the call
    totp_code = pyotp.TOTP(settings.KITE_TOTP_SECRET).now()
    log.info("[auto_login] step 2 — TOTP (request_id prefix: %s)", request_id[:8])
    try:
        resp = sess.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": settings.KITE_USER_ID,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp",
                "skip_session": "",
            },
            timeout=15,
            allow_redirects=False,
        )
    except Exception as exc:
        raise KiteAutoLoginError(f"TOTP request failed: {exc}") from exc

    # Zerodha now returns HTTP 200 with enctoken cookies after TOTP instead of redirecting.
    # Step 3: hit the Connect OAuth endpoint — cookies from step 2 carry the auth session,
    # and Zerodha redirects through connect/finish → KITE_REDIRECT_URL?request_token=...
    if resp.status_code == 200:
        body2 = resp.json()
        if body2.get("status") != "success":
            raise KiteAutoLoginError(f"TOTP rejected: {body2.get('message', body2)}")
        log.info("[auto_login] step 3 — hitting Connect OAuth endpoint")
        connect_url = (
            f"https://kite.zerodha.com/connect/login"
            f"?api_key={settings.KITE_API_KEY}&v=3"
        )
        try:
            resp = sess.get(connect_url, allow_redirects=False, timeout=15)
        except Exception as exc:
            raise KiteAutoLoginError(f"Connect OAuth GET failed: {exc}") from exc

    # Step 3/4: follow the redirect chain to extract request_token
    request_token = _follow_to_request_token(sess, resp, redirect_base)

    # Step 4: exchange request_token → access_token and persist encrypted to disk
    from app.kite_session import get_session_manager
    try:
        access_token = get_session_manager().handle_callback(request_token)
    except Exception as exc:
        raise KiteAutoLoginError(f"token exchange failed: {exc}") from exc

    log.info("[auto_login] login complete for %s", settings.KITE_USER_ID)

    if on_success is not None:
        try:
            on_success(settings.KITE_API_KEY, access_token)
        except Exception as exc:
            log.warning("[auto_login] on_success callback raised (non-fatal): %s", exc)

    return access_token


def _follow_to_request_token(
    sess: _http.Session,
    resp: _http.Response,
    redirect_base: str,
) -> str:
    """Follow the Kite redirect chain and extract request_token when found.

    Checks any Location URL for request_token regardless of domain — this handles
    cases where KITE_REDIRECT_URL in .env differs from the domain registered on
    the Kite Connect app (e.g. ngrok vs duckdns).
    """
    for hop in range(10):
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise KiteAutoLoginError(
                f"unexpected non-redirect HTTP {resp.status_code} at hop {hop}"
            )
        location = resp.headers.get("Location", "")
        if not location:
            raise KiteAutoLoginError(f"empty Location header at hop {hop}")

        if not location.startswith("http"):
            location = "https://kite.zerodha.com" + location

        # Extract request_token from any redirect URL that carries it — don't
        # follow through to our own /kite/callback (that would call handle_callback twice).
        params = parse_qs(urlparse(location).query)
        token = params.get("request_token", [None])[0]
        if token:
            log.info("[auto_login] step 3 — request_token extracted at hop %d", hop)
            return token

        try:
            resp = sess.get(location, allow_redirects=False, timeout=15)
        except Exception as exc:
            raise KiteAutoLoginError(f"redirect follow failed at hop {hop}: {exc}") from exc

    raise KiteAutoLoginError("exhausted 10-hop redirect chain without finding request_token")
