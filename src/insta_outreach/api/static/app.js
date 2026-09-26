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
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
// "2026-09-21 13:36 IST" -> "21 Sep 13:36"
function when(local) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})/.exec(local || '');
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[4]}` : (local || '');
}
const hhmm = (local) => (/(\d{2}:\d{2})/.exec(local || '') || [])[1] || '';
const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
function store(key, value) { try { localStorage.setItem(key, value); } catch { /* private mode */ } }
function recall(key) { try { return localStorage.getItem(key); } catch { return null; } }

const S = {
  overview: null, cursor: null, filter: 'all', tech: recall('io_tech') === '1', tab: 'approvals', leadFilter: '',
  auditKind: '', failures: 0, tabLoadedAt: 0, pendingTabRefresh: false, blobUrls: [], preflightAt: 0, unseen: 0,
};

// ------------------------------------------------------------ plain words
const STOP_REASON = {
  CHECKPOINT_REQUIRED: "Instagram asked to confirm it's you",
  LOGIN_REQUIRED: 'Instagram logged the account out',
  ACCOUNT_RESTRICTED: 'Instagram restricted the account',
  HUMAN_ACTION_REQUIRED: 'Instagram is asking for something only you can do',
  UI_CHANGED: "Instagram's page looked different than expected",
  RATE_LIMITED: "Instagram said 'try again later'",
};
function laneWhy(reason) {
  const text = String(reason || '');
  const code = text.split(':')[0].trim();
  if (STOP_REASON[code]) return STOP_REASON[code];
  return text.startsWith('rate limited') ? STOP_REASON.RATE_LIMITED : (text || 'stopped by hand');
}
const LANE_NAME = {API: 'Official API', BROWSER: 'Browser'};
const laneName = (ch) => LANE_NAME[ch] || ch;
const LANE_JOB = {
  API: "replies and comment replies, within Meta's rules",
  BROWSER: 'first messages, searches and profile checks',
};
const LANE_STATE = {ACTIVE: ['good', 'working'], COOLDOWN: ['warning', 'resting'], HALTED: ['critical', 'stopped: needs you']};
const LEAD_STATUS = {
  DISCOVERED: 'found, not checked yet', ANALYZED: 'checked: no clear need', QUALIFIED: 'good fit',
  DISQUALIFIED: 'not a fit', DUPLICATE: 'duplicate', OUTREACH_PENDING: 'message queued', CONTACTED: 'messaged',
  REPLIED: 'replied', HANDED_OFF: 'with you', CLOSED: 'closed', UNREACHABLE: "can't be messaged",
};
const MODE_NOW = {
  OBSERVE: 'Watching only: finding and checking businesses. Nothing is written or sent.',
  DRAFT: 'Writing messages for you to read. Nothing is sent.',
  APPROVAL: 'Sending only the messages you approve.',
  AUTONOMOUS: 'Sending on its own, within your limits.',
};
const MESSAGE_KIND = {SEND_OUTREACH: 'first message', SEND_FOLLOW_UP: 'follow-up', SEND_REPLY: 'reply'};
// The gate's reasons for holding a message back, in plain words.
function plainWait(reason) {
  const r = String(reason || ''); let m;
  if (r.startsWith('lane ') && r.includes('halted')) return 'you to fix the Instagram problem and press Resume';
  if (r === 'outside send hours') return 'sending hours to start';
  if (r === 'outside browser active hours') return "the browser's active hours";
  if (r === 'all lanes cooling down') return "Instagram's slow-down to end";
  if ((m = /^daily (outreach|follow-up) cap (\d+) reached$/.exec(r))) return `tomorrow (today's limit of ${m[2]} ${m[1] === 'outreach' ? 'first messages' : 'follow-ups'} is reached)`;
  if ((m = /^hourly (outreach|reply) cap (\d+) reached$/.exec(r))) return `the next hour (limit: ${m[2]} per hour)`;
  if (r === 'hourly browser action cap reached') return 'the next hour (browser limit reached)';
  return r;
}

// -------------------------------------------------------------------- API
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

