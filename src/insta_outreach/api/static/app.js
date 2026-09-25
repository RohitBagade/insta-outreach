// Mission Control. Security rule: every piece of external text (bios, DMs,
// usernames, reasons) is inserted with textContent, never as HTML.
'use strict';

const $ = (id) => document.getElementById(id);
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === undefined || c === null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const status = (tone, label) => h('span', {class: 'st ' + tone}, label);
const fmt = (n) => (n === undefined || n === null ? '–' : Number(n).toLocaleString('en-IN'));

const S = {
  overview: null, cursor: null, filter: 'all', tab: 'approvals', leadFilter: '', auditKind: '',
  lastOk: 0, failures: 0, tabLoadedAt: 0, pendingTabRefresh: false, blobUrls: [], preflightAt: 0,
};

// ------------------------------------------------------------------ API
function token() { try { return sessionStorage.getItem('io_token') || ''; } catch { return ''; } }
function setToken(t) { try { sessionStorage.setItem('io_token', t); } catch { /* private mode */ } }
async function call(path, opts = {}) {
  const headers = {'content-type': 'application/json'};
  const t = token(); if (t) headers.authorization = 'Bearer ' + t;
  const r = await fetch(path, {...opts, headers});
  if (r.status === 401) { askToken(); throw new Error('control token required'); }
  if (!r.ok) { const body = await r.json().catch(() => ({})); throw new Error(body.detail || r.statusText); }
  return r;
}
const api = async (path, opts) => (await call(path, opts)).json();
const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});

let tokenAsked = false;
function askToken() {
  if (tokenAsked) return; tokenAsked = true;
  const dlg = $('tokenDialog');
  dlg.addEventListener('close', () => { const v = $('tokenInput').value.trim(); if (v) setToken(v); tokenAsked = false; refreshAll(); }, {once: true});
  dlg.showModal();
}
let toastTimer;
function toast(msg, error = false) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast' + (error ? ' error' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, error ? 9000 : 3500);
}
async function act(fn, ok) {
  try { await fn(); if (ok) toast(ok); await refreshAll(); } catch (e) { toast(String(e.message || e), true); }
}

