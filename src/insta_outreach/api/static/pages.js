// Mission Control pages. Uses the helpers in app.js (h, s, api, act, S, ...).
// External text is always inserted with textContent (h() children), never as HTML.
'use strict';

// ================================================================= charts
// Dataviz reference specs: bars <= 24px thick with a 4px rounded data end,
// square at the baseline; hairline grid; one hue per series; text in ink tokens;
// every bar has a hover/focus tooltip and the numbers exist as a table too.
function niceStep(raw) { const p = 10 ** Math.floor(Math.log10(raw)); const m = raw / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; }
function axis(maxValue) {
  const step = Math.max(1, niceStep(Math.max(1, maxValue) / 4));
  const top = Math.max(step, Math.ceil(Math.max(1, maxValue) / step) * step);
  const list = []; for (let v = 0; v <= top + 1e-9; v += step) list.push(v);
  return {top, list};
}
function barRight(x, y, w, hgt) {
  const r = Math.min(4, w / 2, hgt / 2);
  return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${hgt - 2 * r}a${r},${r} 0 0 1 -${r},${r}h${-(w - r)}z`;
}
function barUp(x, base, w, hgt) {
  const r = Math.min(4, w / 2, hgt);
  return `M${x},${base}v${-(hgt - r)}a${r},${r} 0 0 1 ${r},${-r}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${hgt - r}z`;
}
function hitArea(attrs, lines) {
  return s('rect', {...attrs, class: 'hit', tabindex: 0, onpointermove: (e) => tip(e, lines), onpointerleave: untip, onfocus: (e) => tip(e, lines), onblur: untip});
}
function hbarChart(el, rows, {label, value, lines, name}) {
  const W = Math.max(280, el.clientWidth || 560); const rowH = 28; const barH = 16;
  const left = Math.min(160, Math.round(W * 0.3)); const right = 52;
  const H = rows.length * rowH + 6;
  const max = Math.max(1, ...rows.map(value));
  const svg = s('svg', {class: 'chart', width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': name});
  rows.forEach((r, i) => {
    const y = i * rowH + (rowH - barH) / 2; const v = value(r); const w = ((W - left - right) * v) / max;
    svg.append(s('text', {x: left - 8, y: y + barH - 3, 'text-anchor': 'end'}, clip(label(r), Math.max(8, Math.floor(left / 7)))));
    if (w > 0.5) svg.append(s('path', {class: 'bar', d: barRight(left, y, Math.max(2, w), barH)}));
    svg.append(s('text', {class: 'val', x: left + Math.max(2, w) + 6, y: y + barH - 3}, fmt(v)));
    svg.append(hitArea({x: 0, y: i * rowH, width: W, height: rowH}, lines(r)));
  });
  svg.append(s('line', {class: 'base', x1: left, x2: left, y1: 0, y2: H}));
  el.replaceChildren(rows.length ? svg : h('div', {class: 'empty'}, 'No data yet.'));
}
function columnChart(el, days, key, name) {
  const W = Math.max(240, el.clientWidth || 320); const H = 170;
  const left = 30; const right = 6; const top = 10; const bottom = 22;
  const values = days.map((d) => d[key]); const {top: ymax, list} = axis(Math.max(0, ...values));
  const plotW = W - left - right; const plotH = H - top - bottom; const base = top + plotH;
  const band = plotW / days.length; const bw = Math.min(24, Math.max(3, band * 0.62));
  const svg = s('svg', {class: 'chart', width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `${name} per day`});
  for (const t of list) {
    const y = base - (plotH * t) / ymax;
    svg.append(s('line', {class: t ? 'grid' : 'base', x1: left, x2: W - right, y1: y, y2: y}));
    svg.append(s('text', {x: left - 6, y: y + 4, 'text-anchor': 'end'}, fmt(t)));
  }
  const every = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(plotW / 52))));
  days.forEach((d, i) => {
    const v = d[key]; const x = left + band * i + (band - bw) / 2; const hgt = (plotH * v) / ymax;
    if (hgt > 0) svg.append(s('path', {class: 'bar', d: barUp(x, base, bw, Math.max(2, hgt))}));
    if (i % every === 0 || i === days.length - 1) {
      if (i === days.length - 1 || (days.length - 1 - i) >= every / 2) svg.append(s('text', {x: x + bw / 2, y: H - 6, 'text-anchor': 'middle'}, d.label));
    }
    svg.append(hitArea({x: left + band * i, y: top, width: band, height: plotH}, [fmt(v), `${name} on ${d.label}`]));
  });
  el.replaceChildren(svg);
}
let chartTimer;
window.addEventListener('resize', () => { clearTimeout(chartTimer); chartTimer = setTimeout(() => { if (S.page === 'analytics' && S.analytics) renderAnalytics(S.analytics); if (S.page === 'dashboard' && S.dash) renderCategories(S.dash); }, 200); });

// ============================================================== dashboard
const KPIS = [
  {id: 'found', label: 'Businesses found', icon: 'search', href: '#/leads'},
  {id: 'good_fit', label: 'Good fit', icon: 'star', href: '#/leads?status=good'},
  {id: 'messaged', label: 'Messaged', icon: 'send', href: '#/leads?status=messaged'},
  {id: 'replied', label: 'Replied', icon: 'chat', href: '#/chats'},
  {id: 'interested', label: 'Interested', icon: 'heart', href: '#/leads?stage=interested'},
  {id: 'clients', label: 'Clients', icon: 'award', href: '#/leads?stage=WON'},
];
function buildKpis() {
  $('kpis').replaceChildren(...KPIS.map((k) => h('a', {class: 'card kpi', id: 'kpi-' + k.id, href: k.href},
    h('div', {class: 'kpi-top'}, h('span', {class: 'kpi-ico'}, icon(k.icon)), k.label),
    h('div', {class: 'value'}, '–'), h('div', {class: 'delta'}, ' '))));
}
function renderKpis(d) {
  for (const k of KPIS) {
    const card = $('kpi-' + k.id); const v = d.kpis[k.id];
    card.querySelector('.value').textContent = fmt(v.total);
    const delta = card.querySelector('.delta');
    delta.textContent = v.week ? `+${fmt(v.week)} in the last 7 days` : 'none in the last 7 days';
    delta.className = 'delta' + (v.week ? ' up' : '');
  }
}
const FUNNEL = [['found', 'Found'], ['good_fit', 'Good fit'], ['messaged', 'Messaged'], ['replied', 'Replied'], ['interested', 'Interested'], ['clients', 'Clients']];
function renderFunnel(d) {
  const top = Math.max(1, d.kpis.found.total);
  $('funnel').replaceChildren(...FUNNEL.map(([key, label], i) => {
    const v = d.kpis[key].total; const prev = i ? d.kpis[FUNNEL[i - 1][0]].total : null;
    const bar = h('div', {class: 'fb'}); bar.style.width = ((100 * v) / top).toFixed(1) + '%';
    const kpi = KPIS.find((k) => k.id === key);
    return h('div', {class: 'fstep', role: 'link', tabindex: 0, title: 'Show the list', onclick: () => { location.hash = kpi.href; }, onkeydown: (e) => { if (e.key === 'Enter') location.hash = kpi.href; }},
      h('span', {class: 'fl'}, label), h('div', {class: 'ft'}, bar), h('span', {class: 'fv'}, fmt(v)),
      i ? h('span', {class: 'fc'}, prev ? `${Math.round((100 * v) / prev)}% of ${FUNNEL[i - 1][1].toLowerCase()}` : '') : null);
  }));
}
function progressItems(o, d) {
  const u = o.usage; const a = o.attention; const items = [];
  const halted = o.lanes.filter((l) => l.configured && l.state === 'HALTED');
  for (const l of o.lanes.filter((x) => x.needs_login)) {
    items.push({state: 'todo', title: l.channel === 'RESEARCH' ? 'Log in to the research account' : 'Log in to Instagram', sub: 'a window opens on this computer; you type the password yourself', href: '#/settings'});
  }
  items.push(halted.length
    ? {state: 'bad', title: 'Fix the Instagram problem, then press Resume', sub: halted.map((l) => `${laneName(l.channel)}: ${laneWhy(l.reason)}`).join('; '), href: '#/problems'}
    : {state: 'done', title: 'Instagram connections are working', sub: o.simulated ? 'simulated' : '', href: '#/settings'});
  if (o.mode !== 'OBSERVE') {
    items.push(a.pending_approvals
      ? {state: 'todo', title: `Review ${plural(a.pending_approvals, 'message', 'messages')} waiting for you`, sub: 'nothing is sent without your click', href: '#/approvals'}
      : {state: 'done', title: 'No messages waiting for your approval', href: '#/approvals'});
  }
  const waiting = d ? d.awaiting_you : 0;
  items.push(waiting
    ? {state: 'todo', title: `Answer ${plural(waiting, 'business', 'businesses')} waiting for your reply`, sub: 'they wrote last; the bot leaves these chats to you', href: '#/chats'}
    : {state: 'done', title: 'Nobody is waiting for your reply', href: '#/chats'});
  if (o.mode === 'APPROVAL' || o.mode === 'AUTONOMOUS') {
    items.push({state: u.outreach_today >= u.outreach_per_day ? 'done' : 'info', title: `First messages sent today: ${fmt(u.outreach_today)} of ${fmt(u.outreach_per_day)}`,
      sub: u.in_send_hours ? `sending hours ${u.send_hours}` : `sending starts ${when(u.next_send_window_local)}`, meter: [u.outreach_today, u.outreach_per_day]});
  }
  items.push({state: 'info', title: `Businesses checked today: ${fmt(u.inspections_today)}`, sub: `limit ${fmt(u.profile_inspections_per_day)} a day`, meter: [u.inspections_today, u.profile_inspections_per_day]});
  if (!o.simulated) {
    const failing = S.preflight.filter((c) => c.result === 'FAIL').length;
    items.push(failing
      ? {state: 'todo', title: `${plural(failing, 'go-live check', 'go-live checks')} not passing yet`, sub: 'needed only for Autonomous mode', href: '#/settings'}
      : {state: 'done', title: 'Every go-live check passes', href: '#/settings'});
  }
  return items;
}
function renderProgress() {
  const o = S.overview; if (!o) return;
  $('todayDate').textContent = when(o.now_local).split(' ').slice(0, 2).join(' ');
  $('progress').replaceChildren(...progressItems(o, S.dash).map((it) => {
    let bar = null;
    if (it.meter) {
      const fill = h('div', {class: 'mini-fill'}); fill.style.width = (it.meter[1] ? Math.min(100, (100 * it.meter[0]) / it.meter[1]) : 0).toFixed(1) + '%';
      bar = h('div', {class: 'mini-track'}, fill);
    }
    const mark = {done: '✓', bad: '!', todo: '', info: ''}[it.state];
    return h('li', {}, h('span', {class: 'check ' + it.state, 'aria-label': it.state === 'done' ? 'done' : it.state === 'bad' ? 'needs you' : 'to do'}, mark),
      h('div', {}, h('div', {class: 'pt'}, it.title), it.sub ? h('div', {class: 'ps'}, it.sub) : null, bar),
      it.href && it.state !== 'done' ? h('a', {class: 'link small', href: it.href}, 'Open') : h('span'));
  }));
}
function renderLatest(d) {
  const list = $('todayList');
  if (!d.latest.length) { list.replaceChildren(h('div', {class: 'empty'}, S.overview && S.overview.mode === 'OBSERVE' ? 'Observe mode: nothing is written or sent. Switch to Draft or Approval (top right) to get messages.' : 'No messages yet.')); return; }
  list.replaceChildren(...d.latest.map((m) => {
    const [tone, word] = ACTION_STATE[m.status] || ['', nice(m.status)];
    return h('div', {class: 'li'}, avatar(null, m.handle),
      h('div', {}, h('div', {class: 'who'}, handleLink(m.handle), h('span', {class: 'tag'}, m.kind)), h('div', {class: 'what', title: m.preview}, m.preview || '')),
      h('div', {class: 'meta'}, pill(tone, word), h('span', {class: 'nowrap'}, when(m.at_local))));
  }));
}
function renderTop(d) {
  const list = $('topList');
  if (!d.top.length) { list.replaceChildren(h('div', {class: 'empty'}, 'No good-fit businesses waiting. They appear here as the bot finds and checks them.')); return; }
  list.replaceChildren(...d.top.map((t) => {
    let action = h('button', {class: 'small', onclick: () => openLead(t.handle)}, 'View');
    if (t.action_status === 'PENDING_APPROVAL' || t.action_status === 'DRAFTED') action = h('a', {class: 'btn small primary', href: '#/approvals'}, 'Review message');
    else if (t.action_status === 'APPROVED' || t.action_status === 'EXECUTING') action = pill('info', 'queued');
    const facts = [t.niche, t.location, t.followers !== null && t.followers !== undefined ? fmt(t.followers) + ' followers' : null].filter(Boolean).join(' · ');
    return h('div', {class: 'li'}, avatar(t.full_name, t.handle),
      h('div', {}, h('div', {class: 'who'}, handleLink(t.handle, t.full_name || '@' + t.handle), scoreBadge(t.score)),
        h('div', {class: 'what'}, facts || t.why || ''), t.opportunities.length ? h('div', {}, t.opportunities.map((o) => h('span', {class: 'tag'}, nice(o)))) : null),
      h('div', {class: 'meta'}, action));
  }));
}
function renderCategories(d) {
  hbarChart($('categories'), d.categories, {
    name: 'Businesses found by category', label: (r) => nice(r.name), value: (r) => r.found,
    lines: (r) => [plural(r.found, 'business found', 'businesses found'), `${nice(r.name)}: ${fmt(r.good_fit)} good fit, ${fmt(r.messaged)} messaged, ${fmt(r.replied)} replied`],
  });
}
async function loadDashboard() {
  const d = await api('/api/dashboard');
  S.dash = d; renderKpis(d); renderFunnel(d); renderLatest(d); renderTop(d); renderCategories(d); renderProgress();
  $('b-chats').textContent = d.awaiting_you || '';
}

// ============================================================== approvals
async function loadApprovals() {
  const box = $('approvals');
  if (document.activeElement && document.activeElement.tagName === 'TEXTAREA' && box.contains(document.activeElement)) return;  // don't clobber an edit
  const items = await api('/api/actions?status=PENDING_APPROVAL&status=DRAFTED&limit=100&with_lead=true');
  const o = S.overview;
  if (!items.length) {
    box.replaceChildren(h('div', {class: 'card empty'}, o && o.mode === 'OBSERVE'
      ? 'Observe mode: the bot only finds and checks businesses. Switch to Draft or Approval (top right) and it writes messages for you to approve here.'
      : 'Nothing is waiting for your approval. New messages appear here as the bot writes them.'));
    return;
  }
  const note = o && o.mode === 'DRAFT' ? h('div', {class: 'card small'}, 'Draft mode: approved messages wait until you switch to Approval or Autonomous.') : null;
  const live = o && !o.simulated;
  const max = 480;
  fill(box, note, items.map((a) => {
    const lead = a.lead || {};
    const text = h('textarea', {'aria-label': 'Message text'}, a.message || '');
    const count = h('div', {class: 'count-note'}, `${(a.message || '').length} characters`);
    text.addEventListener('input', () => { count.textContent = `${text.value.length} characters` + (text.value.length > max ? ' · long for a first message' : ''); });
    const edited = () => (text.value.trim() !== (a.message || '').trim() ? text.value.trim() : undefined);
    const kind = a.type === 'SEND_OUTREACH' && a.capability === 'private_reply' ? 'private reply to their comment' : (MESSAGE_KIND[a.type] || a.type);
    const facts = [lead.category || lead.niche, lead.location, lead.followers !== null && lead.followers !== undefined ? fmt(lead.followers) + ' followers' : null].filter(Boolean).join(' · ');
    const approve = h('button', {class: 'primary'}, icon('check'), 'Approve');
    approve.addEventListener('click', busy(approve, () => act(() => post(`/api/actions/${a.id}/approve`, {edited_text: edited()}), edited() ? 'Approved with your edit' : 'Approved')));
    return h('div', {class: 'card item approval', dataset: {action: a.id}},
      h('div', {class: 'lead-side'},
        h('div', {class: 'lead-top'}, avatar(lead.full_name, a.target_username, true),
          h('div', {}, h('div', {}, h('strong', {}, lead.full_name || a.target_username)), handleLink(a.target_username))),
        h('div', {class: 'row'}, scoreBadge(lead.score), facts ? h('span', {class: 'small muted'}, facts) : null),
        lead.status_reason ? h('div', {class: 'small ink2'}, lead.status_reason) : null,
        (lead.opportunities || []).length ? h('div', {}, lead.opportunities.map((op) => h('span', {class: 'tag', title: op.rationale}, nice(op.type)))) : null,
        lead.website ? h('div', {class: 'small muted'}, 'website: ' + lead.website) : null,
        live ? h('a', {class: 'link small', href: `https://www.instagram.com/${encodeURIComponent(a.target_username)}/`, target: '_blank', rel: 'noopener noreferrer'}, 'Open their profile on Instagram ↗') : null),
      h('div', {class: 'msg-side'},
        h('div', {class: 'row'}, h('span', {class: 'tag'}, kind), a.status === 'DRAFTED' ? status('neutral', 'draft') : status('warning', 'waiting for you'),
          h('span', {class: 'small muted'}, 'written by ' + (a.composer === 'template' ? 'a template' : a.composer === 'llm' ? 'Claude' : (a.composer || '?')))),
        text, count,
        a.facts_used && a.facts_used.length ? h('div', {class: 'facts'}, 'Based on: ', ...a.facts_used.map((f) => h('span', {class: 'tag'}, nice(f)))) : null,
        h('div', {class: 'row'}, approve,
          h('button', {onclick: () => { const reason = prompt('What should change? (kept in the history)', 'tone'); if (reason !== null) act(() => post(`/api/actions/${a.id}/reject`, {reason, redraft: true}), 'Rejected: a new version will be written'); }}, 'Reject & rewrite'),
          h('button', {class: 'danger', onclick: () => { const reason = prompt("Reject and don't message this business? Reason:", 'not a fit'); if (reason !== null) act(() => post(`/api/actions/${a.id}/reject`, {reason, redraft: false}), 'Rejected'); }}, "Don't message"))));
  }));
}