// ------------------------------------------------------------- right now
function renderNow(o) {
  const u = o.usage; const f = o.funnel;
  const halted = o.lanes.filter((l) => l.configured && l.state === 'HALTED');
  let tone = 'good'; let label = 'Running'; let text = MODE_NOW[o.mode] || o.mode;
  if (o.paused) {
    tone = 'warning'; label = 'Paused'; text = 'Paused: nothing happens until you press Resume.';
  } else if (halted.length) {
    tone = 'critical'; label = 'Needs you';
    text = `${laneWhy(halted[0].reason)}. The ${laneName(halted[0].channel).toLowerCase()} is stopped until you fix it and press Resume.`;
  }
  $('nowMark').replaceChildren(status(tone, label));
  $('nowText').textContent = text;
  const lines = [];
  if (o.simulated) lines.push('This is a simulation: made-up businesses, no real Instagram, nothing leaves your computer.');
  if (o.mode === 'APPROVAL' || o.mode === 'AUTONOMOUS') {
    lines.push(`Today: ${fmt(u.outreach_today)} of ${fmt(u.outreach_per_day)} first messages and ${plural(u.followups_today, 'follow-up', 'follow-ups')} sent.`);
    lines.push(u.in_send_hours
      ? `Sending hours ${u.send_hours}: open now.` + (u.next_send_earliest_local ? ` Next message not before ${hhmm(u.next_send_earliest_local)}: messages are spaced out on purpose.` : '')
      : `Outside sending hours (${u.send_hours}). Sending starts again ${when(u.next_send_window_local)}.`);
  }
  if (f.waiting_approval && o.mode !== 'OBSERVE') lines.push(`${plural(f.waiting_approval, 'message is', 'messages are')} waiting for your approval (see below).`);
  const waits = o.pipeline.gate.waiting_for;
  if (f.queued && waits.length) lines.push(`${plural(f.queued, 'approved message is', 'approved messages are')} queued, waiting for ${[...new Set(waits.map((w) => plainWait(w.reason)))].join(' and ')}.`);
  for (const l of o.lanes.filter((x) => x.configured && x.state === 'COOLDOWN')) {
    lines.push(`The ${laneName(l.channel).toLowerCase()} is resting until ${when(l.until_local)} because Instagram asked to slow down.`);
  }
  if (!u.in_browser_hours) lines.push(`The browser is resting outside its hours (${u.browser_hours}).`);
  $('nowLines').replaceChildren(...lines.map((t) => h('li', {}, t)));
  $('now').className = 'card now ' + tone;
}

