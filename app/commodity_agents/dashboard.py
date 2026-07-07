"""Dashboard HTML/PWA assets, served by routes.py.

Single-file mobile-first pages. Auth is the same httponly zb_session cookie
as /control: pages redirect to /login when there is no valid session, data
calls send the cookie automatically, and no credential is ever stored in the
browser (the old localStorage admin-token scheme is removed; the header path
remains server-side for programmatic API use only). Installable on iOS via
the manifest; approve/reject is the two-step CONFIRM-tap flow.
"""
from __future__ import annotations

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d1117">
<link rel="manifest" href="/commodity-agents/manifest.json">
<title>Commodity Agents</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--fg:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
padding:12px;padding-bottom:60px;max-width:760px;margin:0 auto}
h1{font-size:19px;margin:6px 0 14px;display:flex;justify-content:space-between;align-items:center}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.badge{padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.b-sell{background:#3d1418;color:var(--red)}.b-buy{background:#12261e;color:var(--green)}
.b-none{background:#21262d;color:var(--dim)}.b-veto{background:#3a2c12;color:var(--amber)}
.dim{color:var(--dim);font-size:12.5px}
button{background:#21262d;color:var(--fg);border:1px solid var(--border);border-radius:8px;
padding:8px 14px;font-size:14px;cursor:pointer}
button:active{opacity:.7}
.approve{background:#1a7f37;border-color:#1a7f37;color:#fff}
.reject{background:#8b1a1a;border-color:#8b1a1a;color:#fff}
.confirm{background:var(--amber);border-color:var(--amber);color:#000;font-weight:700}
input{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg);
padding:9px;width:100%;font-size:14px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--border);
border-radius:8px;padding:10px;font-size:12px;max-height:50vh;overflow:auto}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;align-items:flex-end;z-index:10}
.sheet{background:var(--card);border-radius:16px 16px 0 0;padding:16px;width:100%;max-width:760px;
margin:0 auto;max-height:85vh;overflow:auto}
.hidden{display:none}
#toast{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:#21262d;
border:1px solid var(--border);padding:9px 16px;border-radius:10px;font-size:13.5px;z-index:20}
.paper{background:#1c2536;color:var(--blue);text-align:center;border-radius:8px;
padding:5px;font-size:12px;margin-bottom:12px}
</style>
</head>
<body>
<h1>Commodity Agents
  <span><button onclick="location.href='/commodity-agents/analyze'">Analyze</button>
  <button onclick="location.href='/commodity-agents/desk'">Desk</button>
  <button onclick="runNow()">Run now</button>
  <button onclick="location.href='/auth/logout'" title="Sign out">&#8618;</button></span></h1>
<div class="paper">Decision-support mode — approvals are recorded; live execution is separately gated.</div>
<div id="cards"><div class="card dim">Loading…</div></div>
<div id="sheet" class="overlay hidden" onclick="if(event.target===this)closeSheet()">
  <div class="sheet" id="sheetBody"></div></div>
<div id="toast" class="hidden"></div>
<script>
const API='/commodity-agents';
let pendingConfirm=null,recLots={};
localStorage.removeItem('ca_token'); // legacy token storage — now cookie-session auth
function hdrs(){return {'Content-Type':'application/json'}}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.remove('hidden');
setTimeout(()=>t.classList.add('hidden'),3500)}
async function api(path,opts){const r=await fetch(API+path,Object.assign({headers:hdrs()},opts||{}));
if(r.status===401){location.href='/login?next='+encodeURIComponent(location.pathname);throw new Error('401')}
if(!r.ok){const d=await r.json().catch(()=>({detail:r.status}));toast(d.detail||('HTTP '+r.status));throw new Error(r.status)}
return r.json()}
function badge(rec){if(!rec)return '<span class="badge b-none">no data</span>';
if(rec.risk_vetoed)return '<span class="badge b-veto">RISK VETO</span>';
if(rec.direction==='SELL')return '<span class="badge b-sell">SELL</span>';
if(rec.direction==='BUY')return '<span class="badge b-buy">BUY</span>';
return '<span class="badge b-none">NO TRADE</span>'}
function esc(s){return (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){
try{const d=await api('/recommendations');const el=document.getElementById('cards');el.innerHTML='';
for(const [com,rec] of Object.entries(d.recommendations)){
const card=document.createElement('div');card.className='card';
if(!rec){
const lr=(d.last_runs||{})[com];
let why='No pipeline run yet — next cycle fires within 30 min during market hours.';
if(lr){const at=lr.started_at.replace('T',' ').slice(5,16);
if(lr.status==='FAILED')why='Last run '+at+' <b style="color:var(--red)">FAILED</b>: '+esc(lr.error||'unknown');
else if(lr.status==='RUNNING')why='Run in progress (started '+at+')…';
else why='Last run '+at+' finished ('+esc(lr.status)+') without a recommendation.'}
card.innerHTML='<div class="row"><b>'+com+'</b>'+badge(null)+'</div>'+
'<div class="dim" style="margin-top:6px">'+why+'</div>';
el.appendChild(card);continue}
const conf=rec.confidence!=null?Math.round(rec.confidence*100)+'%':'—';
recLots[rec.id]=rec.suggested_lots||1;
let actions='';
if(rec.status==='PROPOSED'&&!rec.risk_vetoed&&rec.direction!=='NO_TRADE'){
actions='<div class="row" style="margin-top:10px" id="act-'+rec.id+'">'+
'<button class="approve" onclick="decide('+rec.id+',\\'approve\\')">Approve</button>'+
'<button class="reject" onclick="decide('+rec.id+',\\'reject\\')">Reject</button></div>'}
else if(rec.status!=='PROPOSED'){actions='<div class="dim" style="margin-top:8px">'+rec.status+'</div>'}
card.innerHTML='<div class="row"><b>'+com+'</b>'+badge(rec)+'</div>'+
'<div class="dim">'+esc(rec.created_at.replace('T',' ').slice(0,16))+' · '+esc(rec.strategy_type)+' · conf '+conf+
(rec.suggested_lots!=null?' · size '+rec.suggested_lots+' lot'+(rec.suggested_lots===1?'':'s'):'')+'</div>'+
'<div style="margin:7px 0 3px">'+esc((rec.reasoning_summary||'').slice(0,220))+'</div>'+
(rec.strikes.length?'<div class="dim">Strikes: '+esc(rec.strikes.join(', '))+'</div>':'')+
'<div class="row" style="margin-top:8px"><button onclick="drill(\\''+rec.run_id+'\\')">Reasoning</button>'+
'<button onclick="showHistory(\\''+com+'\\')">History</button></div>'+actions;
el.appendChild(card)}
}catch(e){}}
async function decide(id,action){
const box=document.getElementById('act-'+id);
if(pendingConfirm&&pendingConfirm.id===id&&pendingConfirm.action===action){
const r=await api('/decision',{method:'POST',body:JSON.stringify(
{recommendation_id:id,action:action,confirm_token:pendingConfirm.token,lots:recLots[id]||1})});
pendingConfirm=null;toast(action.toUpperCase()+' recorded — '+r.status);load();return}
const r=await api('/decision',{method:'POST',body:JSON.stringify({recommendation_id:id,action:action})});
pendingConfirm={id:id,action:action,token:r.confirm_token};
box.innerHTML='<button class="confirm" onclick="decide('+id+',\\''+action+'\\')">TAP AGAIN to confirm '+
action.toUpperCase()+' ('+(recLots[id]||1)+' lot'+((recLots[id]||1)===1?'':'s')+')'+
'</button><button onclick="pendingConfirm=null;load()">Cancel</button>';
setTimeout(()=>{if(pendingConfirm&&pendingConfirm.id===id){pendingConfirm=null;load()}},r.expires_in_seconds*1000)}
async function drill(runId){const d=await api('/runs/'+runId);
openSheet('<h3>Reasoning trail — '+esc(d.commodity)+'</h3><div class="dim">'+esc(d.status)+
' · '+esc((d.started_at||'').replace('T',' ').slice(0,16))+'</div><pre>'+
esc(JSON.stringify({regime:d.regime,events:d.events,trend_agent:d.trend_agent,
event_agent:d.event_agent,vol_agent:d.vol_agent,judge:d.judge,risk_guard:d.risk_guard},null,1))+'</pre>')}
async function showHistory(com){const d=await api('/'+com+'/history?limit=25');
let rows=d.history.map(r=>'<div class="card"><div class="row"><b>'+
esc(r.created_at.replace('T',' ').slice(0,16))+'</b>'+badge(r)+'</div><div class="dim">'+
esc(r.strategy_type)+' · '+esc(r.status)+'</div></div>').join('');
openSheet('<h3>'+com+' — history</h3>'+(rows||'<div class="dim">empty</div>'))}
async function runNow(){await api('/run',{method:'POST',body:JSON.stringify({})});
toast('Pipeline started for all commodities');setTimeout(load,20000)}
function openSheet(html){document.getElementById('sheetBody').innerHTML=html;
document.getElementById('sheet').classList.remove('hidden')}
function closeSheet(){document.getElementById('sheet').classList.add('hidden')}
load();setInterval(load,60000);
if('serviceWorker' in navigator)navigator.serviceWorker.register('/commodity-agents/sw.js');
</script>
</body></html>"""

ANALYZE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0d1117">
<link rel="manifest" href="/commodity-agents/manifest.json">
<title>Analyze — Commodity Agents</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--fg:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
padding:12px;padding-bottom:60px;max-width:760px;margin:0 auto}
h1{font-size:19px;margin:6px 0 14px;display:flex;justify-content:space-between;align-items:center}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chip{padding:7px 13px;border-radius:999px;border:1px solid var(--border);background:#21262d;
color:var(--fg);font-size:13.5px;cursor:pointer}
.chip.sel{background:var(--blue);border-color:var(--blue);color:#06263f;font-weight:700}
button{background:#21262d;color:var(--fg);border:1px solid var(--border);border-radius:8px;
padding:9px 16px;font-size:14px;cursor:pointer}
.go{background:var(--blue);border-color:var(--blue);color:#06263f;font-weight:700;width:100%;
padding:12px;font-size:16px;margin-top:10px}
.go:disabled{opacity:.45}
.approve{background:#1a7f37;border-color:#1a7f37;color:#fff}
.reject{background:#8b1a1a;border-color:#8b1a1a;color:#fff}
.confirm{background:var(--amber);border-color:var(--amber);color:#000;font-weight:700}
input{background:var(--bg);border:1px solid var(--border);border-radius:8px;color:var(--fg);
padding:10px;width:100%;font-size:15px;text-transform:uppercase}
.stage{display:flex;align-items:center;gap:10px;padding:6px 2px;font-size:14px}
.dot{width:18px;height:18px;border-radius:50%;border:2px solid var(--border);flex:none;
display:flex;align-items:center;justify-content:center;font-size:11px}
.dot.done{background:var(--green);border-color:var(--green);color:#06260f}
.dot.active{border-color:var(--blue);animation:pulse 1.2s infinite}
@keyframes pulse{50%{box-shadow:0 0 0 5px rgba(88,166,255,.25)}}
.badge{padding:3px 12px;border-radius:999px;font-size:13px;font-weight:700}
.b-sell{background:#3d1418;color:var(--red)}.b-buy{background:#12261e;color:var(--green)}
.b-none{background:#21262d;color:var(--dim)}.b-veto{background:#3a2c12;color:var(--amber)}
.dim{color:var(--dim);font-size:12.5px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--border);
border-radius:8px;padding:10px;font-size:12px;max-height:45vh;overflow:auto}
#toast{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:#21262d;
border:1px solid var(--border);padding:9px 16px;border-radius:10px;font-size:13.5px;z-index:20}
.hidden{display:none}
details summary{cursor:pointer;color:var(--blue);font-size:13.5px;margin-top:8px}
</style>
</head>
<body>
<h1>Analyze a ticker
  <span><button onclick="location.href='/commodity-agents/dashboard'">Dashboard</button>
  <button onclick="location.href='/commodity-agents/desk'">Desk</button></span></h1>

<div class="card">
  <div class="row" id="chips"></div>
  <div style="margin-top:10px"><input id="ticker" placeholder="or type: NIFTY, BANKNIFTY, GOLD, SILVER, CRUDEOIL, NATURALGAS (aliases: NG, BN, CRUDE)"
    onkeydown="if(event.key==='Enter')analyze()"></div>
  <button class="go" id="goBtn" onclick="analyze()">Analyze</button>
</div>

<div class="card hidden" id="progress">
  <b id="progTitle">Running agents…</b>
  <div id="stages" style="margin-top:8px"></div>
</div>

<div id="result"></div>
<div id="toast" class="hidden"></div>

<script>
const API='/commodity-agents';
const TICKERS=['NIFTY','BANKNIFTY','NATURALGAS','CRUDEOIL','GOLD','SILVER'];
const ALIASES={NG:'NATURALGAS',NATGAS:'NATURALGAS',CRUDE:'CRUDEOIL',OIL:'CRUDEOIL',
BN:'BANKNIFTY',BNF:'BANKNIFTY',NF:'NIFTY',AU:'GOLD',AG:'SILVER'};
let sel=null,poller=null,pendingConfirm=null,runStartedAt=null,recLots={};
localStorage.removeItem('ca_token'); // legacy token storage — now cookie-session auth
function hdrs(){return {'Content-Type':'application/json'}}
function toast(m){const t=document.getElementById('toast');t.textContent=m;
t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),4000)}
function esc(s){return (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function api(path,opts){const r=await fetch(API+path,Object.assign({headers:hdrs()},opts||{}));
if(r.status===401){location.href='/login?next='+encodeURIComponent(location.pathname);throw new Error('401')}
if(!r.ok){const d=await r.json().catch(()=>({detail:r.status}));toast(d.detail||('HTTP '+r.status));throw new Error(r.status)}
return r.json()}
function renderChips(){const c=document.getElementById('chips');
c.innerHTML=TICKERS.map(t=>'<span class="chip'+(t===sel?' sel':'')+'" onclick="pick(\\''+t+'\\')">'+t+'</span>').join('')}
function pick(t){sel=t;document.getElementById('ticker').value=t;renderChips()}
function resolveTicker(){let v=(document.getElementById('ticker').value||'').trim().toUpperCase();
if(!v&&sel)v=sel;v=ALIASES[v]||v;return TICKERS.includes(v)?v:null}
async function analyze(){
const t=resolveTicker();
if(!t){toast('Unknown ticker — pick one of: '+TICKERS.join(', '));return}
pick(t);
document.getElementById('goBtn').disabled=true;
document.getElementById('result').innerHTML='';
document.getElementById('progress').classList.remove('hidden');
document.getElementById('progTitle').textContent='Running agents on '+t+'…';
document.getElementById('stages').innerHTML='<div class="dim">starting…</div>';
runStartedAt=Date.now();
try{await api('/run',{method:'POST',body:JSON.stringify({commodity:t})})}
catch(e){ // 429 = a run is already going; just track it
if(!String(e.message).includes('429')){finishRun();return}}
if(poller)clearInterval(poller);
poller=setInterval(()=>pollRun(t),2500);pollRun(t)}
async function pollRun(t){
let d;try{d=await api('/'+t+'/runs/latest')}catch(e){return}
const run=d.run;if(!run)return;
// only track a run that started at/after our trigger (30s grace for clock skew)
if(new Date(run.started_at).getTime() < runStartedAt-30000)return;
renderStages(run);
if(run.status!=='RUNNING'){finishRun();renderResult(run)}}
function renderStages(run){
let firstPending=run.stages.findIndex(s=>!s.done);
document.getElementById('stages').innerHTML=run.stages.map((s,i)=>{
let cls=s.done?'done':(i===firstPending&&run.status==='RUNNING'?'active':'');
return '<div class="stage"><span class="dot '+cls+'">'+(s.done?'&#10003;':'')+'</span>'+
'<span'+(s.done?'':' class="dim"')+'>'+s.label+'</span></div>'}).join('')}
function finishRun(){if(poller){clearInterval(poller);poller=null}
document.getElementById('goBtn').disabled=false}
function badge(rec){if(rec.risk_vetoed)return '<span class="badge b-veto">RISK VETO — NO TRADE</span>';
if(rec.direction==='SELL')return '<span class="badge b-sell">SELL</span>';
if(rec.direction==='BUY')return '<span class="badge b-buy">BUY</span>';
return '<span class="badge b-none">NO TRADE</span>'}
function renderResult(run){
const el=document.getElementById('result');
if(run.status==='FAILED'){
el.innerHTML='<div class="card"><b>Run failed</b><div class="dim" style="margin-top:6px">'+
esc(run.error||'unknown error')+'</div></div>';return}
const rec=run.recommendation;
if(!rec){el.innerHTML='<div class="card dim">Run finished but produced no recommendation.</div>';return}
const conf=rec.confidence!=null?Math.round(rec.confidence*100)+'%':'—';
recLots[rec.id]=rec.suggested_lots||1;
let dissent=(rec.dissenting_views||[]).map(v=>'<li>'+esc(v)+'</li>').join('');
let actions='';
if(rec.status==='PROPOSED'&&!rec.risk_vetoed&&rec.direction!=='NO_TRADE'){
actions='<div class="row" style="margin-top:12px" id="act-'+rec.id+'">'+
'<button class="approve" onclick="decide('+rec.id+',\\'approve\\')">Approve</button>'+
'<button class="reject" onclick="decide('+rec.id+',\\'reject\\')">Reject</button></div>'}
el.innerHTML='<div class="card">'+
'<div class="row" style="justify-content:space-between"><b style="font-size:17px">'+esc(rec.commodity)+'</b>'+badge(rec)+'</div>'+
'<div class="dim" style="margin-top:4px">'+esc(rec.strategy_type)+' · confidence '+conf+
' · '+(run.status==='SKIPPED_REGIME'?'regime-gated (LLM round skipped)':'full debate')+'</div>'+
(rec.suggested_lots!=null?'<div style="margin-top:6px">Suggested size: <b>'+rec.suggested_lots+
' lot'+(rec.suggested_lots===1?'':'s')+'</b><span class="dim"> — from daily loss budget, '+
'worst-case per lot, and margin</span>'+(rec.suggested_lots===0?
' <b style="color:var(--amber)">(worst case exceeds budget — skip or reduce)</b>':'')+'</div>':'')+
(rec.strikes&&rec.strikes.length?'<div style="margin-top:8px"><b>Strikes:</b> '+esc(rec.strikes.join(', '))+'</div>':'')+
'<div style="margin-top:8px">'+esc(rec.reasoning_summary||'')+'</div>'+
(run.analytics?renderAnalytics(run.analytics):'')+
(dissent?'<details><summary>Dissenting views ('+(rec.dissenting_views||[]).length+
')</summary><ul style="margin:6px 0 0 18px">'+dissent+'</ul></details>':'')+
'<details id="trailDet"><summary>Full reasoning trail</summary><pre id="trail">loading…</pre></details>'+
actions+'</div>';
loadIvChart(rec.commodity);
el.querySelector('#trailDet').addEventListener('toggle',async ev=>{
if(ev.target.open){const full=await api('/runs/'+run.run_id);
const t=document.getElementById('trail');
if(t)t.textContent=JSON.stringify({regime:full.regime,events:full.events,
trend_agent:full.trend_agent,event_agent:full.event_agent,vol_agent:full.vol_agent,
judge:full.judge,risk_guard:full.risk_guard},null,1)}},{once:false})}
function inr(x){if(x==null)return '&#8212;';
return '<span style="color:var(--'+(x>=0?'green':'red')+')">'+(x>=0?'+':'&#8722;')+
'&#8377;'+Math.abs(Math.round(x)).toLocaleString('en-IN')+'</span>'}
function renderAnalytics(a){
let h='<div style="margin-top:10px;border-top:1px solid var(--border);padding-top:10px">'+
'<b>Vol analytics</b>';
if(a.vrp){const v=a.vrp,pos=v.vrp_pts>=0;
h+='<div style="margin-top:6px">IV '+v.atm_iv_pct+'% vs realized '+v.realized_vol_pct+
'% &#8594; <b style="color:var(--'+(pos?'green':'red')+')">VRP '+(pos?'+':'')+v.vrp_pts+' pts</b></div>'+
'<div class="dim">'+esc(v.read)+'</div>'}
if(a.iv_trend&&a.iv_trend.direction!=='insufficient-history'){const tr=a.iv_trend;
const col=tr.direction==='expanding'?'var(--red)':(tr.direction==='contracting'?'var(--green)':'var(--fg)');
h+='<div style="margin-top:6px">IV trend: <b style="color:'+col+'">'+tr.direction.toUpperCase()+'</b>'+
(tr.change_pct!=null?' <span class="dim">('+(tr.change_pct>0?'+':'')+tr.change_pct+'% over last runs)</span>':'')+'</div>'}
else if(a.iv_trend){h+='<div class="dim" style="margin-top:6px">IV trend: not enough run history yet ('+
a.iv_trend.samples+' samples, need 6)</div>'}
if(a.term_structure){const ts=a.term_structure;
const tcol=ts.ratio>=1.05?'var(--green)':(ts.ratio<=0.95?'var(--dim)':'var(--fg)');
h+='<div style="margin-top:6px">Term structure: front '+ts.near_iv_pct+'% / next '+ts.next_iv_pct+
'% = <b style="color:'+tcol+'">&#215;'+ts.ratio+'</b></div><div class="dim">'+esc(ts.read)+'</div>'}
if(a.skew){const sk=a.skew;
h+='<div style="margin-top:6px">25&#916; skew: put '+sk.put_iv_pct+'% vs call '+sk.call_iv_pct+
'% = <b>'+(sk.rr_25d_pts>0?'+':'')+sk.rr_25d_pts+' pts</b></div><div class="dim">'+esc(sk.read)+'</div>'}
if(a.positioning&&a.positioning.pcr!=null){const po=a.positioning;
h+='<div style="margin-top:6px">Positioning (&#177;'+(po.band_pct||10)+'% band): PCR '+po.pcr+
(po.max_pain!=null?' &#183; max pain '+po.max_pain:'')+
(po.put_wall!=null?' &#183; put wall '+po.put_wall:'')+
(po.call_wall!=null?' &#183; call wall '+po.call_wall:'')+'</div>'}
if(a.india_vix!=null){h+='<div style="margin-top:6px">India VIX: <b>'+a.india_vix+'</b></div>'}
if(a.expected_move){const e=a.expected_move;
h+='<div style="margin-top:6px">Market-implied move: &#177;'+e.implied_move_pts+' pts ('+
e.implied_move_pct+'%) over '+e.days_to_expiry+'d';
if(e.realized_move_pts!=null)h+=' &#183; realized pace &#177;'+e.realized_move_pts+
' pts &#183; edge &#215;'+e.edge_ratio;
h+='</div><div class="dim">Straddle breakevens: '+e.breakeven_lower+' &#8212; '+e.breakeven_upper+'</div>'}
if(a.stress&&a.stress.rows&&a.stress.rows.length){
h+='<details><summary>Stress test ('+esc(a.stress.basis)+', IV shock +'+a.stress.iv_shock_pts+
' pts)</summary><table style="width:100%;margin-top:6px;font-size:12.5px;border-collapse:collapse">'+
'<tr class="dim" style="text-align:right"><td style="text-align:left">Move</td>'+
'<td>P&amp;L @ expiry</td><td>P&amp;L IV shock</td></tr>'+
a.stress.rows.map(r=>'<tr style="text-align:right"><td style="text-align:left">'+
(r.move_pct>0?'+':'')+r.move_pct+'%</td><td>'+inr(r.pnl_at_expiry)+'</td><td>'+
inr(r.pnl_iv_up5)+'</td></tr>').join('')+'</table></details>'}
h+='<div id="ivwrap" style="margin-top:10px"></div></div>';
return h}
async function loadIvChart(t){
let d;try{d=await api('/'+t+'/iv-history?limit=120')}catch(e){return}
const w=document.getElementById('ivwrap');if(!w)return;
const pts=(d.points||[]).filter(p=>p.iv!=null);
if(pts.length<2){w.innerHTML='<div class="dim">IV history: '+pts.length+
' sample(s) stored so far &#8212; the IV-over-time chart appears after a few runs.</div>';return}
const W=680,H=170,P=26;
const ivs=pts.map(p=>p.iv*100),rvs=pts.map(p=>p.rv!=null?p.rv*100:null);
const all=ivs.concat(rvs.filter(v=>v!=null));
const lo=Math.min.apply(null,all),hi=Math.max.apply(null,all),span=(hi-lo)||1;
const X=i=>P+(W-2*P)*(pts.length<2?0:i/(pts.length-1));
const Y=v=>H-P-(H-2*P)*(v-lo)/span;
const line=arr=>{let s='',pen=false;
for(let i=0;i<arr.length;i++){if(arr[i]==null){pen=false;continue}
s+=(pen?'L':'M')+X(i).toFixed(1)+' '+Y(arr[i]).toFixed(1);pen=true}return s};
const t0=new Date(pts[0].t),t1=new Date(pts[pts.length-1].t);
const fmt=x=>x.toLocaleDateString('en-IN',{day:'numeric',month:'short'});
w.innerHTML='<div class="dim" style="margin-bottom:4px">ATM IV <span style="color:var(--blue)">&#9644;</span>'+
' vs realized vol <span style="color:var(--amber)">&#9644;</span> &#8212; last '+pts.length+
' runs ('+fmt(t0)+' &#8594; '+fmt(t1)+')</div>'+
'<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;background:var(--bg);'+
'border:1px solid var(--border);border-radius:8px">'+
'<text x="3" y="'+(Y(hi)+4)+'" fill="#8b949e" font-size="10">'+hi.toFixed(1)+'%</text>'+
'<text x="3" y="'+(Y(lo)+4)+'" fill="#8b949e" font-size="10">'+lo.toFixed(1)+'%</text>'+
'<line x1="'+P+'" y1="'+Y(lo)+'" x2="'+(W-P)+'" y2="'+Y(lo)+'" stroke="#30363d" stroke-width="1"/>'+
'<path d="'+line(ivs)+'" fill="none" stroke="#58a6ff" stroke-width="2"/>'+
(rvs.some(v=>v!=null)?'<path d="'+line(rvs)+'" fill="none" stroke="#d29922" stroke-width="1.5" stroke-dasharray="4 3"/>':'')+
'</svg>'}
async function decide(id,action){
const box=document.getElementById('act-'+id);
if(pendingConfirm&&pendingConfirm.id===id&&pendingConfirm.action===action){
const r=await api('/decision',{method:'POST',body:JSON.stringify(
{recommendation_id:id,action:action,confirm_token:pendingConfirm.token,lots:recLots[id]||1})});
pendingConfirm=null;toast(action.toUpperCase()+' recorded — '+r.status);
box.innerHTML='<div class="dim">'+r.status+' · '+esc(r.note||'')+'</div>';return}
const r=await api('/decision',{method:'POST',body:JSON.stringify({recommendation_id:id,action:action})});
pendingConfirm={id:id,action:action,token:r.confirm_token};
box.innerHTML='<button class="confirm" onclick="decide('+id+',\\''+action+'\\')">TAP AGAIN to confirm '+
action.toUpperCase()+' ('+(recLots[id]||1)+' lot'+((recLots[id]||1)===1?'':'s')+')'+
'</button><button onclick="pendingConfirm=null;location.reload()">Cancel</button>'}
renderChips();
if('serviceWorker' in navigator)navigator.serviceWorker.register('/commodity-agents/sw.js');
</script>
</body></html>"""

DESK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#0d1117">
<link rel="manifest" href="/commodity-agents/manifest.json">
<title>Desk — Commodity Agents</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--fg:#e6edf3;--dim:#8b949e;
--green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;
padding:12px;padding-bottom:60px;max-width:760px;margin:0 auto}
h1{font-size:19px;margin:6px 0 14px;display:flex;justify-content:space-between;align-items:center}
h2{font-size:15px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:12px}
.dim{color:var(--dim);font-size:12.5px}
button{background:#21262d;color:var(--fg);border:1px solid var(--border);border-radius:8px;
padding:8px 14px;font-size:14px;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td,th{padding:4px 6px;text-align:right;border-bottom:1px solid var(--border)}
td:first-child,th:first-child{text-align:left}
th{color:var(--dim);font-weight:500}
.chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600;
background:#21262d}
#toast{position:fixed;bottom:14px;left:50%;transform:translateX(-50%);background:#21262d;
border:1px solid var(--border);padding:9px 16px;border-radius:10px;font-size:13.5px;z-index:20}
.hidden{display:none}
</style>
</head>
<body>
<h1>Desk
  <span><button onclick="location.href='/commodity-agents/dashboard'">Dashboard</button>
  <button onclick="location.href='/commodity-agents/analyze'">Analyze</button>
  <button onclick="loadAll()">&#8635;</button></span></h1>

<div class="card"><h2>Open positions — live Greeks</h2><div id="greeks" class="dim">loading…</div></div>
<div class="card"><h2>Trade journal &amp; expectancy</h2><div id="journal" class="dim">loading…</div></div>
<div class="card"><h2>LLM scorecard</h2><div id="calib" class="dim">loading…</div></div>
<div id="toast" class="hidden"></div>

<script>
const API='/commodity-agents';
localStorage.removeItem('ca_token'); // legacy token storage — now cookie-session auth
function esc(s){return (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function toast(m){const t=document.getElementById('toast');t.textContent=m;
t.classList.remove('hidden');setTimeout(()=>t.classList.add('hidden'),4000)}
function inr(x){if(x==null)return '&#8212;';
return '<span style="color:var(--'+(x>=0?'green':'red')+')">'+(x>=0?'+':'&#8722;')+
'&#8377;'+Math.abs(Math.round(x)).toLocaleString('en-IN')+'</span>'}
function num(x,d){return x==null?'&#8212;':Number(x).toFixed(d==null?1:d)}
async function api(path){const r=await fetch(API+path);
if(r.status===401){location.href='/login?next='+encodeURIComponent(location.pathname);throw new Error('401')}
if(!r.ok){const d=await r.json().catch(()=>({detail:r.status}));throw new Error(d.detail||r.status)}
return r.json()}

async function loadGreeks(){
const el=document.getElementById('greeks');
let d;try{d=await api('/portfolio-greeks')}catch(e){
el.innerHTML='<span class="dim">'+esc(e.message)+' &#8212; greeks need a live Kite session.</span>';return}
if(!d.positions.length){el.innerHTML='<span class="dim">No open option positions.</span>';return}
let h='<table><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>LTP</th><th>&#916;</th><th>Vega</th><th>&#920;/day</th></tr>';
for(const p of d.positions){
h+='<tr><td>'+esc(p.tradingsymbol)+'</td><td>'+esc(p.side)+'</td><td>'+p.quantity+'</td><td>'+
num(p.ltp,2)+'</td><td>'+num(p.delta,3)+'</td><td>'+num(p.vega)+'</td><td>'+num(p.theta_per_day)+'</td></tr>'}
h+='</table>';
const st=Object.entries(d.straddles||{});
if(st.length){h+='<div style="margin-top:8px">'+st.map(([sid,g])=>{
const bad=Math.abs(g.net_delta_per_lot)>=0.2;
return '<span class="chip" style="color:var(--'+(bad?'red':'green')+')">'+esc(g.underlying)+
' straddle &#916; '+(g.net_delta_per_lot>0?'+':'')+g.net_delta_per_lot+'/lot</span>'}).join(' ')+'</div>'}
const t=d.totals||{};
h+='<div class="dim" style="margin-top:8px">Book: &#916; '+num(t.net_delta_units)+' units &#183; vega '+
num(t.net_vega)+' &#183; theta '+inr(t.net_theta_per_day)+'/day</div>';
if(d.margins){h+='<div class="dim" style="margin-top:4px">Margin: '+
Object.entries(d.margins).map(([seg,m])=>seg+' &#8377;'+m.net.toLocaleString('en-IN')+
' free / &#8377;'+m.utilised.toLocaleString('en-IN')+' used').join(' &#183; ')+'</div>'}
el.innerHTML=h}

async function loadJournal(){
const el=document.getElementById('journal');
let d;try{d=await api('/journal')}catch(e){el.innerHTML='<span class="dim">'+esc(e.message)+'</span>';return}
let h='';
const exp=Object.entries(d.expectancy_by_regime||{});
if(exp.length){
h+='<table><tr><th>Regime</th><th>Trades</th><th>Win%</th><th>Mean P&amp;L</th><th>Total</th><th>Worst</th></tr>'+
exp.map(([r,s])=>'<tr><td>'+esc(r)+'</td><td>'+s.trades+'</td><td>'+s.win_rate_pct+'%</td><td>'+
inr(s.mean_pnl)+'</td><td>'+inr(s.total_pnl)+'</td><td>'+inr(s.worst)+'</td></tr>').join('')+'</table>'}
else{h+='<div class="dim">No closed live trades yet &#8212; expectancy appears after the first exits.</div>'}
if(d.entries.length){
h+='<div style="margin-top:10px"><table><tr><th>When</th><th>Ticker</th><th>Mode</th><th>Regime</th>'+
'<th>VRP</th><th>Conf</th><th>Slip</th><th>MAE</th><th>P&amp;L</th></tr>'+
d.entries.slice(0,15).map(e=>{const c=e.entry_context||{};
return '<tr><td>'+new Date(e.entered_at).toLocaleDateString('en-IN',{day:'numeric',month:'short'})+
'</td><td>'+esc(e.commodity)+'</td><td>'+esc(e.mode)+'</td><td>'+esc(c.regime||'&#8212;')+'</td><td>'+
(c.vrp_pts==null?'&#8212;':(c.vrp_pts>0?'+':'')+c.vrp_pts)+'</td><td>'+
(c.judge_confidence==null?'&#8212;':Math.round(c.judge_confidence*100)+'%')+'</td><td>'+
(e.slippage_pct==null?'&#8212;':(e.slippage_pct>0?'+':'')+e.slippage_pct.toFixed(1)+'%')+'</td><td>'+
(e.mae_pct==null?'&#8212;':e.mae_pct.toFixed(0)+'%')+'</td><td>'+
(e.realized_pnl==null?'<span class="dim">open</span>':inr(e.realized_pnl))+'</td></tr>'}).join('')+
'</table></div>'}
else{h+='<div class="dim" style="margin-top:6px">No approvals journaled yet.</div>'}
el.innerHTML=h}

async function loadCalib(){
const el=document.getElementById('calib');
let d;try{d=await api('/calibration')}catch(e){el.innerHTML='<span class="dim">'+esc(e.message)+'</span>';return}
const j=d.judge||{};
let h='';
if(!j.actionable_recs&&!(d.no_trade&&(d.no_trade.avoided||d.no_trade.missed))){
h='<div class="dim">No scored recommendations yet &#8212; outcomes are evaluated one session after each run.</div>';
el.innerHTML=h;return}
h+='<div>Judge: <b>'+(j.win_rate_pct==null?'&#8212;':j.win_rate_pct+'% win</b> over '+j.actionable_recs+
' actionable calls')+'</div>';
if(j.avg_confidence_on_wins!=null||j.avg_confidence_on_losses!=null){
h+='<div class="dim">avg confidence on wins '+num(j.avg_confidence_on_wins,2)+' vs losses '+
num(j.avg_confidence_on_losses,2)+' &#8212; wins should be higher for a calibrated judge</div>'}
if(d.no_trade){h+='<div style="margin-top:6px">NO-TRADE calls: '+d.no_trade.avoided+
' avoided real danger &#183; '+d.no_trade.missed+' missed a calm market</div>'}
const rel=d.risk_flag_reliability||{};
const rows=[];
for(const agent of ['trend','event','vol']){
for(const flag of ['low','medium','high']){
const b=(rel[agent]||{})[flag];
if(b)rows.push('<tr><td>'+agent+'</td><td>'+flag+'</td><td>'+b.n+'</td><td>'+b.danger_rate_pct+'%</td></tr>')}}
if(rows.length){
h+='<div style="margin-top:10px"><table><tr><th>Agent</th><th>Flag</th><th>N</th><th>Danger rate</th></tr>'+
rows.join('')+'</table><div class="dim" style="margin-top:4px">A reliable agent: danger rate rises with the flag '+
'(high-flag runs should see danger far more often than low-flag runs).</div></div>'}
el.innerHTML=h}

function loadAll(){loadGreeks();loadJournal();loadCalib()}
loadAll();
setInterval(loadAll,60000);
if('serviceWorker' in navigator)navigator.serviceWorker.register('/commodity-agents/sw.js');
</script>
</body></html>"""

MANIFEST_JSON = {
    "name": "Commodity Agents",
    "short_name": "Agents",
    "start_url": "/commodity-agents/dashboard",
    "display": "standalone",
    "background_color": "#0d1117",
    "theme_color": "#0d1117",
    "icons": [{
        "src": ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
                "viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='20' "
                "fill='%230d1117'/%3E%3Ctext x='50' y='68' font-size='52' "
                "text-anchor='middle'%3E%E2%9A%96%3C/text%3E%3C/svg%3E"),
        "sizes": "any", "type": "image/svg+xml", "purpose": "any",
    }],
}

# Minimal service worker: enough for PWA installability; network-first so the
# dashboard never shows stale recommendations.
SERVICE_WORKER_JS = """self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>self.clients.claim());
self.addEventListener('fetch',e=>{});
"""
