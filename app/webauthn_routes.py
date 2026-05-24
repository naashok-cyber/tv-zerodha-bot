"""WebAuthn + password login — Face ID / fingerprint authentication for the PWA."""
from __future__ import annotations

import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

import webauthn
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticationCredential,
    PublicKeyCredentialDescriptor,
    RegistrationCredential,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.storage import WebAuthnCredential, WebSession, get_db_session

router = APIRouter()


def _parse_model(model_cls, body: bytes):
    """Parse a py_webauthn Pydantic model from JSON bytes — handles v1 and v2 API."""
    if hasattr(model_cls, "model_validate_json"):
        return model_cls.model_validate_json(body)
    return model_cls.parse_raw(body)


_RP_ID = "naashshla.duckdns.org"
_RP_ORIGIN = "https://naashshla.duckdns.org"
_RP_NAME = "ZeroBot"
_USER_ID = b"zerobot-admin"
_USER_NAME = "admin"
SESSION_TTL_HOURS = 12

# In-memory challenge store — single-user, short TTL (2 min)
_challenges: dict[str, tuple[bytes, float]] = {}
_chal_lock = Lock()


def _set_challenge(key: str, challenge: bytes) -> None:
    with _chal_lock:
        _challenges[key] = (challenge, time.monotonic() + 120)


def _pop_challenge(key: str) -> bytes | None:
    with _chal_lock:
        entry = _challenges.pop(key, None)
    if entry is None:
        return None
    challenge, expiry = entry
    return None if time.monotonic() > expiry else challenge


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


# ── Login page ────────────────────────────────────────────────────────────────

