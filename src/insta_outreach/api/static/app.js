// Mission Control: the shell (navigation, header, live feed, drawer, polling).
// The pages themselves are in pages.js. Security rule: every piece of external
// text (bios, DMs, usernames, reasons) is inserted with textContent, never as HTML.
'use strict';

const $ = (id) => document.getElementById(id);
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k === 'value' && 'value' in node) node.value = v;
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c === undefined || c === null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
// replaceChildren() would print "null" for an empty slot: skip those.
function fill(el, ...kids) { el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false)); return el; }
const SVG_NS = 'http://www.w3.org/2000/svg';
function s(tag, attrs, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null) continue;
    if (k.startsWith('on')) node.addEventListener(k.slice(2), v); else node.setAttribute(k, String(v));
  }
  for (const c of children.flat()) if (c !== undefined && c !== null) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}
// Line icons (24px grid, stroked).
const ICONS = {
  grid: [['rect', {x: 3, y: 3, width: 7, height: 7, rx: 1.5}], ['rect', {x: 14, y: 3, width: 7, height: 7, rx: 1.5}], ['rect', {x: 14, y: 14, width: 7, height: 7, rx: 1.5}], ['rect', {x: 3, y: 14, width: 7, height: 7, rx: 1.5}]],
  check: [['polyline', {points: '9 11 12 14 22 4'}], ['path', {d: 'M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11'}]],
  users: [['path', {d: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2'}], ['circle', {cx: 9, cy: 7, r: 4}], ['path', {d: 'M23 21v-2a4 4 0 0 0-3-3.87'}], ['path', {d: 'M16 3.13a4 4 0 0 1 0 7.75'}]],
  chat: [['path', {d: 'M21 11.5a8.4 8.4 0 0 1-9 8.5 8.5 8.5 0 0 1-3.8-.9L3 21l1.9-5.2A8.5 8.5 0 0 1 12 3a8.4 8.4 0 0 1 9 8.5z'}]],
  target: [['circle', {cx: 12, cy: 12, r: 10}], ['circle', {cx: 12, cy: 12, r: 6}], ['circle', {cx: 12, cy: 12, r: 2}]],
  chart: [['line', {x1: 18, y1: 20, x2: 18, y2: 10}], ['line', {x1: 12, y1: 20, x2: 12, y2: 4}], ['line', {x1: 6, y1: 20, x2: 6, y2: 14}]],
  activity: [['polyline', {points: '22 12 18 12 15 21 9 3 6 12 2 12'}]],
  alert: [['path', {d: 'M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z'}], ['line', {x1: 12, y1: 9, x2: 12, y2: 13}], ['line', {x1: 12, y1: 17, x2: 12.01, y2: 17}]],
  settings: [['line', {x1: 4, y1: 21, x2: 4, y2: 14}], ['line', {x1: 4, y1: 10, x2: 4, y2: 3}], ['line', {x1: 12, y1: 21, x2: 12, y2: 12}], ['line', {x1: 12, y1: 8, x2: 12, y2: 3}], ['line', {x1: 20, y1: 21, x2: 20, y2: 16}], ['line', {x1: 20, y1: 12, x2: 20, y2: 3}], ['line', {x1: 1, y1: 14, x2: 7, y2: 14}], ['line', {x1: 9, y1: 8, x2: 15, y2: 8}], ['line', {x1: 17, y1: 16, x2: 23, y2: 16}]],
  search: [['circle', {cx: 11, cy: 11, r: 8}], ['line', {x1: 21, y1: 21, x2: 16.65, y2: 16.65}]],
  plus: [['line', {x1: 12, y1: 5, x2: 12, y2: 19}], ['line', {x1: 5, y1: 12, x2: 19, y2: 12}]],
  pause: [['rect', {x: 6, y: 4, width: 4, height: 16, rx: 1}], ['rect', {x: 14, y: 4, width: 4, height: 16, rx: 1}]],
  play: [['polygon', {points: '6 3 20 12 6 21 6 3'}]],
  menu: [['line', {x1: 3, y1: 6, x2: 21, y2: 6}], ['line', {x1: 3, y1: 12, x2: 21, y2: 12}], ['line', {x1: 3, y1: 18, x2: 21, y2: 18}]],
  x: [['line', {x1: 18, y1: 6, x2: 6, y2: 18}], ['line', {x1: 6, y1: 6, x2: 18, y2: 18}]],
  send: [['line', {x1: 22, y1: 2, x2: 11, y2: 13}], ['polygon', {points: '22 2 15 22 11 13 2 9 22 2'}]],
  star: [['polygon', {points: '12 2 15.1 8.3 22 9.3 17 14.1 18.2 21 12 17.8 5.8 21 7 14.1 2 9.3 8.9 8.3 12 2'}]],
  heart: [['path', {d: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l1 1.1L12 21l7.8-7.5 1-1.1a5.5 5.5 0 0 0 0-7.8z'}]],
  award: [['circle', {cx: 12, cy: 8, r: 7}], ['polyline', {points: '8.2 13.9 7 23 12 20 17 23 15.8 13.9'}]],
  login: [['path', {d: 'M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4'}], ['polyline', {points: '10 17 15 12 10 7'}], ['line', {x1: 15, y1: 12, x2: 3, y2: 12}]],
  shield: [['path', {d: 'M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z'}]],
  refresh: [['polyline', {points: '23 4 23 10 17 10'}], ['path', {d: 'M20.5 15a9 9 0 1 1-2.1-9.4L23 10'}]],
  external: [['path', {d: 'M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6'}], ['polyline', {points: '15 3 21 3 21 9'}], ['line', {x1: 10, y1: 14, x2: 21, y2: 3}]],
  trash: [['polyline', {points: '3 6 5 6 21 6'}], ['path', {d: 'M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'}]],
  bell: [['path', {d: 'M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9'}], ['path', {d: 'M13.7 21a2 2 0 0 1-3.4 0'}]],
};
function icon(name) {
  const el = s('svg', {class: 'icon', viewBox: '0 0 24 24', 'aria-hidden': 'true', focusable: 'false'});
  for (const [tag, attrs] of ICONS[name] || []) el.append(s(tag, attrs));
  return el;
}
function hydrateIcons(root) { for (const i of root.querySelectorAll('i[data-icon]')) i.replaceWith(icon(i.dataset.icon)); }

const status = (tone, label) => h('span', {class: 'st ' + tone}, label);
const pill = (tone, label, title) => h('span', {class: 'pill ' + (tone || ''), title}, label);
const fmt = (n) => (n === undefined || n === null ? '–' : Number(n).toLocaleString('en-IN'));
const pct = (x) => (x === null || x === undefined ? '–' : `${Math.round(x * 100)}%`);
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
// "2026-09-21 13:36 IST" -> "21 Sep 13:36"
function when(local) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})/.exec(local || '');
  return m ? `${Number(m[3])} ${MONTHS[Number(m[2]) - 1]} ${m[4]}` : (local || '');
}
// ISO (UTC) -> "21 Sep"
function day(iso) { const d = iso ? new Date(iso) : null; return d && !isNaN(d) ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : ''; }
const hhmm = (local) => (/(\d{2}:\d{2})/.exec(local || '') || [])[1] || '';
const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many}`;
const clip = (text, n) => { const t = String(text || '').replace(/\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n - 1) + '…' : t; };
const initials = (name, handle) => (String(name || '').trim().split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('') || String(handle || '?').replace(/^sim\./, '').slice(0, 2));
const avatar = (name, handle, big) => h('span', {class: 'avatar' + (big ? ' big' : ''), 'aria-hidden': 'true'}, initials(name, handle));
function store(key, value) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch { /* private mode */ } }
function recall(key) { try { return localStorage.getItem(key); } catch { return null; } }
const nice = (word) => String(word || '').toLowerCase().replace(/_/g, ' ');
const handleLink = (handle, text) => h('a', {class: 'link', onclick: (e) => { e.stopPropagation(); openLead(handle); }}, text || '@' + handle);

const S = {
  overview: null, dash: null, setup: null, cursor: null, filter: 'all', tech: recall('io_tech') === '1', page: 'dashboard',
  loadedAt: {}, dirty: {}, failures: 0, blobUrls: [], preflight: [], preflightAt: 0, setupAt: 0, unseen: 0, recent: [],
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
  if (text.startsWith('manual: ')) return 'stopped by hand: ' + text.split(': ').slice(2).join(': ');
  return text.startsWith('rate limited') ? STOP_REASON.RATE_LIMITED : (text || 'stopped by hand');
}
const LANE_NAME = {API: 'Official API', BROWSER: 'Browser', RESEARCH: 'Research account'};
const laneName = (ch) => LANE_NAME[ch] || ch;
const LANE_JOB = {
  API: "replies and comment replies, within Meta's rules",
  BROWSER: 'first messages, searches and profile checks',
  RESEARCH: 'a second account that searches and reads profiles, so your brand account only sends',
};
// The research lane is optional: shown only when it is set up.
const shownLanes = (o) => o.lanes.filter((l) => l.channel !== 'RESEARCH' || l.configured);
const researchOn = (o) => o.lanes.some((l) => l.channel === 'RESEARCH' && l.configured);
const laneJob = (o, ch) => (ch === 'BROWSER' && researchOn(o) ? 'first messages and your chats' : LANE_JOB[ch] || '');
const LANE_STATE = {ACTIVE: ['good', 'working'], COOLDOWN: ['warning', 'resting'], HALTED: ['critical', 'stopped: needs you']};
const LEAD_STATUS = {
  DISCOVERED: 'being checked', ANALYZED: 'no clear need', QUALIFIED: 'good fit', DISQUALIFIED: 'not a fit',
  DUPLICATE: 'duplicate', OUTREACH_PENDING: 'message queued', CONTACTED: 'messaged', REPLIED: 'replied',
  HANDED_OFF: 'with you', CLOSED: 'closed', UNREACHABLE: "can't be messaged",
};
const LEAD_TONE = {QUALIFIED: 'info', OUTREACH_PENDING: 'info', CONTACTED: 'good', REPLIED: 'good', HANDED_OFF: 'warning', DISQUALIFIED: '', ANALYZED: '', DUPLICATE: '', CLOSED: '', UNREACHABLE: '', DISCOVERED: ''};
const GOOD_FIT = ['QUALIFIED', 'OUTREACH_PENDING', 'CONTACTED', 'REPLIED', 'HANDED_OFF', 'CLOSED', 'UNREACHABLE'];
const MODE_NOW = {
  OBSERVE: 'Watching only: finding and checking businesses. Nothing is written or sent.',
  DRAFT: 'Writing messages for you to read. Nothing is sent.',
  APPROVAL: 'Sending only the messages you approve.',
  AUTONOMOUS: 'Sending on its own, within your limits.',
};
const MESSAGE_KIND = {SEND_OUTREACH: 'first message', SEND_FOLLOW_UP: 'follow-up', SEND_REPLY: 'reply'};
const ACTION_STATE = {
  SUCCEEDED: ['good', 'sent'], APPROVED: ['info', 'queued'], PENDING_APPROVAL: ['warning', 'waiting for you'],
  DRAFTED: ['', 'draft'], EXECUTING: ['info', 'sending…'], NEEDS_HUMAN: ['critical', 'stopped: needs you'],
  FAILED: ['critical', 'failed'], BLOCKED: ['warning', 'held back'], CANCELLED: ['', 'cancelled'],
  REJECTED: ['', 'rejected'], EXPIRED: ['', 'expired'], PROPOSED: ['', 'planned'],
};
const STAGES = [['INTERESTED', 'Interested'], ['MEETING', 'Meeting booked'], ['PROPOSAL', 'Proposal sent'], ['WON', 'Client'], ['LOST', 'Not now']];
const STAGE_NAME = Object.fromEntries(STAGES);
const STAGE_TONE = {INTERESTED: 'info', MEETING: 'info', PROPOSAL: 'warning', WON: 'good', LOST: ''};
// The gate's reasons for holding a message back, in plain words.
function plainWait(reason) {
  const r = String(reason || ''); let m;
  if (r.includes('not logged in on this computer')) return 'you to log in to Instagram (Settings)';
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
// The token lives in this tab (sessionStorage), or on this computer if asked to.
function token() { try { return sessionStorage.getItem('io_token') || localStorage.getItem('io_token') || ''; } catch { return ''; } }
function setToken(t, remember) {
  try { sessionStorage.setItem('io_token', t); if (remember) localStorage.setItem('io_token', t); } catch { /* private mode */ }
}
function forgetToken() { try { sessionStorage.removeItem('io_token'); localStorage.removeItem('io_token'); } catch { /* private mode */ } }
// `insta-outreach run --open` opens the page with #token=...: keep it, then drop it from the address bar.
function tokenFromLink() {
  const m = /(?:^#|[#&])token=([^&]+)/.exec(location.hash);
  if (!m) return;
  setToken(decodeURIComponent(m[1]), false);
  history.replaceState(null, '', location.pathname + location.search + '#/dashboard');
}
async function call(path, opts = {}) {
  const headers = {'content-type': 'application/json'};
  const t = token(); if (t) headers.authorization = 'Bearer ' + t;
  const r = await fetch(path, {...opts, headers});
  if (r.status === 401) { forgetToken(); askToken(); throw new Error('control token required'); }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = typeof body.detail === 'string' ? body.detail : (Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join('; ') : '');
    throw new Error(detail || r.statusText);
  }
  return r;
}
const api = async (path, opts) => (await call(path, opts)).json();
const send = (method) => (path, body) => api(path, {method, body: JSON.stringify(body || {})});
const post = send('POST'); const patch = send('PATCH'); const put = send('PUT');
const del = (path) => api(path, {method: 'DELETE'});

let tokenAsked = false;
function askToken() {
  if (tokenAsked) return; tokenAsked = true;
  const dlg = $('tokenDialog');
  dlg.addEventListener('close', () => {
    const v = $('tokenInput').value.trim();
    if (v) setToken(v, $('tokenRemember').checked);
    $('tokenInput').value = ''; tokenAsked = false; refreshAll();
  }, {once: true});
  dlg.showModal();
}
let toastTimer;
function toast(msg, error = false) {
  const t = $('toast'); t.textContent = msg; t.className = 'toast' + (error ? ' error' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, error ? 9000 : 4000);
}
async function act(fn, ok) {
  try { const out = await fn(); if (ok) toast(typeof ok === 'function' ? ok(out) : ok); await refreshAll(); return out; } catch (e) { toast(String(e.message || e), true); return undefined; }
}
function busy(button, fn) {
  return async () => { button.disabled = true; try { await fn(); } finally { button.disabled = false; } };
}
function tip(evt, lines) {
  const t = $('tooltip');
  t.replaceChildren(...lines.map((line, i) => (i === 0 ? h('b', {}, line) : h('div', {}, line))));
  t.hidden = false;
  const r = evt.currentTarget && evt.type === 'focus' ? evt.currentTarget.getBoundingClientRect() : null;
  const x = r ? r.right : evt.clientX; const y = r ? r.top : evt.clientY;
  const w = t.offsetWidth; const hgt = t.offsetHeight;
  t.style.left = Math.min(window.innerWidth - w - 8, x + 14) + 'px';
  t.style.top = Math.max(8, Math.min(window.innerHeight - hgt - 8, y - hgt - 10)) + 'px';
}
function untip() { $('tooltip').hidden = true; }

// ------------------------------------------------------------- navigation
const PAGES = {};  // name -> {load, every (ms), onEvent: RegExp}; filled in by pages.js
function pageFromHash() {
  const m = /^#\/([a-z]+)/.exec(location.hash);
  return m && $('page-' + m[1]) ? m[1] : 'dashboard';
}
function hashParams() { return new URLSearchParams((location.hash.split('?')[1]) || ''); }
function showPage() {
  const page = pageFromHash(); const changed = page !== S.page; S.page = page;
  for (const sec of document.querySelectorAll('.page')) sec.hidden = sec.id !== 'page-' + page;
  for (const a of $('navLinks').querySelectorAll('a')) a.classList.toggle('on', a.dataset.page === page);
  const title = $('page-' + page).dataset.title;
  $('pageTitle').textContent = title; document.title = `${title} · Mission Control`;
  closeNav();
  if (changed) window.scrollTo(0, 0);
  loadPage(true);
}
// force: true when the page is opened (everything is drawn again), 'refresh' after an
// action (fresh data, but forms you are editing stay as they are), false on a timer.
async function loadPage(force) {
  const page = S.page; const def = PAGES[page];
  if (!def) return;
  const age = Date.now() - (S.loadedAt[page] || 0);
  const every = typeof def.every === 'function' ? def.every() : def.every;
  if (!force && !(S.dirty[page] && age > 2500) && !(every && age > every)) return;
  S.loadedAt[page] = Date.now(); S.dirty[page] = false;
  try { await def.load(force); } catch (e) { if (force === true) toast(String(e.message || e), true); }
}
function markDirty(kind) {
  for (const [name, def] of Object.entries(PAGES)) if (def.onEvent && def.onEvent.test(kind)) S.dirty[name] = true;
}
function openNav() { $('nav').classList.add('open'); $('scrim').hidden = false; }
function closeNav() { $('nav').classList.remove('open'); if ($('drawer').hidden) $('scrim').hidden = true; }

// ------------------------------------------------------------- right now
function renderNow(o) {
  const u = o.usage; const f = o.funnel;
  const halted = o.lanes.filter((l) => l.configured && l.state === 'HALTED');
  const login = o.lanes.filter((l) => l.needs_login);
  let tone = 'good'; let label = 'Running'; let text = MODE_NOW[o.mode] || o.mode;
  if (o.paused) {
    tone = 'warning'; label = 'Paused'; text = 'Paused: nothing happens until you press Resume.';
  } else if (login.length && !halted.length) {
    tone = 'warning'; label = 'Needs you';
    text = login.some((l) => l.channel === 'BROWSER')
      ? 'Log in to Instagram to start: Settings > Instagram accounts > Log in. Until then the bot leaves the browser alone.'
      : 'Log in to the research account to start finding businesses: Settings > Instagram accounts > Log in.';
  } else if (halted.length) {
    const lane = halted.find((l) => l.channel !== 'RESEARCH') || halted[0];
    label = 'Needs you';
    if (lane.channel === 'RESEARCH') {
      tone = 'warning';
      text = `${laneWhy(lane.reason)} on the research account. Finding new businesses is paused; @${o.account} keeps sending. Fix it and press Resume.`;
    } else {
      tone = 'critical';
      text = `${laneWhy(lane.reason)}. The ${laneName(lane.channel).toLowerCase()} is stopped until you fix it and press Resume.`;
    }
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
  if (f.waiting_approval && o.mode !== 'OBSERVE') lines.push(`${plural(f.waiting_approval, 'message is', 'messages are')} waiting for your approval.`);
  const waits = o.pipeline.gate.waiting_for;
  if (f.queued && waits.length) lines.push(`${plural(f.queued, 'approved message is', 'approved messages are')} queued, waiting for ${[...new Set(waits.map((w) => plainWait(w.reason)))].join(' and ')}.`);
  for (const l of o.lanes.filter((x) => x.configured && x.state === 'COOLDOWN')) {
    lines.push(`The ${laneName(l.channel).toLowerCase()} is resting until ${when(l.until_local)} because Instagram asked to slow down.`);
  }
  if (!u.in_browser_hours) lines.push(`The browser is resting outside its hours (${u.browser_hours}).`);
  $('nowLines').replaceChildren(...lines.map((t) => h('li', {}, t)));
  $('now').className = 'card hero ' + tone;
  $('reviewBtn').hidden = !(f.waiting_approval && o.mode !== 'OBSERVE');
}

// ------------------------------------------------------ header and strip
function renderHeader(o) {
  const env = $('env');
  env.replaceChildren(h('span', {class: 'long'}, o.simulated ? 'SIMULATION: no real Instagram' : 'LIVE: real Instagram'), h('span', {class: 'short'}, o.simulated ? 'SIMULATION' : 'LIVE'));
  env.className = 'env ' + (o.simulated ? 'sim' : 'live');
  $('account').textContent = '@' + o.account;
  for (const b of $('modes').querySelectorAll('button')) b.classList.toggle('on', b.dataset.mode === o.mode);
  const pause = $('pause');
  pause.replaceChildren(icon(o.paused ? 'play' : 'pause'), h('span', {}, o.paused ? 'Resume' : 'Pause all'));
  pause.classList.toggle('on', o.paused);
  $('clock').textContent = o.now_local + (o.simulated ? ' (simulated)' : '');
}
function renderAttention(o) {
  const a = o.attention; const items = [];
  if (o.paused) items.push(h('span', {class: 'item warning'}, status('warning', 'Everything is paused'), h('button', {class: 'small', onclick: togglePause}, 'Resume')));
  for (const ch of a.halted_lanes) {
    const lane = o.lanes.find((l) => l.channel === ch);
    items.push(h('span', {class: 'item critical'}, status('critical', `Instagram needs you: the ${laneName(ch).toLowerCase()} is stopped`),
      h('span', {class: 'small'}, laneWhy(lane && lane.reason)), h('button', {class: 'small', onclick: () => resumeLane(ch)}, 'Resume…')));
  }
  for (const l of o.lanes.filter((x) => x.needs_login)) {
    items.push(h('span', {class: 'item warning'}, status('warning', l.channel === 'RESEARCH' ? 'Log in to the research account' : 'Log in to Instagram to start'),
      h('a', {class: 'link', href: '#/settings'}, 'Settings')));
  }
  if (a.open_incidents && !a.halted_lanes.length) {
    items.push(h('span', {class: 'item'}, status('warning', plural(a.open_incidents, 'problem', 'problems') + ' to look at'), h('a', {class: 'link', href: '#/problems'}, 'Open')));
  }
  if (a.pending_approvals && o.mode !== 'OBSERVE') {
    items.push(h('span', {class: 'item warning'}, status('warning', plural(a.pending_approvals, 'message', 'messages') + ' waiting for your approval'), h('a', {class: 'link', href: '#/approvals'}, 'Review')));
  }
  if (a.human_owned) {
    items.push(h('span', {class: 'item'}, status('info', plural(a.human_owned, 'chat', 'chats') + ' handed to you'), h('a', {class: 'link', href: '#/chats'}, 'Open')));
  }
  $('attention').replaceChildren(...items); $('attention').hidden = !items.length;
  $('b-approvals').textContent = a.pending_approvals || '';
  $('b-chats').textContent = (S.dash && S.dash.awaiting_you) || '';
  $('b-problems').textContent = a.open_incidents || '';
}
function renderRestart() {
  const setup = S.setup; const pending = setup ? setup.pending_restart : [];
  const banner = $('restartBanner');
  banner.hidden = !pending.length;
  if (!pending.length) return;
  const names = pending.map((k) => setup.labels[k] || k).join(', ');
  $('restartText').textContent = `Saved, applies after a restart: ${names}.` + (setup.restart_supported ? '' : ' Close the program and start it again.');
  $('restartBtn').hidden = !setup.restart_supported;
}
async function restartNow() {
  if (!confirm('Restart the program now with the saved settings? It takes a few seconds; this page reconnects by itself.')) return;
  try {
    await post('/api/setup/restart');
    toast('Restarting… this page reconnects by itself.');
  } catch (e) { toast(String(e.message || e), true); }
}

// ------------------------------------------------------------ lanes, meters
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
  $('meters').replaceChildren(...[
    meter('First messages today', u.outreach_today, u.outreach_per_day),
    meter('First messages this hour', u.outreach_last_hour, u.outreach_per_hour),
    meter('Follow-ups today', u.followups_today, u.followups_per_day),
    meter('Browser page views this hour', u.browser_units_last_hour, u.browser_units_per_hour),
    u.research_on ? meter('Research account page views this hour', u.research_units_last_hour, u.browser_units_per_hour) : null,
    meter('Profiles checked today', u.inspections_today, u.profile_inspections_per_day)].filter(Boolean));
}
function renderLanes(o) {
  $('lanes').replaceChildren(...shownLanes(o).map((l) => {
    const [tone, word] = !l.configured ? ['neutral', 'off'] : l.needs_login && l.state !== 'HALTED' ? ['warning', 'log in first'] : LANE_STATE[l.state];
    return h('div', {class: 'lane'},
      h('div', {class: 'lane-head'}, h('strong', {}, laneName(l.channel)), status(tone, word)),
      h('div', {class: 'small muted'}, l.configured ? laneJob(o, l.channel) : (l.channel === 'API' ? 'not connected (optional)' : 'not switched on')),
      l.configured && l.state !== 'ACTIVE' && l.reason ? h('div', {class: 'small'}, laneWhy(l.reason)) : null,
      l.until_local ? h('div', {class: 'small muted'}, 'until ' + when(l.until_local)) : null,
      l.configured ? h('div', {class: 'row'},
        l.state !== 'ACTIVE' ? h('button', {class: 'small', onclick: () => resumeLane(l.channel)}, 'Resume…') : null,
        l.state === 'ACTIVE' ? h('button', {class: 'small danger', onclick: () => haltLane(l.channel)}, 'Stop') : null) : null);
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
async function findNow() {
  const out = await act(() => post('/api/discovery/run'), (r) => r.message);
  if (out) S.dirty.dashboard = true;
}
async function pollOverview() {
  const o = await api('/api/overview');
  const restarted = S.overview && S.overview.started !== o.started;
  if (S.overview && S.overview.environment !== o.environment) { S.cursor = null; S.recent = []; }  // another database
  S.overview = o; renderHeader(o); renderAttention(o); renderNow(o); renderLanes(o); renderUsage(o);
  if (typeof renderProgress === 'function') renderProgress();
  if (restarted) {  // everything may differ now (another Instagram, other accounts): draw the page again
    toast('Restarted with the saved settings');
    S.loadedAt = {}; S.settingsBuilt = false; S.campaigns = null;
    await Promise.allSettled([pollSetup(), pollPreflight()]);
    await loadPage(true);
  }
}
async function pollSetup() {
  S.setup = await api('/api/setup'); S.setupAt = Date.now(); renderRestart();
}
async function pollPreflight() {
  S.preflight = await api('/api/preflight'); S.preflightAt = Date.now();
  const failing = S.preflight.filter((c) => c.result === 'FAIL').length;
  $('b-settings').textContent = S.overview && !S.overview.simulated && failing ? failing : '';
}

// ----------------------------------------------------------------- feed
const GROUPS = {
  messages: (k) => k === 'message.sent' || k.startsWith('send.') || k.startsWith('action.'),
  replies: (k) => /^(reply\.|conversation\.|suppression\.|lead\.stage)/.test(k),
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
  const link = handleLink(handle, at);
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
  const pillEl = $('newPill');
  pillEl.hidden = S.unseen <= 0;
  pillEl.textContent = `↑ ${plural(S.unseen, 'new event', 'new events')}`;
}
const KPI_BY_NODE = {discover: 'found', analyze: 'good_fit', send: 'messaged', gate: 'messaged', conversation: 'replied'};
function pulse(nodeId) {
  const card = $('kpi-' + (KPI_BY_NODE[nodeId] || ''));
  if (!card) return;
  card.classList.remove('pulse'); void card.offsetWidth; card.classList.add('pulse');
}
function renderRecent() {
  const list = $('recent');
  if (!S.recent.length) { list.replaceChildren(h('li', {class: 'empty'}, 'Nothing yet. Things appear here as they happen.')); return; }
  list.replaceChildren(...S.recent.slice(0, 8).map((it) => h('li', {},
    h('span', {class: 'i ' + it.tone}, ICON[it.tone] || '●'),
    h('span', {}, ...withLink(it.title || it.summary, it.handle)),
    h('span', {class: 't'}, when(it.at_local)))));
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
    if (it.important) S.recent.unshift(it);
    if (fresh && it.node && (it.important || S.tech) && !pulsed.has(it.node)) { pulsed.add(it.node); pulse(it.node); }
    if (fresh) markDirty(it.kind);
  }
  S.recent.length = Math.min(S.recent.length, 20);
  renderRecent();
  updateEmpty();
  if (reading) { feed.scrollTop += feed.scrollHeight - before; S.unseen += shown; updatePill(); }
  while (feed.children.length > 600) feed.lastChild.remove();  // after the correction above, so it can't skew it
}
async function pollFeed() {
  if (!S.cursor) {
    const r = await api('/api/feed?limit=300'); S.cursor = r.cursor; $('feed').replaceChildren(); S.recent = []; addFeed(r.items, false); return;
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

// --------------------------------------------------------------- drawer
function closeDrawer() { $('drawer').hidden = true; $('scrim').hidden = true; }
function stageControl(lead) {
  const select = h('select', {'aria-label': 'Sales stage'}, h('option', {value: ''}, 'No stage yet'),
    STAGES.map(([k, label]) => h('option', {value: k, selected: lead.stage === k}, label)));
  const note = h('input', {type: 'text', placeholder: 'note (optional)', value: lead.stage_note || '', 'aria-label': 'Stage note'});
  const save = h('button', {class: 'small primary'}, 'Save');
  save.addEventListener('click', busy(save, () => act(() => put(`/api/leads/${encodeURIComponent(lead.username)}/stage`, {stage: select.value || null, note: note.value.trim()}), 'Stage saved').then(() => openLead(lead.username))));
  return h('div', {class: 'field'}, h('span', {}, 'Sales stage'),
    h('div', {class: 'row'}, select, note, save),
    lead.stage_auto ? h('span', {class: 'help-text'}, 'Marked interested automatically from their reply. Set it yourself as the conversation moves on.') : null);
}
async function openLead(handle) {
  if (!handle) return;
  handle = String(handle).replace(/^@/, '');
  $('drawerTitle').textContent = '@' + handle; $('drawer').hidden = false; $('scrim').hidden = false;
  const body = $('drawerBody'); body.replaceChildren(h('div', {class: 'empty'}, 'Loading…'));
  try {
    const [lead, explain, msgs] = await Promise.all([
      api('/api/leads/' + encodeURIComponent(handle)),
      api('/api/leads/' + encodeURIComponent(handle) + '/explain'),
      api('/api/conversations/' + encodeURIComponent('@' + handle) + '/messages').catch(() => []),
    ]);
    const live = S.overview && !S.overview.simulated;
    const facts = [lead.category || lead.niche, lead.location, lead.followers !== null && lead.followers !== undefined ? fmt(lead.followers) + ' followers' : null].filter(Boolean).join(' · ');
    fill(body,
      h('div', {class: 'row'}, avatar(lead.full_name, lead.username, true),
        h('div', {}, h('strong', {}, lead.full_name || handle), h('div', {class: 'small muted'}, facts)),
        h('span', {class: 'spacer'}), scoreBadge(lead.score)),
      h('div', {class: 'row'}, pill(LEAD_TONE[lead.status], LEAD_STATUS[lead.status] || nice(lead.status)),
        lead.stage ? pill(STAGE_TONE[lead.stage], STAGE_NAME[lead.stage]) : null,
        lead.website ? h('span', {class: 'small muted'}, lead.website) : null),
      lead.status_reason ? h('div', {class: 'small'}, lead.status_reason) : null,
      (lead.opportunities || []).length ? h('div', {}, (lead.opportunities || []).map((o) => h('div', {class: 'small'}, '• ' + o.rationale))) : null,
      stageControl(lead),
      h('div', {}, h('h3', {}, 'Chat'), msgs.length
        ? h('div', {class: 'bubbles'}, msgs.map((m) => h('div', {class: 'bubble ' + (m.direction === 'OUTBOUND' ? 'out' : 'in')}, m.text,
            h('div', {class: 'meta'}, [m.sender, m.state, m.intent, (m.sent_at || '').slice(0, 16).replace('T', ' ') + ' UTC'].filter(Boolean).join(' · ')))))
        : h('div', {class: 'empty'}, 'No messages yet.')),
      h('div', {class: 'row'},
        h('button', {onclick: () => act(() => post('/api/conversations/' + encodeURIComponent('@' + handle) + '/claim'), 'This chat is now yours')}, 'Take over this chat'),
        live ? h('a', {class: 'btn', href: `https://www.instagram.com/${encodeURIComponent(handle)}/`, target: '_blank', rel: 'noopener noreferrer'}, icon('external'), 'Open on Instagram') : null,
        h('button', {class: 'danger', onclick: () => { const reason = prompt('Never contact @' + handle + '? Reason:', 'asked in person'); if (reason !== null) act(() => post('/api/suppressions', {kind: 'USERNAME', value: handle, reason}), 'Added to the never-contact list'); }}, 'Never contact')),
      h('details', {class: 'trail-box'}, h('summary', {}, 'Why the bot did what it did (step by step)'), h('pre', {class: 'trail'}, explain.lines.join('\n'))));
  } catch (e) { body.replaceChildren(h('div', {class: 'empty'}, String(e.message || e))); }
}
function scoreBadge(score) {
  if (score === null || score === undefined) return h('span', {class: 'muted small'}, 'no score');
  const fill = h('span', {class: 'fill'}); fill.style.width = Math.max(0, Math.min(100, score)) + '%';
  return h('span', {class: 'score', title: 'score out of 100'}, h('span', {class: 'track'}, fill), String(score));
}