// ------------------------------------------------------------ workflow
const NODES = [
  {id: 'discover', label: 'Discover', unit: 'leads found', tab: ['leads', 'DISCOVERED'],
    value: (p) => p.discover.total, subs: (p) => [['waiting to inspect', p.discover.waiting]]},
  {id: 'analyze', label: 'Analyze', unit: 'inspected', tab: ['leads', 'DISQUALIFIED'],
    value: (p) => p.analyze.analyzed,
    subs: (p) => [['disqualified', p.analyze.disqualified], ['duplicates', p.analyze.duplicate], ['no opportunity', p.analyze.not_qualified]]},
  {id: 'qualify', label: 'Qualified', unit: 'ready for a message', tab: ['leads', 'QUALIFIED'],
    value: (p) => p.qualify.qualified, subs: () => []},
  {id: 'draft', label: 'Draft', unit: 'messages waiting', tab: ['approvals'],
    value: (p) => p.draft.drafted + p.draft.pending_approval,
    subs: (p) => [['need your approval', p.draft.pending_approval], ['drafts (DRAFT mode)', p.draft.drafted]]},
  {id: 'gate', label: 'Gate', unit: 'approved, queued', tab: ['leads', 'OUTREACH_PENDING'],
    value: (p) => p.gate.queued,
    subs: (p) => [...p.gate.waiting_for.map((w) => ['waiting: ' + w.reason, w.count]), ['blocked', p.gate.blocked], ['parked for you', p.gate.parked]]},
  {id: 'send', label: 'Send', unit: 'sent today', tab: ['audit', 'message.sent'], lanes: true,
    value: (p) => p.send.sent_today, subs: (p) => [['sent in total', p.send.sent_total], ['failed today', p.send.failed_today]]},
  {id: 'conversation', label: 'Conversations', unit: 'waiting for a reply', tab: ['conversations'],
    value: (p) => p.conversation.contacted,
    subs: (p) => [['replied', p.conversation.replied], ['with you (handed off)', p.conversation.handed_off], ['closed', p.conversation.closed], ['suppressed', p.conversation.suppressed]]},
];
function buildFlow() {
  const flow = $('flow');
  NODES.forEach((n, i) => {
    if (i) flow.append(h('div', {class: 'edge', id: 'edge-' + n.id}, h('span', {class: 'dot'})));
    flow.append(h('div', {class: 'node', id: 'node-' + n.id, role: 'button', tabindex: '0', title: 'Open details',
      onclick: () => openTab(...n.tab), onkeydown: (e) => { if (e.key === 'Enter') openTab(...n.tab); }},
      h('div', {class: 'label'}, n.label), h('div', {class: 'value num'}, '–'), h('div', {class: 'unit'}, n.unit),
      h('div', {class: 'subs'}), n.lanes ? h('div', {class: 'lanechips'}) : null));
  });
}
const LANE_TONE = {ACTIVE: 'good', COOLDOWN: 'warning', HALTED: 'critical'};
function renderFlow(o) {
  const p = o.pipeline;
  for (const n of NODES) {
    const node = $('node-' + n.id);
    node.querySelector('.value').textContent = fmt(n.value(p));
    node.querySelector('.subs').replaceChildren(...n.subs(p).filter(([, v]) => v).map(([k, v]) => h('div', {class: 'sub'}, k + ' ', h('b', {class: 'num'}, fmt(v)))));
    if (n.lanes) {
      node.querySelector('.lanechips').replaceChildren(...o.lanes.map((l) => status(l.configured ? LANE_TONE[l.state] : 'neutral', l.channel + (l.configured ? ' ' + l.state.toLowerCase() : ' off'))));
    }
  }
  $('node-send').classList.toggle('alert', o.lanes.some((l) => l.configured && l.state === 'HALTED'));
  $('node-gate').classList.toggle('alert', p.gate.parked > 0);
  $('node-draft').classList.toggle('alert', p.draft.pending_approval > 0 && o.mode !== 'DRAFT');
  $('flowNote').textContent = p.gate.next_attempt_local ? 'next queued attempt: ' + p.gate.next_attempt_local : '';
}
function pulse(nodeId) {
  const node = $('node-' + nodeId); if (!node) return;
  node.classList.remove('pulse'); void node.offsetWidth; node.classList.add('pulse');
  const edge = $('edge-' + nodeId);
  if (edge) { edge.classList.remove('flowing'); void edge.offsetWidth; edge.classList.add('flowing'); }
}