def _login_html(next_url: str, has_credential: bool, error: bool) -> str:
    safe_next = next_url.replace('"', '%22')

    error_html = (
        "<div class='err-banner'>Incorrect username or password</div>"
        if error else ""
    )

    faceid_html = ""
    if has_credential:
        faceid_html = """
        <div class='card fcard'>
          <div class='fi-icon' id='fiIcon'>
            <svg width='52' height='52' viewBox='0 0 52 52' fill='none'>
              <path d='M8 20 L8 8 L20 8' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
              <path d='M32 8 L44 8 L44 20' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
              <path d='M44 32 L44 44 L32 44' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
              <path d='M20 44 L8 44 L8 32' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
              <circle cx='19' cy='22' r='2.2' fill='#007AFF'/>
              <circle cx='33' cy='22' r='2.2' fill='#007AFF'/>
              <path d='M19 32 Q26 37.5 33 32' stroke='#007AFF' stroke-width='2.2' fill='none' stroke-linecap='round'/>
              <line x1='26' y1='18' x2='26' y2='28' stroke='#007AFF' stroke-width='2' stroke-linecap='round'/>
            </svg>
          </div>
          <p class='fi-label'>Sign in with Face ID</p>
          <button class='btn-faceid' id='fiBt' onclick='doAuth()'>Use Face ID</button>
          <p class='fi-err' id='fiErr'></p>
        </div>
        <div class='divider'><span>or use password</span></div>
        """

    setup_hint = (
        "" if has_credential else
        "<p class='setup-hint'>Sign in with your password to enable Face ID</p>"
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
.fcard{{text-align:center}}
.fi-icon{{margin-bottom:10px;display:inline-block}}
.fi-label{{font-size:.88em;color:#636366;margin-bottom:14px;font-weight:500}}
.fi-err{{color:#D70015;font-size:.8em;margin-top:10px;min-height:16px}}
.btn-faceid{{display:block;width:100%;padding:14px;background:#007AFF;color:#fff;
  border:none;border-radius:10px;font-size:.95em;font-weight:600;cursor:pointer;
  font-family:inherit;transition:opacity .15s,transform .1s;letter-spacing:-.01em}}
.btn-faceid:active{{transform:scale(.97)}}.btn-faceid:hover{{opacity:.88}}
.btn-faceid:disabled{{opacity:.45;cursor:default}}
.divider{{display:flex;align-items:center;gap:10px;color:#AEAEB2;font-size:.78em;
  font-weight:500;margin-bottom:14px}}
.divider::before,.divider::after{{content:'';flex:1;height:1px;background:#E5E5EA}}
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
.setup-hint{{text-align:center;font-size:.78em;color:#8E8E93;margin-top:12px}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.45}}}}
.scanning{{animation:pulse 1s ease-in-out infinite}}
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
  {faceid_html}
  <div class='card'>
    <form method='post' action='/auth/login'>
      <input type='hidden' name='next' value='{safe_next}'>
      <div class='field'><input type='text' name='username' placeholder='Username'
        autocomplete='username' required></div>
      <div class='field'><input type='password' name='password' placeholder='Password'
        autocomplete='current-password' required></div>
      <button type='submit' class='btn-signin'>Sign In</button>
    </form>
    {setup_hint}
  </div>
</div>
<script>
function b64(s){{s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';
  const b=atob(s),u=new Uint8Array(b.length);
  for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return u.buffer}}
function toB64(buf){{const b=new Uint8Array(buf);let s='';
  for(const x of b)s+=String.fromCharCode(x);
  return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=/g,'')}}
function credJSON(c){{const r=c.response,j={{id:c.id,rawId:toB64(c.rawId),type:c.type,response:{{}}}};
  if(r.clientDataJSON)j.response.clientDataJSON=toB64(r.clientDataJSON);
  if(r.attestationObject)j.response.attestationObject=toB64(r.attestationObject);
  if(r.authenticatorData)j.response.authenticatorData=toB64(r.authenticatorData);
  if(r.signature)j.response.signature=toB64(r.signature);
  if(r.userHandle)j.response.userHandle=toB64(r.userHandle);
  return j}}
async function doAuth(){{
  const bt=document.getElementById('fiBt'),
        er=document.getElementById('fiErr'),
        ic=document.getElementById('fiIcon');
  if(bt)bt.disabled=true;
  if(ic)ic.classList.add('scanning');
  if(er)er.textContent='';
  try{{
    const or=await fetch('/auth/auth-options');
    const opts=await or.json();
    opts.challenge=b64(opts.challenge);
    if(opts.allowCredentials)opts.allowCredentials=opts.allowCredentials.map(c=>
      ({{...c,id:b64(c.id)}}));
    const cred=await navigator.credentials.get({{publicKey:opts}});
    const ar=await fetch('/auth/authenticate',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(credJSON(cred))}});
    const res=await ar.json();
    if(res.ok){{window.location.href=new URLSearchParams(location.search).get('next')||'/control'}}
    else throw new Error(res.error||'Authentication failed');
  }}catch(e){{
    if(er)er.textContent=e.name==='NotAllowedError'?'Face ID cancelled — use password below':e.message;
    if(bt)bt.disabled=false;
    if(ic)ic.classList.remove('scanning');
  }}
}}
</script>
</body>
</html>"""


def _setup_html(next_url: str) -> str:
    safe_next = next_url.replace('"', '%22')
    return f"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'>
<title>ZeroBot — Enable Face ID</title>
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
.card{{background:#fff;border-radius:14px;padding:28px 22px;text-align:center;
  box-shadow:0 1px 3px rgba(0,0,0,.07),0 1px 2px rgba(0,0,0,.04)}}
.fi-icon{{margin-bottom:18px}}
.card h2{{font-size:1.2em;font-weight:700;color:#1C1C1E;margin-bottom:10px}}
.card p{{font-size:.88em;color:#636366;line-height:1.55;margin-bottom:24px}}
.btn-enable{{display:block;width:100%;padding:14px;background:#007AFF;color:#fff;
  border:none;border-radius:10px;font-size:.95em;font-weight:600;cursor:pointer;
  font-family:inherit;transition:opacity .15s,transform .1s;margin-bottom:12px;letter-spacing:-.01em}}
.btn-enable:active{{transform:scale(.97)}}.btn-enable:hover{{opacity:.88}}
.btn-enable:disabled{{opacity:.45;cursor:default}}
.btn-skip{{display:block;width:100%;padding:12px;background:transparent;color:#8E8E93;
  border:none;font-size:.84em;font-weight:500;cursor:pointer;font-family:inherit;text-decoration:none}}
.fi-err{{color:#D70015;font-size:.82em;margin-top:12px;min-height:16px}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.45}}}}
.scanning{{animation:pulse 1s ease-in-out infinite}}
</style>
</head>
<body>
<div class='container'>
  <div class='app-hdr'>
    <div class='app-icon'>📈</div>
    <div class='app-title'>ZeroBot</div>
  </div>
  <div class='card'>
    <div class='fi-icon' id='fiIcon'>
      <svg width='64' height='64' viewBox='0 0 52 52' fill='none'>
        <path d='M8 20 L8 8 L20 8' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
        <path d='M32 8 L44 8 L44 20' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
        <path d='M44 32 L44 44 L32 44' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
        <path d='M20 44 L8 44 L8 32' stroke='#007AFF' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'/>
        <circle cx='19' cy='22' r='2.2' fill='#007AFF'/>
        <circle cx='33' cy='22' r='2.2' fill='#007AFF'/>
        <path d='M19 32 Q26 37.5 33 32' stroke='#007AFF' stroke-width='2.2' fill='none' stroke-linecap='round'/>
        <line x1='26' y1='18' x2='26' y2='28' stroke='#007AFF' stroke-width='2' stroke-linecap='round'/>
      </svg>
    </div>
    <h2>Enable Face ID</h2>
    <p>Sign in faster next time — no password needed. ZeroBot will recognise you with biometrics.</p>
    <button class='btn-enable' id='enBt' onclick='doSetup()'>Enable Face ID</button>
    <a href='{safe_next}' class='btn-skip'>Skip for now</a>
    <p class='fi-err' id='fiErr'></p>
  </div>
</div>
<script>
function b64(s){{s=s.replace(/-/g,'+').replace(/_/g,'/');while(s.length%4)s+='=';
  const b=atob(s),u=new Uint8Array(b.length);
  for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return u.buffer}}
function toB64(buf){{const b=new Uint8Array(buf);let s='';
  for(const x of b)s+=String.fromCharCode(x);
  return btoa(s).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=/g,'')}}
function credJSON(c){{const r=c.response,j={{id:c.id,rawId:toB64(c.rawId),type:c.type,response:{{}}}};
  if(r.clientDataJSON)j.response.clientDataJSON=toB64(r.clientDataJSON);
  if(r.attestationObject)j.response.attestationObject=toB64(r.attestationObject);
  return j}}
async function doSetup(){{
  const bt=document.getElementById('enBt'),
        er=document.getElementById('fiErr'),
        ic=document.getElementById('fiIcon');
  if(bt)bt.disabled=true;
  if(ic)ic.classList.add('scanning');
  if(er)er.textContent='';
  try{{
    const or=await fetch('/auth/register-options');
    const opts=await or.json();
    if(opts.error)throw new Error(opts.error);
    opts.challenge=b64(opts.challenge);
    if(opts.user&&opts.user.id)opts.user.id=b64(opts.user.id);
    if(opts.excludeCredentials)opts.excludeCredentials=opts.excludeCredentials.map(c=>
      ({{...c,id:b64(c.id)}}));
    const cred=await navigator.credentials.create({{publicKey:opts}});
    const rr=await fetch('/auth/register',{{
      method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(credJSON(cred))}});
    const res=await rr.json();
    if(res.ok){{window.location.href='{safe_next}'}}
    else throw new Error(res.error||'Registration failed');
  }}catch(e){{
    if(er)er.textContent=e.name==='NotAllowedError'?'Face ID cancelled — tap Enable to try again':e.message;
    if(bt)bt.disabled=false;
    if(ic)ic.classList.remove('scanning');
  }}
}}
</script>
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
    has_cred = db.query(WebAuthnCredential).first() is not None
    return HTMLResponse(_login_html(next, has_cred, bool(error)))


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
    dest = next or "/control"
    has_cred = db.query(WebAuthnCredential).first() is not None
    target = f"/auth/setup-faceid?next={dest}" if not has_cred else dest
    resp = RedirectResponse(target, status_code=303)
    _issue_session(db, resp)
    return resp


@router.get("/auth/setup-faceid", response_class=HTMLResponse, include_in_schema=False)
async def setup_faceid_page(
    request: Request,
    next: str = "/control",
    db: Session = Depends(get_db_session),
) -> Response:
    if not _valid_session(request.cookies.get("zb_session"), db):
        return RedirectResponse(f"/login?next={next}", status_code=303)
    return HTMLResponse(_setup_html(next))


@router.get("/auth/register-options", include_in_schema=False)
async def register_options(
    request: Request,
    db: Session = Depends(get_db_session),
) -> Response:
    if not _valid_session(request.cookies.get("zb_session"), db):
        return Response(content='{"error":"not authenticated"}', status_code=401,
                        media_type="application/json")
    existing = db.query(WebAuthnCredential).first()
    exclude = [PublicKeyCredentialDescriptor(id=existing.credential_id)] if existing else []
    opts = webauthn.generate_registration_options(
        rp_id=_RP_ID,
        rp_name=_RP_NAME,
        user_id=_USER_ID,
        user_name=_USER_NAME,
        user_display_name="ZeroBot Admin",
        authenticator_selection=AuthenticatorSelectionCriteria(
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        exclude_credentials=exclude,
    )
    _set_challenge("registration", opts.challenge)
    return Response(content=webauthn.helpers.options_to_json(opts), media_type="application/json")


@router.post("/auth/register", include_in_schema=False)
async def register(
    request: Request,
    db: Session = Depends(get_db_session),
) -> Response:
    if not _valid_session(request.cookies.get("zb_session"), db):
        return Response(content='{"error":"not authenticated"}', status_code=401,
                        media_type="application/json")
    challenge = _pop_challenge("registration")
    if challenge is None:
        return Response(content='{"error":"challenge expired — refresh and try again"}',
                        status_code=400, media_type="application/json")
    body = await request.body()
    try:
        cred = _parse_model(RegistrationCredential, body)
        verification = webauthn.verify_registration_response(
            credential=cred,
            expected_challenge=challenge,
            expected_rp_id=_RP_ID,
            expected_origin=_RP_ORIGIN,
        )
    except Exception as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=400,
                        media_type="application/json")
    existing = db.query(WebAuthnCredential).first()
    if existing:
        db.delete(existing)
    db.add(WebAuthnCredential(
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return Response(content='{"ok":true}', media_type="application/json")


@router.get("/auth/auth-options", include_in_schema=False)
async def auth_options(db: Session = Depends(get_db_session)) -> Response:
    cred = db.query(WebAuthnCredential).first()
    allow = [PublicKeyCredentialDescriptor(id=cred.credential_id)] if cred else []
    opts = webauthn.generate_authentication_options(
        rp_id=_RP_ID,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    _set_challenge("authentication", opts.challenge)
    return Response(content=webauthn.helpers.options_to_json(opts), media_type="application/json")


@router.post("/auth/authenticate", include_in_schema=False)
async def authenticate(
    request: Request,
    db: Session = Depends(get_db_session),
) -> Response:
    challenge = _pop_challenge("authentication")
    if challenge is None:
        return Response(content='{"error":"challenge expired — refresh and try again"}',
                        status_code=400, media_type="application/json")
    stored = db.query(WebAuthnCredential).first()
    if stored is None:
        return Response(content='{"error":"no credential registered"}', status_code=400,
                        media_type="application/json")
    body = await request.body()
    try:
        cred = _parse_model(AuthenticationCredential, body)
        verification = webauthn.verify_authentication_response(
            credential=cred,
            expected_challenge=challenge,
            expected_rp_id=_RP_ID,
            expected_origin=_RP_ORIGIN,
            credential_public_key=stored.public_key,
            credential_current_sign_count=stored.sign_count,
        )
    except Exception as exc:
        return Response(content=json.dumps({"error": str(exc)}), status_code=401,
                        media_type="application/json")
    stored.sign_count = verification.new_sign_count
    resp = Response(content='{"ok":true}', media_type="application/json")
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