// ============================================================== businesses
const LEAD_FILTERS = [
  ['', 'All businesses'], ['good', 'Good fit (any stage)'], ['QUALIFIED', 'Good fit, not messaged yet'], ['OUTREACH_PENDING', 'Message queued'],
  ['messaged', 'Messaged'], ['REPLIED', 'Replied'], ['HANDED_OFF', 'With you'], ['DISCOVERED', 'Being checked'],
  ['ANALYZED', 'No clear need'], ['DISQUALIFIED', 'Not a fit'], ['DUPLICATE', 'Duplicates'], ['CLOSED', 'Closed'], ['UNREACHABLE', "Can't be messaged"],
];
const MESSAGED = ['CONTACTED', 'REPLIED', 'HANDED_OFF', 'CLOSED'];
function leadMatches(l, status, niche, stage, q) {
  if (status === 'good' && !GOOD_FIT.includes(l.status)) return false;
  if (status === 'messaged' && !(l.contacted_at || MESSAGED.includes(l.status))) return false;
  if (status && !['good', 'messaged'].includes(status) && l.status !== status) return false;
  if (niche && (l.niche || 'other') !== niche) return false;
  if (stage === 'interested' && !(l.stage && l.stage !== 'LOST')) return false;
  if (stage && stage !== 'interested' && l.stage !== stage) return false;
  if (q && !(`${l.username} ${l.full_name || ''}`.toLowerCase().includes(q))) return false;
  return true;
}
function renderLeads() {
  const all = S.leads || [];
  const status = $('leadStatus').value; const niche = $('leadNiche').value; const q = $('leadSearch').value.trim().toLowerCase().replace(/^@/, '');
  const stage = hashParams().get('stage') || '';
  const rows = all.filter((l) => leadMatches(l, status, niche, stage, q));
  $('leadCount').replaceChildren(plural(rows.length, 'business', 'businesses'),
    stage ? ` · stage: ${stage === 'interested' ? 'interested or further' : (STAGE_NAME[stage] || stage)} · ` : '',
    stage ? h('a', {class: 'link', href: '#/leads'}, 'show all') : '');
  const head = ['Business', 'Category', 'Place', 'Followers', 'Score', 'Status', 'Stage', 'Found'];
  const table = h('table', {}, h('thead', {}, h('tr', {}, head.map((c) => h('th', {class: c === 'Followers' ? 'num' : ''}, c)))),
    h('tbody', {}, rows.slice(0, 500).map((l) => h('tr', {class: 'clickable', tabindex: 0, onclick: () => openLead(l.username), onkeydown: (e) => { if (e.key === 'Enter') openLead(l.username); }},
      h('td', {}, h('div', {class: 'biz'}, avatar(l.full_name, l.username), h('div', {}, h('div', {class: 'n'}, l.full_name || '@' + l.username), h('div', {class: 'small muted'}, '@' + l.username)))),
      h('td', {}, nice(l.niche || l.category || '')),
      h('td', {}, l.location || ''),
      h('td', {class: 'num'}, l.followers === null || l.followers === undefined ? '–' : fmt(l.followers)),
      h('td', {}, scoreBadge(l.score)),
      h('td', {title: l.status_reason || ''}, pill(LEAD_TONE[l.status], LEAD_STATUS[l.status] || nice(l.status))),
      h('td', {}, l.stage ? pill(STAGE_TONE[l.stage], STAGE_NAME[l.stage] + (l.stage_auto ? ' (from reply)' : '')) : ''),
      h('td', {class: 'small muted'}, day(l.found_at))))));
  $('leadsTable').replaceChildren(rows.length ? table : h('div', {class: 'empty'}, all.length ? 'No business matches these filters.' : 'No businesses yet. The bot finds them by itself, or add one with the button above.'));
  if (rows.length > 500) $('leadsTable').append(h('div', {class: 'small muted empty'}, `Showing the first 500 of ${fmt(rows.length)}: narrow the filters to see the rest.`));
}
async function loadLeads() {
  S.leads = await api('/api/leads?limit=5000');
  const niches = [...new Set(S.leads.map((l) => l.niche || 'other'))].sort();
  const current = $('leadNiche').value;
  $('leadNiche').replaceChildren(h('option', {value: ''}, 'All categories'), ...niches.map((n) => h('option', {value: n, selected: n === current}, nice(n))));
  const wanted = hashParams().get('status');
  if (wanted !== null && S.leadStatusFromHash !== location.hash) { $('leadStatus').value = wanted; S.leadStatusFromHash = location.hash; }
  renderLeads();
}
async function addLead() {
  const dlg = $('addLeadDialog'); $('addLeadHandle').value = ''; $('addLeadNote').value = '';
  dlg.addEventListener('close', async () => {
    if (dlg.returnValue !== 'ok') return;
    const handle = $('addLeadHandle').value.trim(); if (!handle) return;
    const out = await act(() => post('/api/leads', {username: handle, note: $('addLeadNote').value.trim()}), (r) => (r.created ? `Added @${r.username}: it will be checked shortly` : `@${r.username} was already known`));
    if (out) { S.loadedAt.leads = 0; loadPage(true); }
  }, {once: true});
  dlg.showModal();
}