// ------------------------------------------------------------- overview
function renderHeader(o) {
  const env = $('env');
  env.textContent = o.simulated ? 'SIMULATION: no real Instagram' : 'LIVE: real Instagram';
  env.className = 'env ' + (o.simulated ? 'sim' : 'live');
  $('account').textContent = '@' + o.account;
  for (const b of $('modes').querySelectorAll('button')) b.classList.toggle('on', b.dataset.mode === o.mode);
  const pause = $('pause');
  pause.textContent = o.paused ? 'PAUSED: resume' : 'Pause all';
  pause.classList.toggle('on', o.paused);
  $('clock').textContent = o.now_local + (o.simulated ? ' (simulated)' : '');
}
function renderAttention(o) {
  const a = o.attention; const items = [];
  for (const ch of a.halted_lanes) {
    items.push(h('span', {class: 'item critical'}, status('critical', ch + ' lane halted'), h('span', {class: 'small'}, 'a human must resolve it on Instagram first'),
      h('button', {onclick: () => resumeLane(ch)}, 'Resume…')));
  }
  if (a.critical_incidents) items.push(h('span', {class: 'item critical'}, status('critical', a.critical_incidents + ' critical incident(s)'), h('a', {class: 'link', onclick: () => openTab('incidents')}, 'open')));
  if (a.pending_approvals && o.mode !== 'OBSERVE') items.push(h('span', {class: 'item warning'}, status('warning', a.pending_approvals + ' message(s) waiting for approval'), h('a', {class: 'link', onclick: () => openTab('approvals')}, 'review')));
  if (a.human_owned) items.push(h('span', {class: 'item'}, status('info', a.human_owned + ' conversation(s) are yours'), h('a', {class: 'link', onclick: () => openTab('conversations')}, 'view')));
  if (o.paused) items.unshift(h('span', {class: 'item critical'}, status('critical', 'Global pause is ON: nothing is executed')));
  $('attention').replaceChildren(...items); $('attention').hidden = !items.length;
  $('c-approvals').textContent = a.pending_approvals ? '(' + a.pending_approvals + ')' : '';
  $('c-incidents').textContent = a.open_incidents ? '(' + a.open_incidents + ')' : '';
  $('c-conversations').textContent = a.human_owned ? '(' + a.human_owned + ' yours)' : '';
}
function meter(label, used, cap) {
  const ratio = cap ? Math.min(1, used / cap) : 0;
  const tone = ratio >= 1 ? 'critical' : ratio >= 0.8 ? 'warning' : '';
  const fill = h('div', {class: 'fill'}); fill.style.width = (ratio * 100).toFixed(1) + '%';
  return h('div', {class: 'meter ' + tone, role: 'meter', 'aria-valuenow': String(used), 'aria-valuemax': String(cap), 'aria-label': label},
    h('div', {class: 'row'}, h('span', {}, label), h('span', {class: 'num'}, fmt(used) + ' / ' + fmt(cap) + (ratio >= 1 ? ' · at cap' : ''))),
    h('div', {class: 'track'}, fill));
}
function renderUsage(o) {
  const u = o.usage;
  $('meters').replaceChildren(
    meter('New conversations today', u.outreach_today, u.outreach_per_day),
    meter('New conversations this hour', u.outreach_last_hour, u.outreach_per_hour),
    meter('Follow-ups today', u.followups_today, u.followups_per_day),
    meter('Browser page views this hour', u.browser_units_last_hour, u.browser_units_per_hour),
    meter('Profile inspections today', u.inspections_today, u.profile_inspections_per_day));
  const lines = [u.in_send_hours ? `Sending allowed now (${u.send_hours}).` : `Outside send hours (${u.send_hours}); next window ${u.next_send_window_local}.`];
  if (u.next_send_earliest_local) lines.push(`Pacing: next send not before ${u.next_send_earliest_local} (+ random 0–${u.send_jitter_seconds}s).`);
  if (!u.in_browser_hours) lines.push(`Browser is resting (active ${u.browser_hours}).`);
  $('sendWindow').textContent = lines.join(' ');
}
function renderLanes(o) {
  $('lanes').replaceChildren(...o.lanes.map((l) => h('div', {class: 'lane'},
    h('div', {class: 'row'}, h('strong', {}, l.channel === 'API' ? 'Official API' : 'Browser agent'),
      l.configured ? status(LANE_TONE[l.state], l.state) : status('neutral', 'not configured')),
    l.reason ? h('div', {class: 'small'}, l.reason) : null,
    l.until_local ? h('div', {class: 'small muted'}, 'until ' + l.until_local) : null,
    l.configured ? h('div', {class: 'row'},
      l.state !== 'ACTIVE' ? h('button', {onclick: () => resumeLane(l.channel)}, 'Resume…') : null,
      l.state === 'ACTIVE' ? h('button', {class: 'danger', onclick: () => haltLane(l.channel)}, 'Halt') : null) : null)));
}
function resumeLane(ch) {
  const note = prompt(`Resume the ${ch} lane?\n\nOnly do this after you resolved the problem yourself (e.g. completed the security check in the Instagram app). Note for the audit trail:`, 'resolved on my phone');
  if (note !== null) act(() => post(`/api/lanes/${ch}/resume`, {note}), ch + ' lane resumed');
}
function haltLane(ch) {
  const reason = prompt(`Halt the ${ch} lane now? Reason for the audit trail:`, 'manual stop');
  if (reason !== null) act(() => post(`/api/lanes/${ch}/halt`, {reason}), ch + ' lane halted');
}
async function pollOverview() {
  const o = await api('/api/overview');
  S.overview = o; renderHeader(o); renderFlow(o); renderAttention(o); renderUsage(o); renderLanes(o);
}
async function pollPreflight() {
  const checks = await api('/api/preflight');
  S.preflightAt = Date.now();
  const tone = {PASS: 'good', FAIL: 'critical', WARN: 'warning'};
  $('preflight').replaceChildren(...checks.map((c) => h('div', {class: 'pf'}, status(tone[c.result] || 'neutral', c.result), h('span', {}, h('strong', {}, c.check + ' '), c.detail))));
}

