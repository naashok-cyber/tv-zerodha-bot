"""Password login and session management for the ZeroBot web UI."""
from __future__ import annotations

import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.storage import WebSession, get_db_session

router = APIRouter()

SESSION_TTL_HOURS = 12


def _valid_session(token: str | None, db: Session) -> bool:
    if not token:
        return False
    return (
        db.query(WebSession)
        .filter(
            WebSession.token == token,
            WebSession.expires_at > datetime.now(timezone.utc),
        )
        .first()
    ) is not None


def _issue_session(db: Session, response: Response) -> None:
    token = secrets.token_hex(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)
    db.add(WebSession(
        token=token,
        created_at=datetime.now(timezone.utc),
        expires_at=expires,
    ))
    db.commit()
    response.set_cookie(
        key="zb_session",
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=SESSION_TTL_HOURS * 3600,
        path="/",
    )


def _login_html(next_url: str, error: bool) -> str:
    safe_next = next_url.replace('"', '%22')
    error_html = (
        "<div class='err-banner'>Incorrect username or password</div>"
        if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>
<title>ZeroBot — Sign In</title>
<meta name='apple-mobile-web-app-capable' content='yes'>
<meta name='apple-mobile-web-app-status-bar-style' content='default'>
<meta name='theme-color' content='#007AFF'>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Helvetica Neue',Arial,sans-serif;
  background:#F2F2F7;min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:24px 16px;-webkit-font-smoothing:antialiased}}
.container{{width:100%;max-width:360px}}
.app-hdr{{text-align:center;margin-bottom:28px}}
.app-icon{{width:72px;height:72px;background:linear-gradient(145deg,#007AFF,#5856D6);
  border-radius:18px;display:inline-flex;align-items:center;justify-content:center;
  font-size:2em;margin-bottom:14px;box-shadow:0 8px 24px rgba(0,122,255,.28)}}
.app-title{{font-size:1.5em;font-weight:700;color:#1C1C1E;letter-spacing:-.03em}}
.app-sub{{font-size:.84em;color:#8E8E93;margin-top:4px}}
.card{{background:#fff;border-radius:14px;padding:22px 18px;
  box-shadow:0 1px 3px rgba(0,0,0,.07),0 1px 2px rgba(0,0,0,.04);margin-bottom:14px}}
.field{{margin-bottom:11px}}
.field input{{display:block;width:100%;padding:12px 13px;border:1px solid #E5E5EA;
  border-radius:10px;font-size:.9em;font-family:inherit;background:#F9F9F9;color:#1C1C1E;
  transition:border-color .2s,box-shadow .2s}}
.field input:focus{{outline:none;border-color:#007AFF;background:#fff;
  box-shadow:0 0 0 3px rgba(0,122,255,.15)}}
.btn-signin{{display:block;width:100%;padding:13px;background:#1C1C1E;color:#fff;
  border:none;border-radius:10px;font-size:.9em;font-weight:600;cursor:pointer;
  font-family:inherit;transition:opacity .15s,transform .1s;margin-top:4px}}
.btn-signin:active{{transform:scale(.97)}}.btn-signin:hover{{opacity:.88}}
.err-banner{{background:rgba(255,59,48,.1);color:#D70015;border-radius:10px;
  padding:11px 14px;font-size:.84em;font-weight:500;margin-bottom:14px;text-align:center}}
</style>
</head>
<body>
<div class='container'>
  <div class='app-hdr'>
    <div class='app-icon'>📈</div>
    <div class='app-title'>ZeroBot</div>
    <div class='app-sub'>Zerodha Algo Trading</div>
  </div>
  {error_html}
  <div class='card'>
    <form method='post' action='/auth/login'>
      <input type='hidden' name='next' value='{safe_next}'>
      <div class='field'><input type='text' name='username' placeholder='Username'
        autocomplete='username' required></div>
      <div class='field'><input type='password' name='password' placeholder='Password'
        autocomplete='current-password' required></div>
      <button type='submit' class='btn-signin'>Sign In</button>
    </form>
  </div>
</div>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(
    request: Request,
    next: str = "/control",
    error: int = 0,
    db: Session = Depends(get_db_session),
) -> HTMLResponse:
    if _valid_session(request.cookies.get("zb_session"), db):
        return RedirectResponse(next, status_code=303)
    return HTMLResponse(_login_html(next, bool(error)))


@router.post("/auth/login", include_in_schema=False)
async def password_login(
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/control"),
    db: Session = Depends(get_db_session),
) -> Response:
    settings = get_settings()
    if settings.DASHBOARD_PASSWORD and not (
        hmac.compare_digest(username.encode(), settings.DASHBOARD_USERNAME.encode())
        and hmac.compare_digest(password.encode(), settings.DASHBOARD_PASSWORD.encode())
    ):
        return RedirectResponse(f"/login?next={next}&error=1", status_code=303)
    resp = RedirectResponse(next or "/control", status_code=303)
    _issue_session(db, resp)
    return resp


@router.get("/auth/logout", include_in_schema=False)
async def logout(request: Request, db: Session = Depends(get_db_session)) -> Response:
    token = request.cookies.get("zb_session")
    if token:
        db.query(WebSession).filter(WebSession.token == token).delete()
        db.commit()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("zb_session", path="/")
    return resp