// ================================================================== chats
function chatSort(a, b) {
  return (b.waiting_on_you - a.waiting_on_you) || (b.automation_paused - a.automation_paused) || String(b.last_at || '').localeCompare(String(a.last_at || ''));
}
async function loadChats() {
  const items = (await api('/api/conversations?limit=200')).sort(chatSort);
  S.chats = items;
  const list = $('chatList');
  if (!items.length) { list.replaceChildren(h('div', {class: 'empty chat-body'}, 'No chats yet. Conversations appear here once businesses are messaged.')); return; }
  if (!S.chat || !items.some((c) => c.id === S.chat)) S.chat = items[0].id;
  list.replaceChildren(...items.map((c) => h('div', {class: 'chat-item' + (c.id === S.chat ? ' on' : ''), tabindex: 0, onclick: () => { S.chat = c.id; loadChats(); }, onkeydown: (e) => { if (e.key === 'Enter') { S.chat = c.id; loadChats(); } }},
    avatar(c.full_name, c.peer_username),
    h('div', {}, h('div', {class: 'who'}, c.full_name || '@' + (c.peer_username || c.peer_igsid)),
      h('div', {class: 'last'}, (c.last_direction === 'OUTBOUND' ? 'You: ' : '') + clip(c.last_text || '', 80))),
    h('div', {class: 'meta small'}, c.waiting_on_you ? pill('warning', 'your turn') : c.automation_paused ? pill('info', 'you') : pill('', 'bot'), h('div', {class: 'muted'}, day(c.last_at))))));
  await renderChat(items.find((c) => c.id === S.chat));
}
async function renderChat(c) {
  const view = $('chatView');
  const msgs = await api(`/api/conversations/${c.id}/messages`);
  const handle = c.peer_username;
  const live = S.overview && !S.overview.simulated;
  fill(view,
    h('div', {class: 'chat-head'}, avatar(c.full_name, handle),
      h('div', {}, h('strong', {}, c.full_name || '@' + handle), h('div', {class: 'small'}, handle ? handleLink(handle) : '')),
      c.stage ? pill(STAGE_TONE[c.stage], STAGE_NAME[c.stage]) : null,
      h('span', {class: 'spacer'}),
      c.automation_paused ? status('warning', 'you handle this chat') : status('good', 'the bot handles this chat'),
      c.automation_paused
        ? h('button', {class: 'small', onclick: () => act(() => post(`/api/conversations/${c.id}/release`), 'Handed back to the bot')}, 'Hand back to the bot')
        : h('button', {class: 'small', onclick: () => act(() => post(`/api/conversations/${c.id}/claim`), 'This chat is now yours')}, 'Take over'),
      handle ? h('button', {class: 'small', onclick: () => openLead(handle)}, 'Details') : null,
      live && handle ? h('a', {class: 'btn small', href: 'https://www.instagram.com/direct/inbox/', target: '_blank', rel: 'noopener noreferrer'}, icon('external'), 'Instagram inbox') : null),
    h('div', {class: 'chat-body'}, msgs.length
      ? h('div', {class: 'bubbles'}, msgs.map((m) => h('div', {class: 'bubble ' + (m.direction === 'OUTBOUND' ? 'out' : 'in')}, m.text,
          h('div', {class: 'meta'}, [m.direction === 'OUTBOUND' ? (m.sender === 'HUMAN' ? 'you' : 'the bot') : null, m.intent ? nice(m.intent) : null, (m.sent_at || '').slice(0, 16).replace('T', ' ') + ' UTC'].filter(Boolean).join(' · ')))))
      : h('div', {class: 'empty'}, 'No messages yet.')),
    c.automation_paused ? h('div', {class: 'small muted chat-body'}, 'Reply to them from the Instagram app. The bot stays out of this chat until you hand it back.') : null);
  const body = view.querySelector('.chat-body'); body.scrollTop = body.scrollHeight;
}