// ----------------------------------------------------------------- feed
const GROUPS = {
  sends: (k) => k === 'message.sent' || k.startsWith('send.'),
  decisions: (k) => /^(action\.|mode\.|limits\.|pause\.|lead\.added)/.test(k),
  people: (k) => /^(reply\.|conversation\.|suppression\.)/.test(k),
  incidents: (k) => /^(incident\.|lane\.|browser\.)/.test(k),
  agent: (k) => k.startsWith('exec.'),
};
const ICON = {good: '●', warning: '▲', critical: '■', info: '●', neutral: '○'};
function feedVisible(kind) { return S.filter === 'all' || (GROUPS[S.filter] && GROUPS[S.filter](kind)); }
function feedItem(it, fresh) {
  const text = h('span', {}, it.summary);
  const li = h('li', {class: fresh ? 'new' : '', dataset: {kind: it.kind, summary: it.summary, source: it.source}},
    h('span', {class: 't'}, (it.at_local || '').slice(5, 16)),
    h('span', {class: 'i ' + it.tone, title: it.tone}, ICON[it.tone] || '●'),
    h('span', {}, text, it.handle ? h('a', {class: 'link small', onclick: () => openLead(it.handle)}, ' @' + it.handle) : null,
      h('span', {class: 'k'}, it.kind + (it.actor && it.source === 'audit' && it.actor !== 'system' ? ' · by ' + it.actor : '')),
      h('span', {class: 'repeat'})));
  li.hidden = !feedVisible(it.kind);
  return li;
}
// Routine agent work that repeats unchanged (inbox reads every few minutes) is
// folded into the row above it with a counter instead of flooding the feed.
function foldRepeat(it) {
  const top = $('feed').firstElementChild;
  if (it.source !== 'exec' || it.tone !== 'neutral' || !top || top.dataset.source !== 'exec' || top.dataset.summary !== it.summary) return false;
  const n = Number(top.dataset.repeat || 1) + 1; top.dataset.repeat = String(n);
  top.querySelector('.t').textContent = (it.at_local || '').slice(5, 16);
  top.querySelector('.repeat').textContent = ' ×' + n;
  return true;
}
function addFeed(items, fresh) {
  const feed = $('feed'); const pulsed = new Set();
  for (const it of items) {
    if (!foldRepeat(it)) feed.prepend(feedItem(it, fresh));
    if (fresh && it.node && !pulsed.has(it.node)) { pulsed.add(it.node); pulse(it.node); }
    if (fresh && /^(action\.|message\.sent|reply\.|conversation\.|incident\.|lane\.)/.test(it.kind)) S.pendingTabRefresh = true;
  }
  while (feed.children.length > 400) feed.lastChild.remove();
  if (!feed.children.length) feed.append(h('li', {class: 'empty'}, 'No activity yet.'));
  else feed.querySelectorAll('li.empty').forEach((e) => e.remove());
}
async function pollFeed() {
  if (!S.cursor) {
    const r = await api('/api/feed?limit=150'); S.cursor = r.cursor; $('feed').replaceChildren(); addFeed(r.items, false); return;
  }
  const r = await api(`/api/feed?audit=${S.cursor.audit}&attempt=${S.cursor.attempt}&limit=200`);
  S.cursor = r.cursor; if (r.items.length) addFeed(r.items, true);
}
function setFilter(f) {
  S.filter = f;
  for (const b of $('feedFilters').querySelectorAll('button')) b.classList.toggle('on', b.dataset.filter === f);
  for (const li of $('feed').children) if (li.dataset.kind) li.hidden = !feedVisible(li.dataset.kind);
}