// ---------------------------------------------------------------- funnel
const NODES = [
  {id: 'discover', label: 'Found', unit: 'businesses found', tab: ['leads', ''], value: (f) => f.found,
    subs: (f) => [['still being checked', f.checking]]},
  {id: 'fit', label: 'Good fit', unit: 'worth a message', tab: ['leads', 'QUALIFIED'], value: (f) => f.good_fit,
    subs: (f) => [['not a fit', f.not_fit], ['no clear need', f.no_need], ['duplicates', f.duplicates]]},
  {id: 'send', label: 'Messaged', unit: 'businesses messaged', tab: ['leads', 'CONTACTED'], lanes: true, value: (f) => f.messaged,
    subs: (f, o) => [['sent today', o.pipeline.send.sent_today], ['waiting for your approval', f.waiting_approval], ['queued', f.queued]]},
  {id: 'conversation', label: 'Replied', unit: 'businesses replied', tab: ['conversations'], value: (f) => f.replied,
    subs: (f, o) => [['said no / closed', o.pipeline.conversation.closed]]},
  {id: 'you', label: 'With you', unit: 'chats handed to you', tab: ['conversations'], value: (f) => f.with_you, subs: () => []},
];
const NODE_ALIAS = {analyze: 'fit', draft: 'send', gate: 'send'};
function buildFlow() {
  const flow = $('flow');
  NODES.forEach((n, i) => {
    if (i) flow.append(h('div', {class: 'edge', id: 'edge-' + n.id}, h('span', {class: 'dot'})));
    flow.append(h('div', {class: 'node', id: 'node-' + n.id, role: 'button', tabindex: '0', title: 'Show the list',
      onclick: () => openTab(...n.tab), onkeydown: (e) => { if (e.key === 'Enter') openTab(...n.tab); }},
      h('div', {class: 'label'}, n.label), h('div', {class: 'value num'}, '–'), h('div', {class: 'unit'}, n.unit),
      h('div', {class: 'subs'}), n.lanes ? h('div', {class: 'lanechips'}) : null));
  });
}
function renderFlow(o) {
  for (const n of NODES) {
    const node = $('node-' + n.id);
    node.querySelector('.value').textContent = fmt(n.value(o.funnel));
    node.querySelector('.subs').replaceChildren(...n.subs(o.funnel, o).filter(([, v]) => v).map(([k, v]) => h('div', {class: 'sub'}, k + ' ', h('b', {class: 'num'}, fmt(v)))));
    if (n.lanes) {
      node.querySelector('.lanechips').replaceChildren(...o.lanes.filter((l) => l.configured).map((l) => status(LANE_STATE[l.state][0], laneName(l.channel) + ' ' + LANE_STATE[l.state][1].split(':')[0])));
    }
  }
  $('node-send').classList.toggle('alert', o.lanes.some((l) => l.configured && l.state === 'HALTED'));
  $('node-you').classList.toggle('attn', o.funnel.with_you > 0);
}
function pulse(nodeId) {
  const id = NODE_ALIAS[nodeId] || nodeId;
  const node = $('node-' + id); if (!node) return;
  node.classList.remove('pulse'); void node.offsetWidth; node.classList.add('pulse');
  const edge = $('edge-' + id);
  if (edge) { edge.classList.remove('flowing'); void edge.offsetWidth; edge.classList.add('flowing'); }
}

// ------------------------------------------------------ header and strip
function renderHeader(o) {
  const env = $('env');
  env.textContent = o.simulated ? 'SIMULATION: no real Instagram' : 'LIVE: real Instagram';
  env.className = 'env ' + (o.simulated ? 'sim' : 'live');
  $('account').textContent = '@' + o.account;
  for (const b of $('modes').querySelectorAll('button')) b.classList.toggle('on', b.dataset.mode === o.mode);
  const pause = $('pause');
  pause.textContent = o.paused ? 'Resume' : 'Pause all';
  pause.classList.toggle('on', o.paused);
  $('clock').textContent = o.now_local + (o.simulated ? ' (simulated)' : '');
}
function renderAttention(o) {
  const a = o.attention; const items = [];
  if (o.paused) items.push(h('span', {class: 'item warning'}, status('warning', 'Everything is paused'), h('button', {onclick: togglePause}, 'Resume')));
  for (const ch of a.halted_lanes) {
    const lane = o.lanes.find((l) => l.channel === ch);
    items.push(h('span', {class: 'item critical'}, status('critical', `Instagram needs you: the ${laneName(ch).toLowerCase()} is stopped`),
      h('span', {class: 'small'}, laneWhy(lane && lane.reason)), h('button', {onclick: () => resumeLane(ch)}, 'Resume…')));
  }
  if (a.open_incidents && !a.halted_lanes.length) {
    items.push(h('span', {class: 'item'}, status('warning', plural(a.open_incidents, 'problem', 'problems') + ' to look at'), h('a', {class: 'link', onclick: () => openTab('incidents')}, 'Open')));
  }
  if (a.pending_approvals && o.mode !== 'OBSERVE') {
    items.push(h('span', {class: 'item warning'}, status('warning', plural(a.pending_approvals, 'message', 'messages') + ' waiting for your approval'), h('a', {class: 'link', onclick: () => openTab('approvals')}, 'Review')));
  }
  if (a.human_owned) {
    items.push(h('span', {class: 'item'}, status('info', plural(a.human_owned, 'chat', 'chats') + ' handed to you'), h('a', {class: 'link', onclick: () => openTab('conversations')}, 'Open')));
  }
  $('attention').replaceChildren(...items); $('attention').hidden = !items.length;
  $('c-approvals').textContent = a.pending_approvals ? '(' + a.pending_approvals + ')' : '';
  $('c-incidents').textContent = a.open_incidents ? '(' + a.open_incidents + ')' : '';
  $('c-conversations').textContent = a.human_owned ? '(' + a.human_owned + ' yours)' : '';
}