// ============================================================== campaigns
function listToText(kind, value) {
  const items = Array.isArray(value) ? value : [];
  if (kind === 'location') return items.map((l) => (typeof l === 'object' && l ? `${l.name || ''}${l.url ? ' | ' + l.url : ''}` : String(l))).join('\n');
  return items.map((v) => (kind === 'tag' ? '#' + v : kind === 'handle' ? '@' + v : v)).join('\n');
}
const LIST_EXAMPLE = {
  text: 'bridal makeup thane', tag: '#thanedentist', handle: '@some.local.account',
  location: 'Bandra West | https://www.instagram.com/explore/locations/…', url: 'https://www.instagram.com/p/…',
};
function campaignCard(c, index, info) {
  const stats = info.stats[c.id] || {found: 0, good_fit: 0, messaged: 0, replied: 0};
  const disabled = !info.can_edit;
  const on = h('input', {type: 'checkbox', checked: c.enabled !== false, disabled});
  const name = h('input', {type: 'text', value: c.id, disabled: disabled || !c._new, 'aria-label': 'Campaign name'});
  const niches = h('input', {type: 'text', value: (c.niches || []).join(', '), disabled, placeholder: 'dentist, salon, cafe'});
  const places = h('input', {type: 'text', value: (c.locations || []).join(', '), disabled, placeholder: 'Mumbai, Thane'});
  const minScore = h('input', {type: 'number', min: 0, max: 100, value: c.min_score ?? '', placeholder: String(info.min_score_default), disabled});
  const perDay = h('input', {type: 'number', min: 1, max: 1000, value: c.max_new_leads_per_day ?? 60, disabled});
  const specs = new Map((c.strategies || []).map((sp) => [sp.name, sp]));
  const ways = info.strategies.map((st) => {
    const spec = specs.get(st.name) || {name: st.name, enabled: false, params: {}};
    const check = h('input', {type: 'checkbox', checked: spec.enabled !== false && specs.has(st.name), disabled});
    const list = h('textarea', {placeholder: LIST_EXAMPLE[st.list_kind] || '', disabled, 'aria-label': st.list_label}, listToText(st.list_kind, (spec.params || {})[st.list]));
    const box = h('div', {class: 'way' + (check.checked ? '' : ' off')}, h('label', {class: 'inline-check'}, check, h('strong', {}, st.label)), h('div', {class: 'help-text'}, st.help), h('span', {class: 'small muted'}, st.list_label), list);
    check.addEventListener('change', () => box.classList.toggle('off', !check.checked));
    return {st, spec, check, list, box};
  });
  const read = () => ({
    id: name.value.trim().toLowerCase(), enabled: on.checked,
    niches: niches.value.split(',').map((x) => x.trim()).filter(Boolean), locations: places.value.split(',').map((x) => x.trim()).filter(Boolean),
    min_score: minScore.value === '' ? null : Number(minScore.value), max_new_leads_per_day: Number(perDay.value || 60),
    strategies: ways.filter((w) => w.check.checked || specs.has(w.st.name)).map((w) => ({
      name: w.st.name, enabled: w.check.checked, params: {...(w.spec.params || {}), [w.st.list]: w.list.value},
    })),
  });
  const save = h('button', {class: 'primary', disabled}, 'Save campaign');
  save.addEventListener('click', busy(save, () => saveCampaigns((list) => { list[index] = read(); return list; }, 'Campaign saved: it applies from the next search')));
  const remove = h('button', {class: 'danger', disabled}, icon('trash'), 'Delete');
  remove.addEventListener('click', () => { if (confirm(`Delete the campaign "${c.id}"? Businesses it already found stay.`)) saveCampaigns((list) => { list.splice(index, 1); return list; }, 'Campaign deleted'); });
  return h('section', {class: 'card camp'},
    h('div', {class: 'camp-head'}, h('label', {class: 'inline-check', title: 'Switch this campaign on or off'}, on, h('span', {class: 'small'}, 'on')), c._new ? name : h('h3', {}, c.id),
      h('span', {class: 'spacer'}),
      h('div', {class: 'camp-stats'}, ...[['found', stats.found], ['good fit', stats.good_fit], ['messaged', stats.messaged], ['replied', stats.replied]].map(([k, v]) => h('span', {}, h('b', {}, fmt(v)), ' ' + k)))),
    h('div', {class: 'fields-2'},
      h('label', {class: 'field'}, h('span', {}, 'Kinds of business'), niches, h('span', {class: 'help-text'}, 'comma-separated')),
      h('label', {class: 'field'}, h('span', {}, 'Places'), places, h('span', {class: 'help-text'}, 'cities or areas, comma-separated')),
      h('label', {class: 'field'}, h('span', {}, 'Lowest score to message'), minScore, h('span', {class: 'help-text'}, 'empty: the general setting')),
      h('label', {class: 'field'}, h('span', {}, 'New businesses a day'), perDay, h('span', {class: 'help-text'}, 'at most'))),
    h('div', {}, h('h3', {}, 'Ways to find them'), h('div', {class: 'ways'}, ways.map((w) => w.box))),
    h('div', {class: 'row'}, save, c._new ? null : remove));
}
async function saveCampaigns(change, message) {
  const base = (S.campaigns ? S.campaigns.campaigns : []).filter((c) => !c._new).map((c) => ({...c}));
  const pending = (S.campaigns ? S.campaigns.campaigns : []).filter((c) => c._new);
  // Unsaved new campaigns are sent only when they are the one being saved (read() drops the _new mark).
  const list = change([...base, ...pending].map((c) => ({...c}))).filter((c) => !c._new);
  const out = await act(() => put('/api/campaigns', {campaigns: list}), message);
  if (out) { S.campaigns = out; renderCampaigns(); }
}
function renderCampaigns() {
  const info = S.campaigns;
  const box = $('campaigns');
  const cards = info.campaigns.map((c, i) => campaignCard(c, i, info));
  fill(box, info.can_edit ? null : h('div', {class: 'card small'}, info.why_not), cards, cards.length ? null : h('div', {class: 'card empty'}, 'No campaigns yet: add one to tell the bot who to look for.'));
  $('addCampaign').disabled = !info.can_edit;
}
async function loadCampaigns(force) {
  if (force !== true && S.campaigns) return;  // forms are never refreshed under your hands
  S.campaigns = await api('/api/campaigns'); renderCampaigns();
}
function newCampaign() {
  if (!S.campaigns) return;
  const n = S.campaigns.campaigns.length + 1;
  S.campaigns.campaigns.push({_new: true, id: `campaign-${n}`, enabled: true, niches: [], locations: [], min_score: null, max_new_leads_per_day: 60,
    strategies: [{name: 'keyword_search', enabled: true, params: {max_queries_per_run: 3, max_results: 20}},
      {name: 'suggested_accounts', enabled: true, params: {seeds_per_run: 2, max_results: 15}},
      {name: 'hashtag', enabled: true, params: {tags_per_run: 2, max_posts: 9}}]});
  renderCampaigns();
  $('campaigns').lastElementChild.scrollIntoView({behavior: 'smooth', block: 'start'});
}