// ----------------------------------------------------------------- tabs
function openTab(tab, arg) {
  S.tab = tab;
  if (tab === 'leads') S.leadFilter = arg || '';
  if (tab === 'audit') S.auditKind = arg || '';
  for (const b of $('tabs').querySelectorAll('button')) b.classList.toggle('on', b.dataset.tab === tab);
  loadTab();
  $('tabs').scrollIntoView({behavior: 'smooth', block: 'start'});
}
async function loadTab() {
  S.tabLoadedAt = Date.now(); S.pendingTabRefresh = false;
  const body = $('tab');
  try {
    const render = {approvals: tabApprovals, incidents: tabIncidents, conversations: tabConversations, leads: tabLeads, audit: tabAudit}[S.tab];
    const content = await render();
    if (content) body.replaceChildren(...[].concat(content));
  } catch (e) { body.replaceChildren(h('div', {class: 'empty'}, String(e.message || e))); }
}
async function tabApprovals() {
  if (document.activeElement && document.activeElement.tagName === 'TEXTAREA' && $('tab').contains(document.activeElement)) return null; // don't clobber an edit
  const items = await api('/api/actions?status=PENDING_APPROVAL&status=DRAFTED&limit=100');
  if (!items.length) return h('div', {class: 'empty'}, 'Nothing waiting for approval.');
  const note = S.overview && S.overview.mode === 'DRAFT' ? h('p', {class: 'small muted'}, 'Mode is DRAFT: approved messages stay queued until you switch to APPROVAL or AUTONOMOUS.') : null;
  return [note, ...items.map((a) => {
    const text = h('textarea', {}, a.message || '');
    const edited = () => (text.value.trim() !== (a.message || '').trim() ? text.value.trim() : undefined);
    const gate = (a.gate || {}).proposal;
    return h('div', {class: 'item'},
      h('div', {class: 'row'}, h('a', {class: 'link', onclick: () => openLead(a.target_username)}, '@' + a.target_username),
        h('span', {class: 'tag'}, a.type === 'SEND_OUTREACH' ? (a.capability === 'private_reply' ? 'first message: private reply to their comment' : 'first message') : a.type.toLowerCase().replace('send_', '')),
        status(a.status === 'DRAFTED' ? 'neutral' : 'warning', a.status), h('span', {class: 'small muted'}, (a.created_at || '').slice(0, 16).replace('T', ' ') + ' UTC · written by ' + (a.composer || '?'))),
      a.facts_used && a.facts_used.length ? h('div', {class: 'small'}, 'facts used: ', ...a.facts_used.map((f) => h('span', {class: 'tag'}, f))) : null,
      gate ? h('div', {class: 'small muted'}, 'gate at proposal: ' + gate.outcome + ' - ' + gate.reasons.join('; ')) : null,
      text,
      h('div', {class: 'row'},
        h('button', {class: 'primary', onclick: () => act(() => post(`/api/actions/${a.id}/approve`, {edited_text: edited()}), edited() ? 'Approved with your edit' : 'Approved')}, 'Approve'),
        h('button', {onclick: () => { const reason = prompt('Why reject? (audit trail)', 'tone'); if (reason !== null) act(() => post(`/api/actions/${a.id}/reject`, {reason, redraft: true}), 'Rejected; a fresh draft will be prepared'); }}, 'Reject & redraft'),
        h('button', {class: 'danger', onclick: () => { const reason = prompt('Reject and never message this lead? Reason:', 'not a fit'); if (reason !== null) act(() => post(`/api/actions/${a.id}/reject`, {reason, redraft: false}), 'Rejected'); }}, 'Reject')));
  })];
}
async function evidenceImg(path) {
  const r = await call('/api/evidence/' + encodeURIComponent(path).replace(/%2F/g, '/'));
  const url = URL.createObjectURL(await r.blob()); S.blobUrls.push(url);
  const img = h('img', {alt: 'evidence screenshot', title: path}); img.src = url;
  img.addEventListener('click', () => window.open(url, '_blank'));
  return img;
}
async function tabIncidents() {
  const items = await api('/api/incidents?all=true');
  S.blobUrls.forEach((u) => URL.revokeObjectURL(u)); S.blobUrls = [];
  if (!items.length) return h('div', {class: 'empty'}, 'No incidents. Checkpoints, rate limits and restrictions would appear here with screenshots.');
  const tone = {CRITICAL: 'critical', WARNING: 'warning', INFO: 'info'};
  return Promise.all(items.map(async (i) => {
    const shots = await Promise.all((i.evidence || []).filter((e) => e.kind === 'screenshot' && e.path).slice(0, 3).map((e) => evidenceImg(e.path).catch(() => h('span', {class: 'small muted'}, 'screenshot unavailable'))));
    return h('div', {class: 'item'},
      h('div', {class: 'row'}, status(i.status === 'OPEN' ? tone[i.severity] : 'neutral', i.status === 'OPEN' ? i.severity : 'RESOLVED'), h('strong', {}, i.title)),
      h('div', {class: 'small muted'}, '#' + i.id + ' · ' + i.kind + ' · ' + (i.created_at || '').slice(0, 16).replace('T', ' ') + ' UTC'),
      i.detail ? h('div', {class: 'small'}, i.detail) : null,
      i.page_url ? h('div', {class: 'small'}, 'page: ' + i.page_url) : null,
      shots.length ? h('div', {class: 'shots'}, shots) : null,
      i.resolved_at ? h('div', {class: 'small muted'}, 'resolved by ' + i.resolved_by + ': ' + (i.resolution_note || '')) : null,
      i.status === 'OPEN' && i.channel ? h('div', {class: 'row'}, h('button', {onclick: () => resumeLane(i.channel)}, 'Resolved on Instagram: resume ' + i.channel + ' lane…')) : null);
  }));
}
async function tabConversations() {
  const items = await api('/api/conversations');
  if (!items.length) return h('div', {class: 'empty'}, 'No conversations yet.');
  items.sort((a, b) => (b.automation_paused - a.automation_paused) || (a.id - b.id));
  return h('table', {}, h('thead', {}, h('tr', {}, ['Who', 'Owner', 'Automation', 'Why', ''].map((c) => h('th', {}, c)))),
    h('tbody', {}, items.map((c) => h('tr', {},
      h('td', {}, h('a', {class: 'link', onclick: () => openLead(c.peer_username)}, '@' + (c.peer_username || c.peer_igsid))),
      h('td', {}, c.owner), h('td', {}, c.automation_paused ? status('warning', 'paused (yours)') : status('good', 'active')),
      h('td', {class: 'small'}, c.paused_reason || ''),
      h('td', {}, c.automation_paused
        ? h('button', {onclick: () => act(() => post(`/api/conversations/${c.id}/release`), 'Handed back to automation')}, 'Hand back')
        : h('button', {onclick: () => act(() => post(`/api/conversations/${c.id}/claim`), 'Conversation is now yours')}, 'Take over'))))));
}
async function tabLeads() {
  const select = h('select', {onchange: (e) => { S.leadFilter = e.target.value; loadTab(); }},
    ['', 'DISCOVERED', 'QUALIFIED', 'OUTREACH_PENDING', 'CONTACTED', 'REPLIED', 'HANDED_OFF', 'CLOSED', 'ANALYZED', 'DISQUALIFIED', 'DUPLICATE', 'UNREACHABLE']
      .map((s) => h('option', {value: s, selected: s === S.leadFilter}, s || 'all statuses')));
  const q = S.leadFilter ? '&status=' + S.leadFilter : '';
  const items = await api('/api/leads?limit=500' + q);
  const table = h('table', {}, h('thead', {}, h('tr', {}, ['Lead', 'Score', 'Status', 'Opportunities', 'Why'].map((c, i) => h('th', {class: i === 1 ? 'num' : ''}, c)))),
    h('tbody', {}, items.map((l) => h('tr', {},
      h('td', {}, h('a', {class: 'link', onclick: () => openLead(l.username)}, '@' + l.username), h('div', {class: 'small muted'}, l.full_name || '')),
      h('td', {class: 'num'}, l.score ?? '–'), h('td', {}, l.status),
      h('td', {}, (l.opportunities || []).map((o) => h('span', {class: 'tag', title: o.rationale}, o.type))),
      h('td', {class: 'small'}, l.status_reason || '')))));
  return [h('div', {class: 'row'}, select, h('span', {class: 'small muted'}, items.length + ' lead(s)')), items.length ? table : h('div', {class: 'empty'}, 'None.')];
}
async function tabAudit() {
  const input = h('input', {placeholder: 'event prefix, e.g. message.sent, lane, mode', value: S.auditKind});
  input.addEventListener('change', () => { S.auditKind = input.value.trim(); loadTab(); });
  const items = await api('/api/audit?limit=300' + (S.auditKind ? '&kind=' + encodeURIComponent(S.auditKind) : ''));
  return [h('div', {class: 'row'}, input, h('span', {class: 'small muted'}, 'append-only; newest first')),
    h('table', {}, h('thead', {}, h('tr', {}, ['Time (UTC)', 'Who', 'Event', 'Subject', 'What'].map((c) => h('th', {}, c)))),
      h('tbody', {}, items.map((e) => h('tr', {},
        h('td', {class: 'small num'}, (e.at || '').slice(0, 16).replace('T', ' ')), h('td', {class: 'small'}, e.actor),
        h('td', {class: 'small'}, e.kind),
        h('td', {class: 'small'}, e.subject && e.subject.startsWith('@') ? h('a', {class: 'link', onclick: () => openLead(e.subject.slice(1))}, e.subject) : (e.subject || '')),
        h('td', {}, e.summary)))))];
}