// ---------------------------------------------------------- side panels
function meter(label, used, cap) {
  const ratio = cap ? Math.min(1, used / cap) : 0;
  const tone = ratio >= 1 ? 'critical' : ratio >= 0.8 ? 'warning' : '';
  const fill = h('div', {class: 'fill'}); fill.style.width = (ratio * 100).toFixed(1) + '%';
  return h('div', {class: 'meter ' + tone, role: 'meter', 'aria-valuenow': String(used), 'aria-valuemax': String(cap), 'aria-label': label},
    h('div', {class: 'row'}, h('span', {}, label), h('span', {class: 'num'}, fmt(used) + ' / ' + fmt(cap) + (ratio >= 1 ? ' · limit reached' : ''))),
    h('div', {class: 'track'}, fill));
}
function renderUsage(o) {
  const u = o.usage;
  $('meters').replaceChildren(
    meter('First messages today', u.outreach_today, u.outreach_per_day),
    meter('First messages this hour', u.outreach_last_hour, u.outreach_per_hour),
    meter('Follow-ups today', u.followups_today, u.followups_per_day),
    meter('Browser page views this hour', u.browser_units_last_hour, u.browser_units_per_hour),
    meter('Profiles checked today', u.inspections_today, u.profile_inspections_per_day));
}
function renderLanes(o) {
  $('lanes').replaceChildren(...o.lanes.map((l) => {
    const [tone, word] = l.configured ? LANE_STATE[l.state] : ['neutral', 'off'];
    return h('div', {class: 'lane'},
      h('div', {class: 'row'}, h('strong', {}, laneName(l.channel)), status(tone, word)),
      h('div', {class: 'small muted'}, LANE_JOB[l.channel] || ''),
      l.configured && l.state !== 'ACTIVE' && l.reason ? h('div', {class: 'small'}, laneWhy(l.reason)) : null,
      l.until_local ? h('div', {class: 'small muted'}, 'until ' + when(l.until_local)) : null,
      l.configured ? h('div', {class: 'row'},
        l.state !== 'ACTIVE' ? h('button', {onclick: () => resumeLane(l.channel)}, 'Resume…') : null,
        l.state === 'ACTIVE' ? h('button', {class: 'danger', onclick: () => haltLane(l.channel)}, 'Stop') : null) : null);
  }));
}
function resumeLane(ch) {
  const name = laneName(ch);
  const note = prompt(`Resume the ${name.toLowerCase()}?\n\nOnly do this after you fixed the problem yourself, e.g. confirmed it's you in the Instagram app.\n\nNote for the history:`, 'fixed it on my phone');
  if (note !== null) act(() => post(`/api/lanes/${ch}/resume`, {note}), name + ' resumed');
}
function haltLane(ch) {
  const name = laneName(ch);
  const reason = prompt(`Stop the ${name.toLowerCase()} now? It stays stopped until you resume it.\n\nReason (for the history):`, 'stopped by hand');
  if (reason !== null) act(() => post(`/api/lanes/${ch}/halt`, {reason}), name + ' stopped');
}
function togglePause() {
  const paused = S.overview && S.overview.paused;
  if (paused && !confirm('Resume everything?')) return;
  act(() => post('/api/pause', {paused: !paused}), paused ? 'Resumed' : 'Everything is paused');
}
async function pollOverview() {
  const o = await api('/api/overview');
  S.overview = o; renderHeader(o); renderNow(o); renderFlow(o); renderAttention(o); renderUsage(o); renderLanes(o);
}
async function pollPreflight() {
  const checks = await api('/api/preflight');
  S.preflightAt = Date.now();
  const tone = {PASS: 'good', FAIL: 'critical', WARN: 'warning'};
  $('preflight').replaceChildren(...checks.map((c) => h('div', {class: 'pf'}, status(tone[c.result] || 'neutral', c.result), h('span', {}, h('strong', {}, c.check + ' '), c.detail))));
}

