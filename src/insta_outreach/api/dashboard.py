"""Single-page operator dashboard served at ``/`` by the control plane.

All external text (bios, messages, usernames) is inserted with textContent,
never innerHTML: prospects' content must not be able to script the console.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Outreach Console</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5d6472;--line:#e3e6ea;--accent:#2f5bea;--ok:#177245;--warn:#a15c00;--bad:#b3261e}
@media (prefers-color-scheme: dark){:root{--bg:#111316;--card:#1a1d22;--ink:#e8eaed;--muted:#9aa1ad;--line:#2b3037;--accent:#7c9bff;--ok:#5cc98d;--warn:#f0b35a;--bad:#ff8a80}}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;z-index:2}
header h1{font-size:16px;margin:0 12px 0 0} main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:16px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
h2{font-size:14px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center} .pill{border:1px solid var(--line);border-radius:999px;padding:2px 10px;font-size:12px}
.ACTIVE,.SUCCEEDED{color:var(--ok)} .COOLDOWN,.PENDING_APPROVAL,.DRAFTED{color:var(--warn)} .HALTED,.FAILED,.CRITICAL{color:var(--bad)}
button,select,input{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:5px 10px}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff} button:disabled{opacity:.5}
textarea{width:100%;min-height:92px;font:inherit;border:1px solid var(--line);border-radius:6px;padding:8px;background:var(--bg);color:var(--ink)}
.item{border-top:1px solid var(--line);padding:10px 0} .item:first-of-type{border-top:0} .muted{color:var(--muted)} .small{font-size:12px}
table{width:100%;border-collapse:collapse} td,th{text-align:left;padding:6px 4px;border-top:1px solid var(--line);vertical-align:top}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px} #err{color:var(--bad)}
a{color:var(--accent)}
</style></head><body>
<header>
  <h1>Outreach Console</h1><span class="pill" id="env"></span>
  <label>Mode <select id="mode"><option>OBSERVE</option><option>DRAFT</option><option>APPROVAL</option><option>AUTONOMOUS</option></select></label>
  <button id="applyMode">Apply</button><button id="pause"></button>
  <input id="token" type="password" placeholder="control token" size="14"><button id="saveToken">Save</button>
  <span id="err"></span>
</header>
<main>
  <section><h2>Lanes & today</h2><div class="row" id="lanes"></div><div class="row small muted" id="counters" style="margin-top:8px"></div></section>
  <div class="grid2">
    <section><h2>Needs attention</h2><div id="incidents"></div></section>
    <section><h2>Human-owned conversations</h2><div id="convs"></div></section>
  </div>
  <section><h2>Approval queue</h2><div id="queue"></div></section>
  <section><h2>Leads</h2><div class="row small" style="margin-bottom:6px"><select id="leadFilter"><option value="">all</option><option>QUALIFIED</option><option>OUTREACH_PENDING</option><option>CONTACTED</option><option>REPLIED</option><option>HANDED_OFF</option><option>ANALYZED</option><option>DISQUALIFIED</option></select></div><table id="leads"></table></section>
  <section><h2>Recent actions</h2><table id="actions"></table></section>
</main>
<script>
const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === 'class') node.className = v; else if (k.startsWith('on')) node.addEventListener(k.slice(2), v); else node.setAttribute(k, v); }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(c ?? ''));
  return node;
}
function token() { try { return sessionStorage.getItem('io_token') || ''; } catch { return ''; } }
async function api(path, opts = {}) {
  const headers = {'content-type': 'application/json'}; const t = token(); if (t) headers.authorization = 'Bearer ' + t;
  const r = await fetch(path, {...opts, headers});
  if (!r.ok) { const body = await r.json().catch(() => ({})); throw new Error(body.detail || r.statusText); }
  return r.json();
}
function flash(e) { $('err').textContent = e ? String(e.message || e) : ''; }
async function act(fn) { try { flash(); await fn(); await refresh(); } catch (e) { flash(e); } }

async function refresh() {
  try {
    const s = await api('/api/status'); flash();
    $('env').textContent = s.environment.toUpperCase() + ' · @' + s.account; $('mode').value = s.mode;
    $('pause').textContent = s.global_pause ? 'Resume all' : 'Pause all';
    $('pause').onclick = () => act(() => api('/api/pause', {method: 'POST', body: JSON.stringify({paused: !s.global_pause})}));
    $('lanes').replaceChildren(...s.lanes.map(l => el('span', {class: 'pill ' + l.state}, `${l.channel}: ${l.state}${l.reason ? ' — ' + l.reason : ''} `,
      l.state !== 'ACTIVE' ? el('button', {onclick: () => act(() => api(`/api/lanes/${l.channel}/resume`, {method: 'POST', body: JSON.stringify({note: 'resumed from console'})}))}, 'Resume') : '')));
    $('counters').replaceChildren(el('span', {}, `sent today: ${s.sent_today}`), el('span', {}, `open incidents: ${s.open_incidents}`),
      el('span', {}, `paused conversations: ${s.paused_conversations}`), el('span', {}, 'leads: ' + Object.entries(s.leads).map(([k, v]) => `${k} ${v}`).join(', ')));
    const incidents = await api('/api/incidents');
    $('incidents').replaceChildren(...(incidents.length ? incidents.map(i => el('div', {class: 'item'},
      el('div', {class: i.severity}, `${i.severity} · ${i.title}`), el('div', {class: 'small muted'}, (i.detail || '') + (i.page_url ? ' · ' + i.page_url : '')),
      ...i.evidence.filter(e => e.kind === 'screenshot' && e.path).slice(0, 2).map(e => el('a', {href: '#', onclick: (ev) => { ev.preventDefault(); openEvidence(e.path); }}, 'screenshot ')))) : [el('div', {class: 'muted'}, 'Nothing needs you right now.')]));
    const convs = await api('/api/conversations?paused=true');
    $('convs').replaceChildren(...(convs.length ? convs.map(c => el('div', {class: 'item'},
      el('div', {}, '@' + (c.peer_username || c.peer_igsid)), el('div', {class: 'small muted'}, c.paused_reason || ''),
      el('button', {onclick: () => act(() => api(`/api/conversations/${c.id}/release`, {method: 'POST'}))}, 'Release to automation'))) : [el('div', {class: 'muted'}, 'None.')]));
    const queue = await api('/api/actions?status=PENDING_APPROVAL&status=DRAFTED&limit=50');
    $('queue').replaceChildren(...(queue.length ? queue.map(renderQueueItem) : [el('div', {class: 'muted'}, 'Queue is empty.')]));
    await renderLeads();
    const recent = await api('/api/actions?limit=25');
    $('actions').replaceChildren(el('tr', {}, el('th', {}, 'type'), el('th', {}, 'target'), el('th', {}, 'status'), el('th', {}, 'via'), el('th', {}, 'result')),
      ...recent.map(a => el('tr', {}, el('td', {}, a.type), el('td', {}, a.target_username || ''), el('td', {class: a.status}, a.status),
        el('td', {}, a.executed_channel || ''), el('td', {class: 'small muted'}, (a.last_result || '') + ' ' + (a.last_result_code || '')))));
  } catch (e) { flash(e); }
}
function renderQueueItem(a) {
  const box = el('textarea', {}); box.value = a.message || '';
  return el('div', {class: 'item'},
    el('div', {class: 'row'}, el('strong', {}, '@' + (a.target_username || '')), el('span', {class: 'pill ' + a.status}, a.status),
      el('span', {class: 'pill'}, a.type), a.params.opportunity ? el('span', {class: 'pill'}, a.params.opportunity) : '', el('span', {class: 'small muted'}, 'composer: ' + (a.composer || ''))),
    el('div', {class: 'small muted'}, 'facts used: ' + (a.facts_used || []).join(', ')), box,
    el('div', {class: 'row'},
      el('button', {class: 'primary', onclick: () => act(() => api(`/api/actions/${a.id}/approve`, {method: 'POST', body: JSON.stringify({message: box.value})}))}, 'Approve'),
      el('button', {onclick: () => act(() => api(`/api/actions/${a.id}/reject`, {method: 'POST', body: JSON.stringify({reason: 'rejected in console', redraft: true})}))}, 'Reject & redraft'),
      el('button', {onclick: () => act(() => api(`/api/actions/${a.id}/reject`, {method: 'POST', body: JSON.stringify({reason: 'not a fit'})}))}, 'Reject lead')));
}
async function renderLeads() {
  const f = $('leadFilter').value; const leads = await api('/api/leads?limit=60' + (f ? '&status=' + f : ''));
  $('leads').replaceChildren(el('tr', {}, el('th', {}, 'lead'), el('th', {}, 'score'), el('th', {}, 'status'), el('th', {}, 'opportunities'), el('th', {}, 'reason')),
    ...leads.map(l => el('tr', {}, el('td', {}, '@' + l.username, el('div', {class: 'small muted'}, [l.niche, l.location, l.followers ? l.followers + ' followers' : ''].filter(Boolean).join(' · '))),
      el('td', {}, l.score ?? ''), el('td', {class: l.status}, l.status), el('td', {class: 'small'}, (l.opportunities || []).map(o => o.type).join(', ')),
      el('td', {class: 'small muted'}, l.status_reason || ''))));
}
async function openEvidence(path) {
  const headers = {}; const t = token(); if (t) headers.authorization = 'Bearer ' + t;
  const r = await fetch('/api/evidence/' + encodeURIComponent(path), {headers});
  if (!r.ok) { flash('evidence not available'); return; }
  window.open(URL.createObjectURL(await r.blob()), '_blank');
}
$('applyMode').onclick = () => act(async () => {
  const mode = $('mode').value; let confirm = false;
  if (mode === 'AUTONOMOUS') confirm = window.confirm('Send eligible outreach automatically within limits?');
  if (mode === 'AUTONOMOUS' && !confirm) return;
  await api('/api/mode', {method: 'POST', body: JSON.stringify({mode, confirm})});
});
$('saveToken').onclick = () => { try { sessionStorage.setItem('io_token', $('token').value); } catch {} refresh(); };
$('leadFilter').onchange = () => renderLeads().catch(flash);
refresh(); setInterval(refresh, 15000);
</script></body></html>
"""