// ============================================================== analytics
const RATE_TILES = [
  ['reply', 'Reply rate', 'of businesses messaged replied', (x) => pct(x)],
  ['interested_per_100', 'Interested per 100 messaged', 'the number that matters most', (x) => (x === null ? '–' : fmt(Math.round(x * 10) / 10))],
  ['clients_per_100', 'Clients per 100 messaged', 'mark clients in a business\'s details', (x) => (x === null ? '–' : fmt(Math.round(x * 10) / 10))],
  ['good_fit', 'Good-fit rate', 'of businesses found were worth a message', (x) => pct(x)],
];
const DAILY = [['found', 'Businesses found'], ['messaged', 'First messages sent'], ['replied', 'Replies received']];
function tableOf(rows, cols) {
  return h('table', {}, h('thead', {}, h('tr', {}, cols.map(([, label, num]) => h('th', {class: num ? 'num' : ''}, label)))),
    h('tbody', {}, rows.map((r) => h('tr', {}, cols.map(([key, , num, f]) => h('td', {class: num ? 'num' : ''}, f ? f(r) : (num ? fmt(r[key]) : r[key])))))));
}
const SOURCE_NAME = {keyword_search: 'Instagram search', hashtag: 'Hashtags', suggested_accounts: 'Similar accounts', location: 'Location pages', followers_of: 'Followers of an account', following_of: 'Accounts an account follows', post_engagers: 'Post commenters', manual: 'Added by you', comment: 'Commented on your post', unknown: 'Other'};
function renderAnalytics(a) {
  $('rates').replaceChildren(...RATE_TILES.map(([key, label, sub, f]) => h('div', {class: 'card stat'}, h('span', {class: 'label'}, label), h('span', {class: 'value'}, f(a.rates[key])), h('span', {class: 'sub'}, sub))));
  const daily = $('daily');
  daily.replaceChildren(...DAILY.map(([key, label]) => {
    const total = a.days.reduce((n, d) => n + d[key], 0);
    return h('section', {class: 'card chart-card'}, h('h3', {}, label), h('p', {class: 'sub small muted'}, `${fmt(total)} in ${a.days.length} days`), h('div', {dataset: {chart: key}}));
  }), h('details', {class: 'card'}, h('summary', {}, 'The same numbers as a table'),
    h('div', {class: 'table-wrap'}, tableOf([...a.days].reverse(), [['label', 'Day'], ['found', 'Found', true], ['messaged', 'First messages', true], ['followups', 'Follow-ups', true], ['replied', 'Replies', true], ['interested', 'Interested', true]]))));
  daily.lastElementChild.classList.add('full');
  for (const [key, label] of DAILY) columnChart(daily.querySelector(`[data-chart="${key}"]`), a.days, key, label);
  const found = Math.max(1, a.funnel[0].count);
  hbarChart($('funnelChart'), a.funnel, {
    name: 'Funnel conversion', label: (r) => FUNNEL.find(([k]) => k === r.stage)[1], value: (r) => r.count,
    lines: (r) => [fmt(r.count), `${FUNNEL.find(([k]) => k === r.stage)[1]}: ${Math.round((100 * r.count) / found)}% of businesses found`],
  });
  const rate = (r) => (r.messaged ? pct(r.replied / r.messaged) : '–');
  $('sourceTable').replaceChildren(tableOf(a.by_source, [['name', 'Found through', false, (r) => SOURCE_NAME[r.name] || nice(r.name)], ['found', 'Found', true], ['good_fit', 'Good fit', true], ['messaged', 'Messaged', true], ['replied', 'Replied', true], ['interested', 'Interested', true]]));
  $('nicheTable').replaceChildren(tableOf(a.by_niche, [['name', 'Category', false, (r) => nice(r.name)], ['found', 'Found', true], ['good_fit', 'Good fit', true], ['messaged', 'Messaged', true], ['replied', 'Replied', true], ['interested', 'Interested', true], ['rate', 'Reply rate', true, rate]]));
}
async function loadAnalytics() {
  const days = Number(recall('io_days') || 14);
  for (const b of $('rangeChips').querySelectorAll('button')) b.classList.toggle('on', Number(b.dataset.days) === days);
  S.analytics = await api('/api/analytics?days=' + days); renderAnalytics(S.analytics);
}

// =============================================================== activity
async function loadHistory() {
  if (!$('historyBox').open) return;
  const input = h('input', {type: 'text', placeholder: 'filter by event type, e.g. message.sent, lane, mode', value: S.auditKind || '', 'aria-label': 'Filter by event type'});
  input.addEventListener('change', () => { S.auditKind = input.value.trim(); loadHistory(); });
  const items = await api('/api/audit?limit=300' + (S.auditKind ? '&kind=' + encodeURIComponent(S.auditKind) : ''));
  $('history').replaceChildren(h('div', {class: 'toolbar'}, input),
    h('div', {class: 'table-wrap'}, h('table', {}, h('thead', {}, h('tr', {}, ['Time (UTC)', 'Who', 'Event', 'Subject', 'What'].map((c) => h('th', {}, c)))),
      h('tbody', {}, items.map((e) => h('tr', {},
        h('td', {class: 'small num'}, (e.at || '').slice(0, 16).replace('T', ' ')), h('td', {class: 'small'}, e.actor),
        h('td', {class: 'small'}, e.kind),
        h('td', {class: 'small'}, e.subject && e.subject.startsWith('@') ? handleLink(e.subject.slice(1), e.subject) : (e.subject || '')),
        h('td', {}, e.summary)))))));
}

// =============================================================== problems
async function evidenceImg(path) {
  const r = await call('/api/evidence/' + encodeURIComponent(path).replace(/%2F/g, '/'));
  const url = URL.createObjectURL(await r.blob()); S.blobUrls.push(url);
  const img = h('img', {alt: 'screenshot of what Instagram showed', title: path}); img.src = url;
  img.addEventListener('click', () => window.open(url, '_blank'));
  return img;
}
async function loadProblems() {
  const items = await api('/api/incidents?all=true');
  S.blobUrls.forEach((u) => URL.revokeObjectURL(u)); S.blobUrls = [];
  const box = $('problems');
  if (!items.length) { box.replaceChildren(h('div', {class: 'card empty'}, "No problems. If Instagram ever asks to confirm it's you or to slow down, it shows up here with a screenshot.")); return; }
  const tone = {CRITICAL: 'critical', WARNING: 'warning', INFO: 'info'};
  box.replaceChildren(...await Promise.all(items.map(async (i) => {
    const shots = await Promise.all((i.evidence || []).filter((e) => e.kind === 'screenshot' && e.path).slice(0, 3).map((e) => evidenceImg(e.path).catch(() => h('span', {class: 'small muted'}, 'screenshot unavailable'))));
    const open = i.status === 'OPEN';
    return h('div', {class: 'card item'},
      h('div', {class: 'row'}, status(open ? tone[i.severity] : 'good', open ? 'needs you' : 'fixed'), h('strong', {}, STOP_REASON[i.kind] || i.title)),
      h('div', {class: 'small muted'}, when((i.created_at || '').replace('T', ' ')) + ' UTC · ' + (i.channel ? laneName(i.channel) : '') + ' · #' + i.id),
      i.detail ? h('div', {class: 'small'}, i.detail) : null,
      i.page_url ? h('div', {class: 'small muted'}, 'page: ' + i.page_url) : null,
      shots.length ? h('div', {class: 'shots'}, shots) : null,
      i.resolved_at ? h('div', {class: 'small muted'}, 'fixed by ' + i.resolved_by + (i.resolution_note ? ': ' + i.resolution_note : '')) : null,
      open && i.channel ? h('div', {class: 'row'}, h('button', {class: 'primary', onclick: () => resumeLane(i.channel)}, `I fixed it on Instagram: resume the ${laneName(i.channel).toLowerCase()}…`)) : null);
  })));
}