// ----------------------------------------------------------------- feed
const GROUPS = {
  messages: (k) => k === 'message.sent' || k.startsWith('send.') || k.startsWith('action.'),
  replies: (k) => /^(reply\.|conversation\.|suppression\.)/.test(k),
  problems: (k) => /^(incident\.|lane\.|send\.|mode\.refused|browser\.)/.test(k),
};
const ICON = {good: '●', warning: '▲', critical: '■', info: '●', neutral: '○'};
function feedVisible(li) {
  const kind = li.dataset.kind;
  if (!kind) return true;
  if (!S.tech && li.dataset.important !== '1') return false;
  return S.filter === 'all' || Boolean(GROUPS[S.filter] && GROUPS[S.filter](kind));
}
function withLink(text, handle) {
  if (!handle) return [text];
  const at = '@' + handle; const i = text.indexOf(at);
  const link = h('a', {class: 'link', onclick: () => openLead(handle)}, at);
  return i < 0 ? [text + ' ', link] : [text.slice(0, i), link, text.slice(i + at.length)];
}
function feedItem(it, fresh) {
  const by = it.actor && it.source === 'audit' && it.actor !== 'system' ? ' · by ' + it.actor : '';
  const li = h('li', {class: fresh ? 'new' : '', dataset: {kind: it.kind, important: it.important ? '1' : '0', summary: it.summary, source: it.source}},
    h('span', {class: 't'}, when(it.at_local)),
    h('span', {class: 'i ' + it.tone, title: it.tone}, ICON[it.tone] || '●'),
    h('span', {},
      h('span', {class: 'plain'}, ...withLink(it.title || it.summary, it.handle)),
      h('span', {class: 'tech'}, ...withLink(it.summary, it.handle), h('span', {class: 'k'}, it.kind + by)),
      h('span', {class: 'repeat'})));
  li.hidden = !feedVisible(li);
  if (fresh) li.addEventListener('animationend', () => li.classList.remove('new'), {once: true});
  return li;
}
// Routine agent work that repeats unchanged (inbox reads every few minutes) is
// folded into the row above it with a counter instead of flooding the list.
function foldRepeat(it) {
  const top = $('feed').firstElementChild;
  if (it.source !== 'exec' || it.tone !== 'neutral' || !top || top.dataset.source !== 'exec' || top.dataset.summary !== it.summary) return false;
  const n = Number(top.dataset.repeat || 1) + 1; top.dataset.repeat = String(n);
  top.querySelector('.t').textContent = when(it.at_local);
  top.querySelector('.repeat').textContent = ' ×' + n;
  return true;
}
function updateEmpty() {
  const feed = $('feed');
  feed.querySelectorAll('li.empty').forEach((e) => e.remove());
  if (![...feed.children].some((li) => !li.hidden)) {
    feed.append(h('li', {class: 'empty'}, S.tech || S.filter !== 'all' ? 'Nothing here yet.' : 'Nothing yet. Things appear here as they happen.'));
  }
}
function updatePill() {
  const pill = $('newPill');
  pill.hidden = S.unseen <= 0;
  pill.textContent = `↑ ${plural(S.unseen, 'new event', 'new events')}`;
}
function addFeed(items, fresh) {
  const feed = $('feed'); const pulsed = new Set();
  // Reading older events? Keep them in place instead of letting new ones push them away.
  const reading = fresh && feed.scrollTop > 8; const before = feed.scrollHeight; let shown = 0;
  for (const it of items) {
    if (!foldRepeat(it)) {
      const li = feedItem(it, fresh); feed.prepend(li);
      if (!li.hidden) shown += 1;
    }
    if (fresh && it.node && (it.important || S.tech) && !pulsed.has(it.node)) { pulsed.add(it.node); pulse(it.node); }
    if (fresh && /^(action\.|message\.sent|reply\.|conversation\.|incident\.|lane\.)/.test(it.kind)) S.pendingTabRefresh = true;
  }
  updateEmpty();
  if (reading) { feed.scrollTop += feed.scrollHeight - before; S.unseen += shown; updatePill(); }
  while (feed.children.length > 600) feed.lastChild.remove();  // after the correction above, so it can't skew it
}
async function pollFeed() {
  if (!S.cursor) {
    const r = await api('/api/feed?limit=300'); S.cursor = r.cursor; $('feed').replaceChildren(); addFeed(r.items, false); return;
  }
  const r = await api(`/api/feed?audit=${S.cursor.audit}&attempt=${S.cursor.attempt}&limit=300`);
  S.cursor = r.cursor; if (r.items.length) addFeed(r.items, true);
}
function refilter() {
  for (const li of $('feed').children) if (li.dataset.kind) li.hidden = !feedVisible(li);
  updateEmpty();
}
function setFilter(f) {
  S.filter = f;
  for (const b of $('feedFilters').querySelectorAll('button')) b.classList.toggle('on', b.dataset.filter === f);
  refilter();
}
function setTech(on) {
  S.tech = on; store('io_tech', on ? '1' : '0');
  $('techToggle').checked = on; $('feed').classList.toggle('tech', on);
  refilter();
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
  if (!items.length) return h('div', {class: 'empty'}, 'Nothing is waiting for your approval.');
  const note = S.overview && S.overview.mode === 'DRAFT' ? h('p', {class: 'small muted'}, 'Draft mode: approved messages wait until you switch to Approval or Autonomous.') : null;
  return [note, ...items.map((a) => {
    const text = h('textarea', {'aria-label': 'Message text'}, a.message || '');
    const edited = () => (text.value.trim() !== (a.message || '').trim() ? text.value.trim() : undefined);
    const kind = a.type === 'SEND_OUTREACH' && a.capability === 'private_reply' ? 'private reply to their comment' : (MESSAGE_KIND[a.type] || a.type);
    return h('div', {class: 'item'},
      h('div', {class: 'row'}, h('a', {class: 'link', onclick: () => openLead(a.target_username)}, '@' + a.target_username),
        h('span', {class: 'tag'}, kind), a.status === 'DRAFTED' ? status('neutral', 'draft') : status('warning', 'waiting for you'),
        h('span', {class: 'small muted'}, 'written by ' + (a.composer === 'template' ? 'a template' : (a.composer || '?')))),
      a.facts_used && a.facts_used.length ? h('div', {class: 'small'}, 'Based on: ', ...a.facts_used.map((f) => h('span', {class: 'tag'}, f.replace(/_/g, ' ')))) : null,
      text,
      h('div', {class: 'row'},
        h('button', {class: 'primary', onclick: () => act(() => post(`/api/actions/${a.id}/approve`, {edited_text: edited()}), edited() ? 'Approved with your edit' : 'Approved')}, 'Approve'),
        h('button', {onclick: () => { const reason = prompt('What should change? (kept in the history)', 'tone'); if (reason !== null) act(() => post(`/api/actions/${a.id}/reject`, {reason, redraft: true}), 'Rejected: a new version will be written'); }}, 'Reject & rewrite'),
        h('button', {class: 'danger', onclick: () => { const reason = prompt("Reject and don't message this business? Reason:", 'not a fit'); if (reason !== null) act(() => post(`/api/actions/${a.id}/reject`, {reason, redraft: false}), 'Rejected'); }}, "Reject, don't message")));
  })];
}
async function evidenceImg(path) {
  const r = await call('/api/evidence/' + encodeURIComponent(path).replace(/%2F/g, '/'));
  const url = URL.createObjectURL(await r.blob()); S.blobUrls.push(url);
  const img = h('img', {alt: 'screenshot of what Instagram showed', title: path}); img.src = url;
  img.addEventListener('click', () => window.open(url, '_blank'));
  return img;
}
async function tabIncidents() {
  const items = await api('/api/incidents?all=true');
  S.blobUrls.forEach((u) => URL.revokeObjectURL(u)); S.blobUrls = [];
  if (!items.length) return h('div', {class: 'empty'}, "No problems. If Instagram ever asks to confirm it's you or to slow down, it shows up here with a screenshot.");
  const tone = {CRITICAL: 'critical', WARNING: 'warning', INFO: 'info'};
  return Promise.all(items.map(async (i) => {
    const shots = await Promise.all((i.evidence || []).filter((e) => e.kind === 'screenshot' && e.path).slice(0, 3).map((e) => evidenceImg(e.path).catch(() => h('span', {class: 'small muted'}, 'screenshot unavailable'))));
    const open = i.status === 'OPEN';
    return h('div', {class: 'item'},
      h('div', {class: 'row'}, status(open ? tone[i.severity] : 'good', open ? 'needs you' : 'fixed'), h('strong', {}, STOP_REASON[i.kind] || i.title)),
      h('div', {class: 'small muted'}, when((i.created_at || '').replace('T', ' ')) + ' UTC · ' + (i.channel ? laneName(i.channel) : '') + ' · #' + i.id),
      i.detail ? h('div', {class: 'small'}, i.detail) : null,
      i.page_url ? h('div', {class: 'small muted'}, 'page: ' + i.page_url) : null,
      shots.length ? h('div', {class: 'shots'}, shots) : null,
      i.resolved_at ? h('div', {class: 'small muted'}, 'fixed by ' + i.resolved_by + (i.resolution_note ? ': ' + i.resolution_note : '')) : null,
      open && i.channel ? h('div', {class: 'row'}, h('button', {onclick: () => resumeLane(i.channel)}, `I fixed it on Instagram: resume the ${laneName(i.channel).toLowerCase()}…`)) : null);
  }));
}
async function tabConversations() {
  const items = await api('/api/conversations');
  if (!items.length) return h('div', {class: 'empty'}, 'No chats yet.');
  items.sort((a, b) => (b.automation_paused - a.automation_paused) || (a.id - b.id));
  return h('table', {}, h('thead', {}, h('tr', {}, ['Who', 'Handled by', 'Why', ''].map((c) => h('th', {}, c)))),
    h('tbody', {}, items.map((c) => h('tr', {},
      h('td', {}, h('a', {class: 'link', onclick: () => openLead(c.peer_username)}, '@' + (c.peer_username || c.peer_igsid))),
      h('td', {}, c.automation_paused ? status('warning', 'you') : status('good', 'the bot')),
      h('td', {class: 'small'}, c.paused_reason || ''),
      h('td', {}, c.automation_paused
        ? h('button', {onclick: () => act(() => post(`/api/conversations/${c.id}/release`), 'Handed back to the bot')}, 'Hand back to the bot')
        : h('button', {onclick: () => act(() => post(`/api/conversations/${c.id}/claim`), 'This chat is now yours')}, 'Take over'))))));
}
async function tabLeads() {
  const select = h('select', {'aria-label': 'Filter by status', onchange: (e) => { S.leadFilter = e.target.value; loadTab(); }},
    ['', ...Object.keys(LEAD_STATUS)].map((s) => h('option', {value: s, selected: s === S.leadFilter}, s ? LEAD_STATUS[s] : 'all businesses')));
  const q = S.leadFilter ? '&status=' + S.leadFilter : '';
  const items = await api('/api/leads?limit=500' + q);
  const table = h('table', {}, h('thead', {}, h('tr', {}, ['Business', 'Score', 'Status', 'Why'].map((c, i) => h('th', {class: i === 1 ? 'num' : ''}, c)))),
    h('tbody', {}, items.map((l) => h('tr', {},
      h('td', {}, h('a', {class: 'link', onclick: () => openLead(l.username)}, '@' + l.username), h('div', {class: 'small muted'}, l.full_name || '')),
      h('td', {class: 'num'}, l.score ?? '–'), h('td', {}, LEAD_STATUS[l.status] || l.status),
      h('td', {class: 'small'}, l.status_reason || '', h('div', {}, (l.opportunities || []).map((o) => h('span', {class: 'tag', title: o.rationale}, o.type.toLowerCase().replace(/_/g, ' ')))))))));
  return [h('div', {class: 'row'}, select, h('span', {class: 'small muted'}, plural(items.length, 'business', 'businesses'))), items.length ? table : h('div', {class: 'empty'}, 'None.')];
}
async function tabAudit() {
  const input = h('input', {placeholder: 'filter by event type, e.g. message.sent, lane, mode', value: S.auditKind, 'aria-label': 'Filter by event type'});
  input.addEventListener('change', () => { S.auditKind = input.value.trim(); loadTab(); });
  const items = await api('/api/audit?limit=300' + (S.auditKind ? '&kind=' + encodeURIComponent(S.auditKind) : ''));
  return [h('div', {class: 'row'}, input, h('span', {class: 'small muted'}, 'every recorded event, newest first; nothing is ever edited or deleted')),
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
      h('div', {}, h('div', {class: 'row'}, h('strong', {}, lead.full_name || handle), h('span', {class: 'tag'}, LEAD_STATUS[lead.status] || lead.status), h('span', {class: 'small muted'}, 'score ' + (lead.score ?? '–'))),
        h('div', {class: 'small'}, lead.status_reason || ''),
        h('div', {}, (lead.opportunities || []).map((o) => h('div', {class: 'small'}, '• ' + o.rationale)))),
      h('div', {}, h('h2', {}, 'Chat'), msgs.length
        ? h('div', {class: 'bubbles'}, msgs.map((m) => h('div', {class: 'bubble ' + (m.direction === 'OUTBOUND' ? 'out' : 'in')}, m.text,
            h('div', {class: 'meta'}, [m.sender, m.state, m.intent, (m.sent_at || '').slice(0, 16).replace('T', ' ') + ' UTC'].filter(Boolean).join(' · ')))))
        : h('div', {class: 'empty'}, 'No messages yet.')),
      h('div', {class: 'row'},
        h('button', {onclick: () => act(() => post('/api/conversations/' + encodeURIComponent('@' + handle) + '/claim'), 'This chat is now yours')}, 'Take over this chat'),
        h('button', {class: 'danger', onclick: () => { const reason = prompt('Never contact @' + handle + '? Reason:', 'asked in person'); if (reason !== null) act(() => post('/api/suppressions', {kind: 'USERNAME', value: handle, reason}), 'Added to the never-contact list'); }}, 'Never contact')),
      h('details', {class: 'trail-box'}, h('summary', {}, 'Why the bot did what it did (step by step)'), h('pre', {class: 'trail'}, explain.lines.join('\n'))));
  } catch (e) { body.replaceChildren(h('div', {class: 'empty'}, String(e.message || e))); }
}