// --------------------------------------------------------------- drawer
async function openLead(handle) {
  if (!handle) return;
  handle = String(handle).replace(/^@/, '');
  $('drawerTitle').textContent = '@' + handle; $('drawer').hidden = false;
  const body = $('drawerBody'); body.replaceChildren(h('div', {class: 'empty'}, 'Loading…'));
  try {
    const [lead, explain, msgs] = await Promise.all([
      api('/api/leads/' + encodeURIComponent(handle)),
      api('/api/leads/' + encodeURIComponent(handle) + '/explain'),
      api('/api/conversations/' + encodeURIComponent('@' + handle) + '/messages').catch(() => []),
    ]);
    body.replaceChildren(
      h('div', {}, h('div', {class: 'row'}, h('strong', {}, lead.full_name || handle), h('span', {class: 'tag'}, lead.status), h('span', {class: 'small muted'}, 'score ' + (lead.score ?? '–'))),
        h('div', {class: 'small'}, lead.status_reason || ''),
        h('div', {}, (lead.opportunities || []).map((o) => h('span', {class: 'tag', title: o.rationale}, o.type + ': ' + o.rationale)))),
      h('div', {}, h('h2', {}, 'Conversation'), msgs.length
        ? h('div', {class: 'bubbles'}, msgs.map((m) => h('div', {class: 'bubble ' + (m.direction === 'OUTBOUND' ? 'out' : 'in')}, m.text,
            h('div', {class: 'meta'}, [m.sender, m.state, m.intent, (m.sent_at || '').slice(0, 16).replace('T', ' ') + ' UTC'].filter(Boolean).join(' · ')))))
        : h('div', {class: 'empty'}, 'No messages.')),
      h('div', {}, h('h2', {}, 'Decision trail'), h('pre', {class: 'trail'}, explain.lines.join('\n'))),
      h('div', {class: 'row'},
        h('button', {onclick: () => act(() => post('/api/conversations/' + encodeURIComponent('@' + handle) + '/claim'), 'Conversation is now yours')}, 'Take over'),
        h('button', {class: 'danger', onclick: () => { const reason = prompt('Never contact @' + handle + '? Reason:', 'asked in person'); if (reason !== null) act(() => post('/api/suppressions', {kind: 'USERNAME', value: handle, reason}), 'Suppressed'); }}, 'Never contact')));
  } catch (e) { body.replaceChildren(h('div', {class: 'empty'}, String(e.message || e))); }
}