// =============================================================== settings
const CHECK_NAME = {
  environment: 'Where it runs', control_token: 'Control token', send_lane: 'A way to send messages',
  browser_session: 'Instagram login (your account)', research_session: 'Instagram login (research account)',
  api_credentials: 'Official Meta API', webhooks: 'Official API webhooks', lanes: 'No account stopped',
  incidents: 'No open problems', approved_sends: 'Messages you approved', sandbox: 'Test mode',
  global_pause: 'Pause switch', llm: 'Message writer (Claude)',
};
const LIMIT_FIELDS = [
  ['outreach_per_day', 'First messages per day'], ['outreach_per_hour', 'First messages per hour'],
  ['followups_per_day', 'Follow-ups per day'], ['replies_per_hour', 'Replies per hour'],
  ['profile_inspections_per_day', 'Profiles checked per day'], ['discovery_runs_per_day', 'Searches per day'],
  ['min_seconds_between_sends', 'Seconds between two messages, at least'], ['browser_units_per_hour', 'Browser page views per hour'],
  ['max_followups_per_lead', 'Follow-ups per business, at most'],
];
function setCard(id, title, sub, ...content) {
  return h('section', {class: 'card', id: 'set-' + id}, h('div', {class: 'card-head'}, h('h3', {}, title), sub ? h('span', {class: 'sub'}, sub) : null), h('div', {class: 'form'}, ...content));
}
function field(label, input, help) { return h('label', {class: 'field'}, h('span', {}, label), input, help ? h('span', {class: 'help-text'}, help) : null); }
async function saveSetup(changes, message) {
  const out = await act(() => patch('/api/setup', {changes}), (r) => (r.restart_required.length ? message + '. Restart to apply it.' : message));
  if (out) { S.setup = out.setup; renderRestart(); renderSettings(out.setup); }
  return out;
}
function whereCard(st) {
  const v = st.values; const live = st.environment === 'live';
  const show = h('input', {type: 'checkbox', checked: v['browser.headless'] === false, disabled: !st.can_edit});
  show.addEventListener('change', () => saveSetup({'browser.headless': !show.checked}, show.checked ? 'The browser window will show' : 'The browser will work hidden'));
  const savedLive = v.environment === 'live';
  return setCard('where', 'Where it runs', null,
    h('div', {class: 'row'}, live ? pill('critical', 'Real Instagram') : pill('info', 'Simulation'), live ? h('span', {}, `connected as @${st.account}`) : h('span', {class: 'ink2'}, 'made-up businesses; nothing leaves your computer')),
    savedLive !== live ? h('div', {class: 'small'}, pill('warning', 'after restart'), ` switches to ${savedLive ? 'real Instagram' : 'the simulation'}`) : null,
    live
      ? h('div', {class: 'row'}, h('button', {disabled: !st.can_edit, onclick: () => backToSimulation(st)}, 'Back to the simulation'))
      : h('div', {class: 'row'}, h('button', {class: 'primary', disabled: !st.can_edit, onclick: () => goLive(st)}, icon('login'), 'Switch to my real Instagram…')),
    h('p', {class: 'small ink2'}, live
      ? 'Sending only happens in Approval (you approve each message) or Autonomous mode. Test mode below limits who can be messaged.'
      : 'When you are ready: switch to your real account. It starts in Observe mode (it sends nothing) and, if you like, only messages test accounts you name.'),
    h('label', {class: 'inline-check'}, show, 'Show the browser window while it works (applies after a restart)'),
    st.can_edit ? null : h('p', {class: 'small muted'}, st.why_not));
}
function backToSimulation(st) {
  if (!confirm('Switch back to the simulation? The program restarts; your real account is left alone.\n\nTest mode is switched off, and paused searches are switched back on, so the simulation works as before.')) return;
  // Test mode and "don't look for businesses yet" belong to testing the real account: in the
  // simulation they would stop it from writing messages to its made-up businesses.
  const changes = {environment: 'local', 'rollout.allowed_targets': null};
  if (st.values.campaigns.length && st.values.campaigns.every((c) => c.enabled === false)) changes.campaigns = st.values.campaigns.map((c) => ({...c, enabled: true}));
  saveSetup(changes, 'Switching to the simulation').then((o) => o && st.restart_supported && restartNowQuiet());
}
async function restartNowQuiet() { try { await post('/api/setup/restart'); toast('Restarting… this page reconnects by itself.'); } catch (e) { toast(String(e.message || e), true); } }
function goLive(st) {
  const dlg = $('goLiveDialog'); $('goLiveAccount').textContent = st.account;
  $('goLiveTargets').value = (st.values['rollout.allowed_targets'] || []).join(', ');
  dlg.addEventListener('close', async () => {
    if (dlg.returnValue !== 'ok') return;
    const changes = {environment: 'live', 'browser.enabled': true, 'browser.headless': !$('goLiveShow').checked, 'rollout.allowed_targets': $('goLiveTargets').value};
    if ($('goLiveNoSearch').checked) changes.campaigns = st.values.campaigns.map((c) => ({...c, enabled: false}));
    const out = await saveSetup(changes, 'Saved: real Instagram');
    if (out && st.restart_supported) await restartNowQuiet();
  }, {once: true});
  dlg.showModal();
}
function accountRow(a, st) {
  const job = a.job;
  const lane = LANE_STATE[a.lane_state] || ['neutral', a.lane_state];
  const login = h('button', {class: 'primary small', disabled: a.simulated || !a.configured || (job && job.state === 'running')}, icon('login'), 'Log in…');
  login.addEventListener('click', () => {
    if (!confirm(`A browser window opens on this computer. Log in to @${a.username} yourself and complete any security check there: the program never types your password.\n\nThe window closes by itself once you're in (you have 15 minutes).`)) return;
    act(() => post(`/api/accounts/${a.which}/login`), 'Login window opening on this computer…').then(() => { S.loadedAt.settings = 0; });
  });
  const check = h('button', {class: 'small', disabled: a.simulated || !a.configured || (job && job.state === 'running')}, icon('shield'), 'Test login');
  check.addEventListener('click', () => act(() => post(`/api/accounts/${a.which}/check`), 'Checking the login (read-only)…'));
  const verified = a.verified_at_local ? `login checked ${when(a.verified_at_local)}${a.verified_via ? ' (' + a.verified_via + ')' : ''}` : 'not logged in from this computer yet';
  return h('div', {class: 'acct-card', dataset: {account: a.which}},
    h('div', {class: 'row'}, h('strong', {}, '@' + a.username), h('span', {class: 'tag'}, a.which === 'brand' ? 'your account: sends messages' : 'research account: finds businesses'),
      h('span', {class: 'spacer'}), a.configured ? status(lane[0], lane[1]) : status('neutral', 'not running')),
    h('div', {class: 'small ink2'}, a.simulated ? 'Simulation: no real login needed.' : verified),
    a.lane_reason && a.lane_state !== 'ACTIVE' ? h('div', {class: 'small'}, laneWhy(a.lane_reason)) : null,
    job ? h('div', {class: 'job ' + job.state}, h('strong', {}, {running: 'Working… ', ok: 'Done. ', failed: 'Not done. '}[job.state]), job.message) : null,
    a.simulated ? null : h('div', {class: 'row'}, login, check, a.lane_state === 'HALTED' ? h('button', {class: 'small', onclick: () => resumeLane(a.channel)}, 'Resume…') : null),
    !a.simulated && !a.configured ? h('div', {class: 'small muted'}, a.which === 'brand' ? 'Turn on the browser (switch to real Instagram) first.' : 'Saved: applies after a restart.') : null);
}
function accountsCard(st) {
  const v = st.values;
  const on = h('input', {type: 'checkbox', checked: v['research.enabled'], disabled: !st.can_edit});
  const user = h('input', {type: 'text', value: v['research.account.username'] || '', placeholder: 'research account username', disabled: !st.can_edit});
  const save = h('button', {class: 'small', disabled: !st.can_edit}, 'Save');
  save.addEventListener('click', busy(save, () => saveSetup({'research.enabled': on.checked, 'research.account.username': user.value.trim()}, 'Research account saved')));
  return setCard('accounts', 'Instagram accounts', 'logins stay on this computer',
    h('div', {id: 'accountRows'}, st.accounts.map((a) => accountRow(a, st))),
    h('details', {}, h('summary', {}, 'Use a separate research account to find businesses'),
      h('div', {class: 'form'},
        h('p', {class: 'small ink2'}, 'A second, ordinary Instagram account does all the searching and profile reading, so @' + st.account + ' only sends. Create it by hand, use it normally for a few days, then turn it on here and log it in above.'),
        h('label', {class: 'inline-check'}, on, 'Use a research account'), field('Its username', user), h('div', {class: 'row'}, save))),
    h('div', {class: 'small muted'}, 'Official Meta API: ' + (st.api.connected ? `connected (${st.api.login_type} login)` : 'not connected (optional; see docs/RESEARCH.md)')));
}
function testCard(st) {
  const v = st.values;
  const input = h('input', {type: 'text', value: (v['rollout.allowed_targets'] || []).join(', '), placeholder: 'e.g. my_second_account', disabled: !st.can_edit});
  const save = h('button', {class: 'small primary', disabled: !st.can_edit}, 'Save');
  save.addEventListener('click', busy(save, () => {
    if (!input.value.trim() && st.environment === 'live' && !confirm('Turn test mode off? Messages can then go to any good-fit business (still only after your approval in Approval mode).')) return Promise.resolve();
    return saveSetup({'rollout.allowed_targets': input.value}, input.value.trim() ? 'Test mode saved' : 'Test mode is off');
  }));
  const targets = v['rollout.allowed_targets'] || [];
  return setCard('test', 'Test mode', targets.length ? 'on' : 'off',
    h('p', {class: 'small ink2'}, 'While this list has names, messages go only to these accounts: ideal for testing with your own second account.'),
    field('Only message these accounts', input, 'usernames, comma-separated; empty = test mode off'),
    h('div', {class: 'row'}, save, targets.length ? pill('warning', `only ${targets.map((t) => '@' + t).join(', ')}`) : pill('', 'off: any good-fit business')));
}
function limitsCard(st) {
  const inputs = {};
  const rows = LIMIT_FIELDS.map(([key, label]) => {
    inputs[key] = h('input', {type: 'number', min: 0, value: st.limits[key], placeholder: String(st.limit_defaults[key])});
    return field(label, inputs[key], key in st.limit_overrides ? `changed here (default ${st.limit_defaults[key]})` : `default ${st.limit_defaults[key]}`);
  });
  const save = h('button', {class: 'small primary'}, 'Save limits');
  save.addEventListener('click', busy(save, async () => {
    const overrides = {};
    for (const [key] of LIMIT_FIELDS) { const n = Number(inputs[key].value); if (inputs[key].value !== '' && n !== st.limits[key]) overrides[key] = n; }
    if (!Object.keys(overrides).length) { toast('Nothing changed'); return; }
    await act(() => patch('/api/limits', {overrides}), 'Limits saved: they apply at once');
    S.loadedAt.settings = 0; await loadPage(true);
  }));
  const reset = h('button', {class: 'small'}, 'Back to defaults');
  reset.addEventListener('click', busy(reset, async () => { await act(() => patch('/api/limits', {clear: true}), 'Limits are back to the defaults'); await loadPage(true); }));
  return setCard('limits', 'Daily limits', 'apply at once',
    h('p', {class: 'small ink2'}, 'The bot never goes over these, on top of spacing messages out and resting at night. Lower is safer for the account.'),
    h('div', {class: 'fields-2'}, rows), h('div', {class: 'row'}, save, reset));
}
function hoursPair(value, disabled) {
  const a = h('input', {type: 'time', value: value[0], disabled}); const b = h('input', {type: 'time', value: value[1], disabled});
  return {el: h('div', {class: 'hours'}, a, h('span', {class: 'muted'}, 'to'), b), read: () => [a.value, b.value]};
}
function hoursCard(st) {
  const send = hoursPair(st.values['schedule.send_hours'], !st.can_edit); const browse = hoursPair(st.values['schedule.browser_active_hours'], !st.can_edit);
  const save = h('button', {class: 'small primary', disabled: !st.can_edit}, 'Save hours');
  save.addEventListener('click', busy(save, () => saveSetup({'schedule.send_hours': send.read(), 'schedule.browser_active_hours': browse.read()}, 'Hours saved')));
  return setCard('hours', 'Hours', (S.overview && S.overview.timezone) || '',
    field('Send messages between', send.el, 'first messages and follow-ups wait outside these hours'),
    field('Use the browser between', browse.el, 'searching and checking profiles rests outside these hours'),
    h('div', {class: 'row'}, save));
}
function messagesCard(st) {
  const v = st.values; const off = !st.can_edit;
  const name = h('input', {type: 'text', value: v['offer.sender_name'], disabled: off});
  const site = h('input', {type: 'url', value: v['offer.website_url'], disabled: off});
  const link = h('input', {type: 'checkbox', checked: v['offer.include_link_in_first_message'], disabled: off});
  const score = h('input', {type: 'number', min: 0, max: 100, value: v['scoring.min_score_to_contact'], disabled: off});
  const followers = h('input', {type: 'number', min: 0, value: v['scoring.min_followers'], disabled: off});
  const save = h('button', {class: 'small primary', disabled: off}, 'Save');
  save.addEventListener('click', busy(save, () => saveSetup({'offer.sender_name': name.value, 'offer.website_url': site.value, 'offer.include_link_in_first_message': link.checked, 'scoring.min_score_to_contact': Number(score.value), 'scoring.min_followers': Number(followers.value)}, 'Saved')));
  return setCard('messages', 'Your messages', null,
    h('div', {class: 'fields-2'}, field('Your name in messages', name), field('Your website', site),
      field('Lowest score worth a message', score, 'businesses scoring less are not messaged (0-100)'),
      field('Fewest followers worth a message', followers, 'set 0 while testing with a brand-new account')),
    h('label', {class: 'inline-check'}, link, 'Put your website link in the first message'),
    h('div', {class: 'row'}, save));
}
function alertsCard(st) {
  const al = st.alerts; const off = !al.can_edit;
  const tokenInput = h('input', {type: 'password', autocomplete: 'off', placeholder: al.telegram_token_set ? 'saved (paste a new one to replace it)' : '123456789:AA…', disabled: off});
  const saveToken = h('button', {class: 'small', disabled: off}, 'Save token');
  saveToken.addEventListener('click', busy(saveToken, async () => { if (!tokenInput.value.trim()) return; const r = await act(() => post('/api/alerts/telegram', {bot_token: tokenInput.value.trim()}), 'Bot token saved in .env'); if (r) await loadPage(true); }));
  const result = h('div', {class: 'small'});
  const find = h('button', {class: 'small', disabled: off || !al.telegram_token_set}, 'Find my chat');
  find.addEventListener('click', busy(find, async () => {
    try {
      const r = await post('/api/alerts/find-chat');
      result.replaceChildren(r.message, ...r.chats.map((c) => h('button', {class: 'small', onclick: () => act(() => post('/api/alerts/telegram', {chat_id: c.id}), 'Chat saved').then(() => loadPage(true))}, c.name || c.id)));
      if (r.saved) await loadPage(true);
    } catch (e) { toast(String(e.message || e), true); }
  }));
  const test = h('button', {class: 'small primary', disabled: !al.telegram_ready && !al.webhook_set}, icon('bell'), 'Send a test alert');
  test.addEventListener('click', busy(test, async () => {
    try { const r = await post('/api/alerts/test'); toast(r.results.map((x) => `${x.to}: ${x.ok ? 'sent' : 'failed (' + x.error + ')'}`).join(' · '), !r.results.every((x) => x.ok)); } catch (e) { toast(String(e.message || e), true); }
  }));
  const level = h('select', {disabled: off}, [['INFO', 'Everything (also replies and handovers)'], ['WARNING', 'Problems (recommended)'], ['CRITICAL', 'Only when an account is stopped']].map(([k, label]) => h('option', {value: k, selected: al.min_severity === k}, label)));
  const url = h('input', {type: 'url', value: al.dashboard_url || '', placeholder: 'https://… (optional)', disabled: off});
  const save = h('button', {class: 'small', disabled: off}, 'Save');
  save.addEventListener('click', busy(save, () => saveSetup({'notifications.min_severity': level.value, 'notifications.dashboard_url': url.value.trim() || null}, 'Alert settings saved')));
  return setCard('alerts', 'Phone alerts (Telegram)', al.telegram_ready ? 'on' : 'off',
    h('div', {class: 'row'}, al.telegram_ready ? status('good', `alerts go to Telegram chat ${al.telegram_chat_id}`) : status('neutral', 'not set up yet')),
    field('1. Create a bot: in Telegram message @BotFather, send /newbot, paste the token it gives you', h('div', {class: 'row'}, tokenInput, saveToken), 'kept in the .env file on this computer, never shown again'),
    field('2. Open your new bot in Telegram and press Start, then', h('div', {class: 'row'}, find), null), result,
    field('3. Check it works', h('div', {class: 'row'}, test)),
    h('div', {class: 'fields-2'}, field('Which alerts reach your phone', level), field('Mission Control link in alerts', url, 'e.g. your Tailscale address')),
    h('div', {class: 'row'}, save), off ? h('p', {class: 'small muted'}, st.why_not) : null);
}
function checksCard() {
  const tone = {PASS: 'good', FAIL: 'critical', WARN: 'warning'};
  const o = S.overview || {};
  return setCard('checks', 'Go-live checks', o.simulated ? 'simulation: nothing to check' : 'needed for Autonomous mode',
    h('p', {class: 'small ink2'}, 'Autonomous mode on the real account is refused until every required check passes. Approval mode works without them.'),
    h('div', {id: 'checkRows'}, S.preflight.map((c) => h('div', {class: 'pf'}, status(tone[c.result] || 'neutral', c.result === 'PASS' ? 'ok' : c.result === 'FAIL' ? 'not yet' : 'note'),
      h('span', {}, h('strong', {}, (CHECK_NAME[c.check] || c.check) + ' '), h('span', {class: 'small ink2'}, c.detail))))));
}
function neverCard(list) {
  const input = h('input', {type: 'text', placeholder: '@username'}); const reason = h('input', {type: 'text', placeholder: 'reason (optional)'});
  const add = h('button', {class: 'small danger'}, 'Never contact');
  add.addEventListener('click', busy(add, async () => { if (!input.value.trim()) return; await act(() => post('/api/suppressions', {kind: 'USERNAME', value: input.value.trim().replace(/^@/, ''), reason: reason.value.trim() || 'manual'}), 'Added to the never-contact list'); await loadPage(true); }));
  return setCard('never', 'Never contact', plural(list.length, 'entry', 'entries'),
    h('p', {class: 'small ink2'}, 'Anyone who says stop is added automatically. Add people here by hand too.'),
    h('div', {class: 'row'}, input, reason, add),
    list.length ? h('div', {class: 'table-wrap'}, h('table', {}, h('tbody', {}, list.slice(0, 200).map((x) => h('tr', {},
      h('td', {}, x.kind === 'USERNAME' ? '@' + x.value : `${nice(x.kind)}: ${x.value}`), h('td', {class: 'small ink2'}, x.reason || ''),
      h('td', {class: 'small muted nowrap'}, day(x.created_at)),
      h('td', {}, h('button', {class: 'small', onclick: () => { if (confirm(`Remove ${x.value} from the never-contact list?`)) act(() => del(`/api/suppressions/${x.kind}/${encodeURIComponent(x.value)}`), 'Removed').then(() => loadPage(true)); }}, 'Remove'))))))) : null);
}
function safetyCard() {
  const box = h('pre', {class: 'lines'}, 'Loading…');
  const details = h('details', {}, h('summary', {}, 'Show every safety rule in force'), box);
  details.addEventListener('toggle', async () => { if (details.open) { try { box.textContent = (await api('/api/safety')).lines.join('\n'); } catch (e) { box.textContent = String(e.message || e); } } });
  return setCard('safety', 'Safety rules', 'always on', h('p', {class: 'small ink2'}, 'Limits, spacing, hours, duplicate protection and stop rules the bot follows whatever the mode.'), details);
}
function programCard(st) {
  const p = st.program;
  const theme = h('select', {'aria-label': 'Appearance'}, [['', 'Like my computer'], ['light', 'Light'], ['dark', 'Dark']].map(([k, label]) => h('option', {value: k, selected: (recall('io_theme') || '') === k}, label)));
  theme.addEventListener('change', () => { store('io_theme', theme.value || null); if (theme.value) document.documentElement.dataset.theme = theme.value; else delete document.documentElement.dataset.theme; });
  const restart = h('button', {class: 'small', disabled: !st.restart_supported}, icon('refresh'), 'Restart the program');
  restart.addEventListener('click', restartNow);
  return setCard('program', 'Program', 'version ' + p.version,
    h('div', {class: 'small ink2'}, 'Settings file: ', h('code', {}, st.files.config || 'none (demo)')),
    st.files.overlay ? h('div', {class: 'small ink2'}, 'Changes made here: ', h('code', {}, st.files.overlay)) : null,
    h('div', {class: 'small ink2'}, 'Database: ', h('code', {}, p.database), ' · secrets: ', h('code', {}, p.env_file)),
    h('div', {class: 'row'}, restart, st.restart_supported ? null : h('span', {class: 'small muted'}, 'started without `run`: restart it yourself')),
    field('Appearance', theme),
    h('div', {class: 'row'}, h('button', {class: 'small', onclick: () => { forgetToken(); toast('Token forgotten on this computer'); }}, 'Forget the control token here')));
}
const SETTINGS_SECTIONS = [['where', 'Where it runs'], ['accounts', 'Instagram accounts'], ['test', 'Test mode'], ['limits', 'Limits'], ['hours', 'Hours'], ['messages', 'Messages'], ['alerts', 'Phone alerts'], ['checks', 'Go-live checks'], ['never', 'Never contact'], ['safety', 'Safety rules'], ['program', 'Program']];
function renderSettings(st) {
  S.settingsBuilt = true;
  $('settingsNav').replaceChildren(...SETTINGS_SECTIONS.map(([id, label]) => h('button', {onclick: () => $('set-' + id).scrollIntoView({behavior: 'smooth', block: 'start'})}, label)));
  $('settings').replaceChildren(whereCard(st), accountsCard(st), testCard(st), limitsCard(st), hoursCard(st), messagesCard(st), alertsCard(st), checksCard(), neverCard(S.suppressions || []), safetyCard(), programCard(st));
  $('set-where').classList.add('full');
}
function updateSettingsLive(st) {  // only the parts that change by themselves: never the forms
  const rows = $('accountRows'); if (rows) rows.replaceChildren(...st.accounts.map((a) => accountRow(a, st)));
  const checks = $('set-checks'); if (checks) checks.replaceWith(checksCard());
}
async function loadSettings(force) {
  const [st, sup] = await Promise.all([api('/api/setup'), api('/api/suppressions'), pollPreflight()]);
  S.setup = st; S.suppressions = sup; S.setupAt = Date.now(); renderRestart();
  if (force === true || !S.settingsBuilt) renderSettings(st); else updateSettingsLive(st);  // never re-draw a form you are editing
}
const jobRunning = () => Boolean(S.setup && S.setup.accounts.some((a) => a.job && a.job.state === 'running'));