// ------------------------------------------------------------ controls
function bindShell() {
  hydrateIcons(document);
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
  $('menuBtn').addEventListener('click', openNav);
  $('scrim').addEventListener('click', () => { closeNav(); closeDrawer(); });
  $('restartBtn').addEventListener('click', restartNow);
  for (const b of $('feedFilters').querySelectorAll('button')) b.addEventListener('click', () => setFilter(b.dataset.filter));
  $('techToggle').addEventListener('change', (e) => setTech(e.target.checked));
  $('newPill').addEventListener('click', () => { $('feed').scrollTo({top: 0, behavior: 'smooth'}); S.unseen = 0; updatePill(); });
  $('feed').addEventListener('scroll', () => { if ($('feed').scrollTop <= 8 && S.unseen) { S.unseen = 0; updatePill(); } });
  $('drawerClose').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeDrawer(); closeNav(); } });
  $('findNow').addEventListener('click', busy($('findNow'), findNow));
  window.addEventListener('hashchange', showPage);
  const theme = recall('io_theme'); if (theme) document.documentElement.dataset.theme = theme;
}

// ---------------------------------------------------------------- loop
function setConn(ok, msg) {
  const c = $('conn'); c.className = 'conn ' + (ok ? 'ok' : 'bad'); c.textContent = ok ? '● live' : '● ' + (msg || 'reconnecting…');
}
async function refreshAll() {
  await Promise.allSettled([pollOverview(), pollSetup(), pollPreflight()]);
  await loadPage('refresh');
}
async function loop() {
  try {
    await pollFeed();
    await pollOverview();
    if (Date.now() - S.preflightAt > 30000) await pollPreflight();
    if (Date.now() - S.setupAt > 30000) await pollSetup();
    await loadPage(false);
    S.failures = 0; setConn(true);
  } catch (e) {
    S.failures += 1; setConn(false, String(e.message || e).slice(0, 60));
  }
  setTimeout(loop, S.failures ? Math.min(10000, 1500 * S.failures) : 1500);
}
document.addEventListener('DOMContentLoaded', () => {
  tokenFromLink();
  bindShell();
  if (typeof bindPages === 'function') bindPages();
  setTech(S.tech);
  showPage();
  pollSetup().catch(() => {});
  loop();
});