// ------------------------------------------------------------ controls
function bindControls() {
  for (const b of $('modes').querySelectorAll('button')) {
    b.addEventListener('click', () => {
      const mode = b.dataset.mode; const o = S.overview; if (!o || o.mode === mode) return;
      let confirmFlag = false;
      if (mode === 'AUTONOMOUS' && !o.simulated) {
        if (!confirm('Switch the LIVE account to AUTONOMOUS?\n\nEligible messages will be sent automatically within every limit. The server refuses unless every go-live check passes.')) return;
        confirmFlag = true;
      }
      act(() => post('/api/mode', {mode, confirm: confirmFlag}), 'Mode is now ' + mode);
    });
  }
  $('pause').addEventListener('click', () => {
    const paused = S.overview && S.overview.paused;
    if (paused && !confirm('Resume all activity?')) return;
    act(() => post('/api/pause', {paused: !paused}), paused ? 'Resumed' : 'Everything is paused');
  });
  for (const b of $('feedFilters').querySelectorAll('button')) b.addEventListener('click', () => setFilter(b.dataset.filter));
  for (const b of $('tabs').querySelectorAll('button')) b.addEventListener('click', () => openTab(b.dataset.tab));
  $('drawerClose').addEventListener('click', () => { $('drawer').hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('drawer').hidden = true; });
}

// ---------------------------------------------------------------- loop
function setConn(ok, msg) {
  const c = $('conn'); c.className = 'conn ' + (ok ? 'ok' : 'bad'); c.textContent = ok ? '● live' : '● ' + (msg || 'reconnecting…');
}
async function refreshAll() {
  await Promise.allSettled([pollOverview(), loadTab(), pollPreflight()]);
}
async function loop() {
  try {
    await pollFeed();
    await pollOverview();
    if (Date.now() - S.preflightAt > 30000) await pollPreflight();
    const stale = Date.now() - S.tabLoadedAt > 15000;
    if ((S.pendingTabRefresh && Date.now() - S.tabLoadedAt > 2500) || stale) await loadTab();
    S.lastOk = Date.now(); S.failures = 0; setConn(true);
  } catch (e) {
    S.failures += 1; setConn(false, String(e.message || e).slice(0, 60));
  }
  setTimeout(loop, S.failures ? Math.min(10000, 1500 * S.failures) : 1500);
}

buildFlow();
bindControls();
loadTab();
loop();