// ============================================================ registration
PAGES.dashboard = {load: loadDashboard, every: 10000, onEvent: /^(exec\.discover|exec\.inspect|lead\.|action\.|message\.sent|reply\.|conversation\.|lane\.|send\.)/};
PAGES.approvals = {load: loadApprovals, every: 20000, onEvent: /^(action\.|mode\.)/};
PAGES.leads = {load: loadLeads, every: 60000, onEvent: /^lead\./};
PAGES.chats = {load: loadChats, every: 15000, onEvent: /^(message\.sent|reply\.|conversation\.|lead\.stage)/};
PAGES.campaigns = {load: loadCampaigns};
PAGES.analytics = {load: loadAnalytics, every: 60000};
PAGES.activity = {load: loadHistory};
PAGES.problems = {load: loadProblems, every: 20000, onEvent: /^(incident\.|lane\.)/};
PAGES.settings = {load: loadSettings, every: () => (jobRunning() ? 2000 : 15000), onEvent: /^(browser\.|lane\.|settings\.|alerts\.|suppression\.|limits\.)/};

function bindPages() {
  buildKpis();
  for (const id of ['leadSearch', 'leadStatus', 'leadNiche']) $(id).addEventListener(id === 'leadSearch' ? 'input' : 'change', renderLeads);
  $('leadStatus').replaceChildren(...LEAD_FILTERS.map(([v, label]) => h('option', {value: v}, label)));
  $('addLeadBtn').addEventListener('click', addLead);
  $('addCampaign').addEventListener('click', newCampaign);
  $('findNow2').addEventListener('click', busy($('findNow2'), findNow));
  for (const b of $('rangeChips').querySelectorAll('button')) b.addEventListener('click', () => { store('io_days', b.dataset.days); loadAnalytics(); });
  $('historyBox').addEventListener('toggle', () => loadHistory());
}