// ------------------------------------------------------------ controls
function bindControls() {
  for (const b of $('modes').querySelectorAll('button')) {
    b.addEventListener('click', () => {
      const mode = b.dataset.mode; const o = S.overview; if (!o || o.mode === mode) return;
      let confirmFlag = false;
      if (mode === 'AUTONOMOUS' && !o.simulated) {
        if (!confirm('Switch the REAL account to Autonomous?\n\nMessages will be sent automatically within every limit. It is refused unless every go-live check passes.')) return;
        confirmFlag = true;
      }
      act(() => post('/api/mode', {mode, confirm: confirmFlag}), 'Mode is now ' + b.textContent);
    });
  }
  $('pause').addEventListener('click', togglePause);
  for (const b of $('feedFilters').querySelectorAll('button')) b.addEventListener('click', () => setFilter(b.dataset.filter));
  $('techToggle').addEventListener('change', (e) => setTech(e.target.checked));
  $('newPill').addEventListener('click', () => { $('feed').scrollTo({top: 0, behavior: 'smooth'}); S.unseen = 0; updatePill(); });
  $('feed').addEventListener('scroll', () => { if ($('feed').scrollTop <= 8 && S.unseen) { S.unseen = 0; updatePill(); } });
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
    S.failures = 0; setConn(true);
  } catch (e) {
    S.failures += 1; setConn(false, String(e.message || e).slice(0, 60));
  }
  setTimeout(loop, S.failures ? Math.min(10000, 1500 * S.failures) : 1500);
}

buildFlow();
bindControls();
setTech(S.tech);
loadTab();
loop();
