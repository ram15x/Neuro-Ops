// NeuroOps dashboard.
// One page, two data sources: a recording (the hosted demo) or the live API.
// Both hand over the same snapshot shape, so everything below draws from `S.cur`.

const C = { cpu: '#1e5aa8', mem: '#8b3a78', disk: '#a8700f', net: '#1b7c74', danger: '#c4342b',
  ok: '#2c7a4b', wait: '#b7791a', ink: '#17242b', muted: '#56666d', grid: '#d3dad6', sheet: '#f8f9f7' };
const TRACES = [['NetworkIn', C.net, 1.2], ['disk_used_percent', C.disk, 1.8], ['mem_used_percent', C.mem, 1.8], ['CPUUtilization', C.cpu, 1.8]];
const DAY = 288;

// plain words for what the engine reports
const CAUSE = { disk_fill: 'Disk filling up', memory_leak: 'Memory leak', memory_step: 'Memory jumped, then steady',
  runaway_process: 'Runaway process', traffic_surge: 'Traffic surge', cpu_contention: 'CPU contention',
  credit_exhaustion: 'Burst credits running out', network_loss: 'Traffic dropped' };
const FAMILY = { disk_fill: C.disk, memory_leak: C.mem, memory_step: C.mem, runaway_process: C.cpu, traffic_surge: C.cpu,
  cpu_contention: C.cpu, credit_exhaustion: C.cpu, network_loss: C.net };
const ACTION = { disk_cleanup: 'clean up disk', restart_service: 'restart the service', reboot: 'reboot', scale_out: 'add an instance',
  migrate_host: 'move to another host', enable_unlimited: 'switch to unlimited credits', notify: 'tell a person', observe: 'keep watching', none: 'nothing' };
const FLAG = { disk_cleanup: 'C', restart_service: 'R', reboot: 'B', scale_out: '+', migrate_host: 'M', enable_unlimited: 'U' };
const TRUTH = { cpu_runaway: 'runaway cpu', noisy_neighbour: 'noisy neighbour', memory_leak: 'memory leak', disk_fill: 'disk fill',
  traffic_surge: 'traffic surge', network_drop: 'traffic drop', credit_drain: 'stuck hot worker', deploy_step: 'release (harmless)', cron_burst: 'cron job (harmless)' };

const S = { live: false, recs: {}, cur: null, ghost: null, from: null, blend: 1, scenario: 'clean', mode: 'shadow',
  t: 0, playing: true, speed: 12, focus: null, truth: false, token: null, netMax: {}, pendingAfterLogin: null, lanes: {} };
const $ = (id) => document.getElementById(id);
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---------- data ----------
async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw Object.assign(new Error(url + ' ' + r.status), { status: r.status });
  return r.json();
}

async function boot() {
  const q = new URLSearchParams(location.search), want = q.get('source');
  let index = null;
  if (want !== 'live') {
    try { index = await getJSON('replay/index.json'); } catch (e) { index = null; }
  }
  if (index) {
    for (const rec of index.recordings) S.recs[rec.scenario + '-' + rec.mode] = 'replay/' + rec.file;
    
    await pick(q.get('scenario') === 'hard' ? 'hard' : 'clean', q.get('mode') === 'auto' ? 'auto' : 'shadow', true);
    // start a little before the first thing happens, so the page opens on action
    const first = Math.min(...S.cur.incidents.map((i) => i.opened_i));
    S.t = Math.max(S.cur.meta.watching_from, (isFinite(first) ? first : DAY) - 30);
    S.playing = !reduced;
    // ?t=<step> opens at a given moment, paused (handy for sharing a link to one incident)
    if (q.get('t')) { S.t = Math.max(12, Math.min(S.cur.meta.steps - 1, +q.get('t'))); S.playing = false; }
    if (q.get('focus') && S.cur.instances[+q.get('focus')]) S.focus = S.cur.instances[+q.get('focus')].id;
  } else {
    S.live = true;
    await pollLive();
    setInterval(pollLive, 2000);
  }
  buildLanes();
  chrome();
  requestAnimationFrame(frame);
}

async function load(key) {
  if (typeof S.recs[key] === 'string') S.recs[key] = await getJSON(S.recs[key]);
  return S.recs[key];
}

async function pick(scenario, mode, first) {
  const next = await load(scenario + '-' + mode);
  const sameRun = S.cur && scenario === S.scenario;
  // switching mode inside one scenario: slide the traces from one recording to the other
  S.from = sameRun && !reduced ? S.cur : null;
  S.blend = S.from ? 0 : 1;
  S.cur = next;
  S.ghost = mode === 'auto' ? await load(scenario + '-shadow') : null;
  S.scenario = scenario; S.mode = mode;
  prep(next);
  if (!first) { if (!sameRun) S.t = Math.min(S.t, next.meta.steps - 1); chrome(); }
}

function prep(d) {
  S.netMax = {};
  for (const inst of d.instances) {
    const v = d.metrics[inst.id].NetworkIn;
    S.netMax[inst.id] = Math.max(1e-9, ...v) * 1.05;
  }
}

async function pollLive() {
  try {
    const d = await getJSON('../api/v1/engine/snapshot?points=576');
    const pinned = !S.cur || S.t >= S.cur.meta.steps - 2;
    S.cur = d; S.mode = d.meta.mode; prep(d);
    if (pinned) S.t = d.meta.steps - 1;
    if (!Object.keys(S.lanes).length) buildLanes();
    chrome();
  } catch (e) {
    $('source').textContent = 'Cannot reach the engine API. Is the backend running?';
  }
}

// ---------- state at the playhead ----------
function at(t) {
  // replay the audit log up to t to know where every incident stands
  const d = S.cur, st = new Map();
  for (const inc of d.incidents) {
    if (inc.opened_i <= t) st.set(inc.id, { inc, status: 'open', verdict: null, reasons: [], action: null, closed: false, approval: null });
  }
  let actions = 0, waiting = 0;
  for (const e of d.audit) {
    if (e.i > t) break;
    const s = st.get(e.incident);
    if (e.type === 'action' && e.ok) actions++;
    if (!s) continue;
    if (e.type === 'decision') { s.verdict = e.status; s.action = e.action; s.reasons = e.reasons || []; }
    else if (e.type === 'action') { s.verdict = e.ok ? 'ran' : 'failed'; s.action = e.action; s.reasons = [e.detail]; }
    else if (e.type === 'verified') { s.verdict = 'verified'; s.status = 'mitigated'; }
    else if (e.type === 'verify_failed') { s.verdict = 'not_verified'; }
    else if (e.type === 'escalated') { s.verdict = 'person'; s.reasons = e.reasons || []; }
    else if (e.type === 'rejected') { s.verdict = 'rejected'; }
    else if (e.type === 'incident_resolved') { s.closed = true; }
  }
  for (const a of d.approvals || []) {
    if (a.i > t) continue;
    const s = st.get(a.incident);
    if (!s || s.closed) continue;
    // in a recording the answer is in the audit log, live it is the current status
    const answered = S.live ? a.status !== 'pending' : d.audit.some((e) => e.i <= t && e.incident === a.incident && e.action === a.action && (e.type === 'approved' || e.type === 'rejected'));
    if (!answered && s.verdict === 'needs_approval') { s.approval = a; waiting++; }
  }
  return { list: [...st.values()], actions, waiting };
}

function tallyText(now) {
  const d = S.cur, t = S.t;
  if (!d.truth) {
    const open = now.list.filter((s) => !s.closed).length;
    return `<b>${open}</b> open, <b>${now.actions}</b> actions taken, <b>${now.waiting}</b> waiting for approval.`;
  }
  const real = d.truth.filter((e) => !e.benign && e.start_i <= t);
  // a ramp counts as breached once it crosses the line without being stopped first
  const breached = real.filter((e) => e.breach_i != null && e.breach_i <= t && (e.stopped_i == null || e.stopped_i > e.breach_i)).length;
  const b = breached ? `<b class="bad">${breached}</b> reached the danger line` : `<b class="good">none</b> reached the danger line`;
  const w = now.waiting ? `, <b>${now.waiting}</b> waiting for approval` : '';
  return `<b>${real.length}</b> faults so far, ${b}, <b>${now.actions}</b> actions taken${w}.`;
}

// ---------- drawing helpers ----------
function fit(canvas) {
  const r = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [g, r.width, r.height];
}
const label = (g, size, weight = 500, stretch = 'condensed') => { g.font = `${weight} ${size}px Archivo, sans-serif`; if ('fontStretch' in g) g.fontStretch = stretch; };
const when = (i) => new Date(new Date(S.cur.meta.start).getTime() + i * 5 * 60000);
const hhmm = (dt) => dt.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' });
const dayName = (dt) => dt.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });

function value(data, iid, metric, i) {
  const a = data.metrics[iid][metric];
  return a ? a[Math.max(0, Math.min(a.length - 1, i))] : null;
}
function shown(iid, metric, i) {
  // while a mode switch is sliding, mix the old and new recordings
  const v = value(S.cur, iid, metric, i);
  if (S.blend >= 1 || !S.from) return v;
  const o = value(S.from, iid, metric, i);
  return o == null ? v : o + (v - o) * S.blend;
}

// ---------- lanes ----------
function buildLanes() {
  const host = $('lanes');
  host.innerHTML = '';
  S.lanes = {};
  for (const inst of S.cur.instances) {
    const n = S.cur.instances.filter((x) => x.role === inst.role).indexOf(inst) + 1;
    const row = document.createElement('div');
    row.className = 'lane';
    row.innerHTML = `<button type="button" class="who" aria-expanded="false">
        <span class="name">${inst.role} ${n}</span><span class="kind">${inst.type}, ${inst.id.slice(0, 9)}</span>
        <span class="vals"><span class="c"></span><span class="m"></span><span class="d"></span></span>
      </button><canvas></canvas>`;
    host.appendChild(row);
    const canvas = row.querySelector('canvas'), btn = row.querySelector('.who');
    btn.addEventListener('click', () => { S.focus = S.focus === inst.id ? null : inst.id; chrome(); });
    canvas.addEventListener('mousemove', (ev) => hover(ev, inst, canvas));
    canvas.addEventListener('mouseleave', () => { $('tip').hidden = true; });
    S.lanes[inst.id] = { row, canvas, btn, inst, name: `${inst.role} ${n}` };
  }
}

function span(focusOn) { return focusOn ? DAY * 2 : DAY; }

function drawLane(lane, now) {
  const { canvas, inst } = lane, big = S.focus === inst.id;
  const [g, W, H] = fit(canvas);
  const win = span(big), t = S.t, t0 = t - win;
  const X = (i) => (i - t0) / win * (W - 46);
  const top = 12, bottom = H - (big ? 18 : 6);
  const Y = (pct) => bottom - (pct / 100) * (bottom - top);
  g.clearRect(0, 0, W, H);

  // the time before the engine starts watching
  const wf = S.cur.meta.watching_from || 0;
  if (wf > t0) { g.fillStyle = '#eef1ef'; g.fillRect(0, 0, Math.min(W, X(wf)), H); }

  // graph paper: a line every 3 hours, heavier at midnight
  for (let i = Math.ceil(t0 / 36) * 36; i <= t; i += 36) {
    const dt = when(i), mid = dt.getUTCHours() === 0;
    g.strokeStyle = mid ? '#b9c3be' : '#e3e8e5'; g.lineWidth = 1;
    g.beginPath(); g.moveTo(X(i) + 0.5, 0); g.lineTo(X(i) + 0.5, H); g.stroke();
    if (big) { label(g, 10); g.fillStyle = C.muted; g.fillText(mid ? dayName(dt) : hhmm(dt), X(i) + 3, H - 5); }
  }
  g.strokeStyle = '#e3e8e5'; g.beginPath(); g.moveTo(0, Y(50)); g.lineTo(W, Y(50)); g.stroke();
  g.strokeStyle = C.danger; g.setLineDash([2, 3]); g.beginPath(); g.moveTo(0, Y(90)); g.lineTo(W - 46, Y(90)); g.stroke(); g.setLineDash([]);

  // injected faults, hatched along the bottom
  if (S.truth && S.cur.truth) {
    for (const e of S.cur.truth) {
      if (e.instance_id !== inst.id || e.end_i < t0 || e.start_i > t) continue;
      const a = Math.max(0, X(e.start_i)), b = X(Math.min(e.end_i, t));
      g.save(); g.beginPath(); g.rect(a, bottom - 9, b - a, 9); g.clip();
      g.strokeStyle = e.benign ? C.muted : C.ink; g.lineWidth = 1;
      for (let x = a - 10; x < b; x += 5) { g.beginPath(); g.moveTo(x, bottom); g.lineTo(x + 9, bottom - 9); g.stroke(); }
      g.restore();
      if (big && b - a > 60) { label(g, 10, 600); g.fillStyle = C.ink; g.fillText(TRUTH[e.kind] || e.kind, a + 3, bottom - 12); }
    }
  }

  const lo = Math.max(0, Math.floor(t0)), hi = Math.floor(t);
  const line = (data, metric, color, width, dash, mix) => {
    if (!data.metrics[inst.id][metric]) return;
    g.strokeStyle = color; g.lineWidth = width; g.setLineDash(dash || []); g.lineJoin = 'round';
    g.beginPath();
    for (let i = lo; i <= hi; i++) {
      let v = mix ? shown(inst.id, metric, i) : value(data, inst.id, metric, i);
      if (metric === 'NetworkIn') v = v / S.netMax[inst.id] * 100;
      i === lo ? g.moveTo(X(i), Y(v)) : g.lineTo(X(i), Y(v));
    }
    g.stroke(); g.setLineDash([]);
  };

  // what would have happened without healing, drawn only where it differs
  if (S.ghost && S.blend >= 1) {
    for (const [metric] of TRACES.slice(1)) {
      const a = S.ghost.metrics[inst.id][metric], b = S.cur.metrics[inst.id][metric];
      let differs = false;
      for (let i = lo; i <= hi; i += 3) if (Math.abs(a[i] - b[i]) > 1.5) { differs = true; break; }
      if (differs) line(S.ghost, metric, C.muted, 1.2, [4, 3]);
    }
  }
  for (const [metric, color, width] of TRACES) line(S.cur, metric, color, big ? width + 0.4 : width, null, true);
  if (big && S.cur.metrics[inst.id].CPUCreditBalance) {
    // burst credits on their own scale (0 to 576), thin and dotted
    g.strokeStyle = C.cpu; g.lineWidth = 1; g.setLineDash([1, 3]); g.beginPath();
    for (let i = lo; i <= hi; i++) { const y = Y(value(S.cur, inst.id, 'CPUCreditBalance', i) / 576 * 100); i === lo ? g.moveTo(X(i), y) : g.lineTo(X(i), y); }
    g.stroke(); g.setLineDash([]);
  }

  // incidents along the top edge, in the colour of the metric they are about
  let hit = false;
  for (const s of now.list) {
    const inc = s.inc;
    if (inc.target !== inst.id && inc.target !== inst.role) continue;
    const end = inc.closed_i != null && inc.closed_i <= t ? inc.closed_i : t;
    if (end < t0) continue;
    const a = Math.max(0, X(inc.opened_i)), b = X(end);
    g.fillStyle = FAMILY[inc.cause] || C.ink;
    g.globalAlpha = s.closed ? 0.35 : 1;
    g.fillRect(a, 0, Math.max(2, b - a), 5);
    g.globalAlpha = 1;
    if (!s.closed && inc.cause !== 'memory_step') hit = true;
    if (big && b - a > 70) { label(g, 10, 700); g.fillStyle = FAMILY[inc.cause]; g.fillText(CAUSE[inc.cause], a + 2, 16); }
  }
  lane.row.classList.toggle('hit', hit);

  // actions: a flag where the engine did something, ringed green once verified
  for (const e of S.cur.audit) {
    if (e.i > t) break;
    if (e.i < t0 || e.type !== 'action' || !e.ok) continue;
    if (e.target !== inst.id && e.target !== inst.role) continue;
    const x = X(e.i), ok = S.cur.audit.find((v) => v.incident === e.incident && v.action === e.action && v.i > e.i && v.i <= t && (v.type === 'verified' || v.type === 'verify_failed'));
    g.strokeStyle = C.ink; g.lineWidth = 1; g.beginPath(); g.moveTo(x + 0.5, 5); g.lineTo(x + 0.5, bottom); g.stroke();
    g.fillStyle = !ok ? C.ink : ok.type === 'verified' ? C.ok : C.danger;
    g.beginPath(); g.arc(x + 0.5, 13, 7.5, 0, 7); g.fill();
    label(g, 10, 800, 'normal'); g.fillStyle = C.sheet; g.textAlign = 'center'; g.fillText(FLAG[e.action] || '!', x + 0.5, 16.5); g.textAlign = 'left';
  }

  // current values at the pen tip
  const tip = Math.floor(t);
  label(g, 11, 700);
  let lastY = -99;
  for (const [metric, color] of TRACES.slice(1).reverse()) {
    const v = shown(inst.id, metric, tip);
    g.fillStyle = color; g.beginPath(); g.arc(X(tip), Y(v), 2.6, 0, 7); g.fill();
    if (big) { let y = Y(v) + 4; if (Math.abs(y - lastY) < 11) y = lastY + 11; lastY = y; g.fillText(v.toFixed(0) + '%', X(tip) + 7, y); }
  }
  const vals = lane.row.querySelectorAll('.vals span');
  vals[0].textContent = 'cpu ' + shown(inst.id, 'CPUUtilization', tip).toFixed(0);
  vals[1].textContent = 'mem ' + shown(inst.id, 'mem_used_percent', tip).toFixed(0);
  vals[2].textContent = 'disk ' + shown(inst.id, 'disk_used_percent', tip).toFixed(0);
}

function hover(ev, inst, canvas) {
  const r = canvas.getBoundingClientRect(), big = S.focus === inst.id, win = span(big);
  const i = Math.round(S.t - win + (ev.clientX - r.left) / (r.width - 46) * win);
  if (i < 0 || i > S.t) { $('tip').hidden = true; return; }
  const dt = when(i), m = S.cur.metrics[inst.id];
  let html = `<b>${dayName(dt)}, ${hhmm(dt)}</b>
    <span>cpu ${m.CPUUtilization[i].toFixed(1)}%</span><span>memory ${m.mem_used_percent[i].toFixed(1)}%</span>
    <span>disk ${m.disk_used_percent[i].toFixed(1)}%</span><span>traffic in ${m.NetworkIn[i].toFixed(2)} MB per 5 min</span>`;
  if (m.CPUCreditBalance) html += `<span>burst credits ${m.CPUCreditBalance[i].toFixed(0)}</span>`;
  if (S.ghost) {
    const gm = S.ghost.metrics[inst.id];
    for (const [k, name] of [['CPUUtilization', 'cpu'], ['mem_used_percent', 'memory'], ['disk_used_percent', 'disk']]) {
      if (Math.abs(gm[k][i] - m[k][i]) > 1.5) html += `<span class="g">without healing: ${name} ${gm[k][i].toFixed(1)}%</span>`;
    }
  }
  const tip = $('tip');
  tip.innerHTML = html; tip.hidden = false;
  tip.style.left = Math.min(window.innerWidth - 270, ev.clientX + 14) + 'px';
  tip.style.top = (ev.clientY + 14) + 'px';
}

// ---------- scrubber ----------
function drawScrub(now) {
  const canvas = $('scrub'), [g, W, H] = fit(canvas), d = S.cur, N = d.meta.steps;
  const X = (i) => i / (N - 1) * W;
  g.clearRect(0, 0, W, H);
  const wf = d.meta.watching_from || 0;
  if (wf) {
    g.fillStyle = '#eef1ef'; g.fillRect(0, 0, X(wf), H);
    label(g, 10); g.fillStyle = C.muted; g.fillText('learning', 5, H - 5);
  }
  for (let i = 0; i < N; i += 12) {
    const dt = when(i);
    if (dt.getUTCHours() !== 0 || dt.getUTCMinutes() !== 0) continue;
    g.strokeStyle = '#cfd7d2'; g.beginPath(); g.moveTo(X(i) + 0.5, 0); g.lineTo(X(i) + 0.5, H); g.stroke();
    label(g, 10); g.fillStyle = C.muted; g.fillText(dayName(dt), X(i) + 4, 11);
  }
  // one row per instance, one bar per incident
  const rows = d.instances, rh = (H - 22) / rows.length;
  rows.forEach((inst, r) => {
    const y = 16 + r * rh;
    if (S.truth && d.truth) {
      g.fillStyle = '#c9d1cc';
      for (const e of d.truth) if (e.instance_id === inst.id) g.fillRect(X(e.start_i), y + rh - 3, Math.max(2, X(e.end_i) - X(e.start_i)), 2);
    }
    for (const inc of d.incidents) {
      if (inc.target !== inst.id && inc.target !== inst.role) continue;
      const end = inc.closed_i != null ? inc.closed_i : N - 1;
      g.fillStyle = FAMILY[inc.cause] || C.ink;
      g.globalAlpha = inc.opened_i <= S.t ? 1 : 0.28;
      g.fillRect(X(inc.opened_i), y, Math.max(2, X(end) - X(inc.opened_i)), Math.max(2, rh - 4));
    }
    g.globalAlpha = 1;
  });
  for (const e of d.audit) {
    if (e.type !== 'action' || !e.ok) continue;
    g.fillStyle = C.ink; g.globalAlpha = e.i <= S.t ? 1 : 0.3;
    g.beginPath(); g.moveTo(X(e.i) - 3, H); g.lineTo(X(e.i) + 3, H); g.lineTo(X(e.i), H - 6); g.fill();
  }
  g.globalAlpha = 1;
  // the day the lanes are showing, and the playhead
  g.fillStyle = 'rgba(23,36,43,0.07)'; g.fillRect(X(S.t - DAY), 0, X(S.t) - X(S.t - DAY), H);
  g.strokeStyle = C.ink; g.lineWidth = 2; g.beginPath(); g.moveTo(X(S.t), 0); g.lineTo(X(S.t), H); g.stroke();
  canvas.setAttribute('aria-valuemax', N - 1); canvas.setAttribute('aria-valuenow', Math.floor(S.t));
  canvas.setAttribute('aria-valuetext', dayName(when(S.t)) + ' ' + hhmm(when(S.t)));
}

function seek(ev) {
  const r = $('scrub').getBoundingClientRect();
  const i = (ev.clientX - r.left) / r.width * (S.cur.meta.steps - 1);
  S.t = Math.max(12, Math.min(S.cur.meta.steps - 1, i));
  dirty();
}

// ---------- rail ----------
function verdictLine(s) {
  const a = ACTION[s.action] || s.action;
  switch (s.verdict) {
    case 'shadow': return ['wait', `Would ${a}`];
    case 'executed': case 'ran': return ['ran', `Ran: ${a}. Checking it worked`];
    case 'verified': return ['ran', `Fixed by: ${a}`];
    case 'not_verified': return ['held', `${a[0].toUpperCase() + a.slice(1)} did not help, trying the next step`];
    case 'needs_approval': return ['wait', `Waiting for approval to ${a}`];
    case 'blocked': return ['held', `Holding back: ${a}`];
    case 'notify': case 'person': return ['person', 'Handed to a person'];
    case 'rejected': return ['held', `Operator said no to: ${a}`];
    case 'failed': return ['held', `Could not ${a}`];
    case 'none': return ['', s.action === 'observe' ? 'Watching before acting' : 'No action needed'];
    default: return ['', 'Looking at it'];
  }
}

let railKey = '';
function drawRail(now) {
  const open = now.list.filter((s) => !s.closed).sort((a, b) => b.inc.opened_i - a.inc.opened_i);
  const tNow = Math.floor(S.t);
  const events = [];
  for (let k = S.cur.audit.length - 1; k >= 0 && events.length < 40; k--) if (S.cur.audit[k].i <= tNow) events.push(S.cur.audit[k]);
  // only touch the page when something actually changed
  const key = open.map((s) => s.inc.id + s.verdict + (s.approval ? 'a' : '')).join('|') + '#' + (events[0] ? events[0].i + events[0].type + events.length : '') + S.mode + S.scenario;
  if (key === railKey) return;
  railKey = key;

  const host = $('open');
  host.innerHTML = open.length ? '' : '<p class="empty">Nothing open. The fleet looks normal.</p>';
  for (const s of open) {
    const inc = s.inc, [cls, text] = verdictLine(s);
    const where = inc.scope === 'tier' ? `whole ${inc.target} tier` : (S.lanes[inc.target] ? S.lanes[inc.target].name : inc.target);
    const mins = (tNow - inc.opened_i) * 5;
    const card = document.createElement(S.live && s.approval ? 'div' : 'button');
    card.className = 'card'; card.style.setProperty('--fam', FAMILY[inc.cause] || C.ink);
    if (card.tagName === 'BUTTON') card.type = 'button';
    card.innerHTML = `<span class="head"><span class="cause">${CAUSE[inc.cause] || inc.cause}</span>
        <span class="where">${where}, ${mins < 60 ? mins + ' min' : (mins / 60).toFixed(1) + ' h'}</span></span>
      <span class="why">${esc((inc.evidence || [])[0] || '')}</span>
      <span class="verdict ${cls}">${text}</span>
      ${['needs_approval', 'blocked', 'person', 'notify', 'shadow'].includes(s.verdict) && s.reasons.length ? `<ul class="reasons">${s.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}`;
    if (S.live && s.approval) {
      const row = document.createElement('div'); row.className = 'answer';
      row.innerHTML = '<button type="button">Approve</button><button type="button" class="quiet">Reject</button>';
      row.children[0].onclick = () => answer(s.approval.id, 'approve');
      row.children[1].onclick = () => answer(s.approval.id, 'reject');
      card.appendChild(row);
    }
    card.addEventListener('click', (ev) => { if (ev.target.closest('.answer')) return; const iid = inc.scope === 'tier' ? S.cur.instances.find((x) => x.role === inc.target).id : inc.target; S.focus = iid; chrome(); S.lanes[iid].row.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' }); });
    host.appendChild(card);
  }

  $('log').innerHTML = events.map((e) => {
    const who = S.lanes[e.target] ? S.lanes[e.target].name : (e.target ? e.target + ' tier' : '');
    const a = ACTION[e.action] || e.action;
    let text, cls = 't-' + e.type;
    switch (e.type) {
      case 'incident_opened': text = `Noticed: ${CAUSE[e.cause] || e.cause} on ${who}`; break;
      case 'decision': {
        const map = { shadow: `Would ${a} on ${who} (shadow mode, not run)`, executed: `Decided to ${a} on ${who}`,
          needs_approval: `Asked for approval to ${a} on ${who}`, blocked: `Held back from ${who}: ${(e.reasons || [])[0] || ''}`,
          notify: `Out of safe fixes for ${who}`, none: e.action === 'observe' ? `Watching ${who} before acting` : `No action for ${who}: ${(e.reasons || [])[0] || ''}` };
        text = map[e.status] || `${e.status} ${a}`; if (e.status === 'needs_approval') cls = 't-wait'; break;
      }
      case 'action': text = e.ok ? `Ran: ${a} on ${who}` : `Failed to ${a} on ${who}: ${e.detail}`; break;
      case 'verified': text = `Checked: ${a} worked on ${who}`; break;
      case 'verify_failed': text = `Checked: ${a} did not help ${who}`; break;
      case 'approved': text = `Approved by ${e.by === 'evaluation-auto-approver' ? 'the stand-in operator' : e.by}: ${a} on ${who}`; break;
      case 'rejected': text = `Rejected by ${e.by === 'evaluation-auto-approver' ? 'the stand-in operator' : e.by}: ${a} on ${who}`; break;
      case 'escalated': text = `Handed ${who} to a person`; break;
      case 'rolled_back': text = `Put back: ${a} on ${who}, the trouble is over`; break;
      case 'incident_resolved': text = `Closed: ${CAUSE[e.cause] || e.cause} on ${who}, ${e.how}`; break;
      case 'reopened': text = `Back again: ${CAUSE[e.cause] || e.cause} on ${who}`; break;
      case 'mode_changed': text = `Mode changed from ${e.old} to ${e.new} by ${e.by}`; break;
      default: text = e.type;
    }
    const dt = when(e.i);
    return `<li><time>${dt.getUTCDate()} ${dt.toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' })} ${hhmm(dt)}</time><span class="${cls}">${esc(text)}</span></li>`;
  }).join('') || '<li><time></time><span>Nothing yet.</span></li>';
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ---------- page chrome ----------
function chrome() {
  const d = S.cur, m = d.meta;
  document.querySelectorAll('#scenarioSeg button').forEach((b) => b.setAttribute('aria-pressed', b.dataset.scenario === S.scenario));
  document.querySelectorAll('#modeSeg button').forEach((b) => b.setAttribute('aria-pressed', b.dataset.mode === S.mode));
  $('scenarioSeg').hidden = S.live;
  document.querySelector('#modeSeg [data-mode="approval"]').hidden = !S.live;
  $('truthWrap').hidden = !d.truth;
  const sim = m.simulated ? '<b>Simulated fleet</b>' : '<b>Live AWS account</b>';
  $('source').innerHTML = S.live ? `${sim}, live from the engine API` : `${sim}, recorded run. No real servers are involved.`;
  const modeNote = { shadow: 'Shadow mode: the engine decides and records, but runs nothing.',
    approval: 'Approval mode: every action waits for a person.',
    auto: S.live ? 'Auto mode: the engine runs what its guardrails allow and asks for the rest.'
      : 'Auto mode: the engine runs what its guardrails allow. A stand-in operator approves the rest after 15 minutes.' }[S.mode];
  $('lede').textContent = (S.live ? '' : 'Seven servers, eleven days, faults injected at known times. ') + modeNote
    + (S.scenario === 'hard' && !S.live ? ' Hard faults are weaker, noisier and overlap, and the engine gets some of them wrong.' : '');
  $('footnote').innerHTML = (m.simulated ? 'Everything on this page is simulated. The simulator decides which action fixes which fault, so this shows the engine\'s logic, not proof that these fixes work on real machines. ' : '')
    + 'Flags on a lane: C clean up disk, R restart service, B reboot, + add an instance, M move host, U unlimited credits. Green means the check after the action passed. '
    + '<a href="https://github.com/ram15x/NeuroOps">Code and scorecard on GitHub</a>.';
  for (const id in S.lanes) {
    const on = S.focus === id;
    S.lanes[id].row.classList.toggle('focus', on);
    S.lanes[id].btn.setAttribute('aria-expanded', on);
  }
  $('playIcon').setAttribute('d', S.playing ? 'M5 3h4v14H5zM11 3h4v14h-4z' : 'M6 3l11 7-11 7z');
  $('play').setAttribute('aria-label', S.playing ? 'Pause' : 'Play');
  railKey = '';
  dirty();
}

let needsDraw = true, last = performance.now();
const dirty = () => { needsDraw = true; };
function frame(ts) {
  const dt = Math.min(0.1, (ts - last) / 1000); last = ts;
  if (S.playing && !S.live && S.cur) {
    S.t += dt * S.speed;
    if (S.t >= S.cur.meta.steps - 1) { S.t = S.cur.meta.steps - 1; S.playing = false; chrome(); }
    needsDraw = true;
  }
  if (S.blend < 1) { S.blend = Math.min(1, S.blend + dt / 0.6); needsDraw = true; }
  if (needsDraw && S.cur) {
    needsDraw = false;
    const now = at(S.t);
    for (const id in S.lanes) drawLane(S.lanes[id], now);
    drawScrub(now);
    drawRail(now);
    const dtm = when(S.t);
    $('clock').textContent = `${dayName(dtm)}, ${hhmm(dtm)}`;
    $('tally').innerHTML = tallyText(now);
  }
  requestAnimationFrame(frame);
}

// ---------- controls ----------
$('play').addEventListener('click', () => {
  if (S.live) { S.t = S.cur.meta.steps - 1; dirty(); return; }
  if (!S.playing && S.t >= S.cur.meta.steps - 2) S.t = S.cur.meta.watching_from || 0;
  S.playing = !S.playing; chrome();
});
$('speed').addEventListener('change', (e) => { S.speed = +e.target.value; });
$('truth').addEventListener('change', (e) => { S.truth = e.target.checked; dirty(); });
document.querySelectorAll('#scenarioSeg button').forEach((b) => b.addEventListener('click', async () => {
  await pick(b.dataset.scenario, S.mode);
  const first = Math.min(...S.cur.incidents.map((i) => i.opened_i));
  S.t = Math.max(S.cur.meta.watching_from, first - 30); dirty();
}));
document.querySelectorAll('#modeSeg button').forEach((b) => b.addEventListener('click', () => {
  if (S.live) return authed(() => post('../api/v1/engine/mode', { mode: b.dataset.mode }).then(pollLive));
  pick(S.scenario, b.dataset.mode);
}));

const scrub = $('scrub');
let dragging = false;
scrub.addEventListener('pointerdown', (e) => { dragging = true; scrub.setPointerCapture(e.pointerId); seek(e); });
scrub.addEventListener('pointermove', (e) => { if (dragging) seek(e); });
scrub.addEventListener('pointerup', () => { dragging = false; });
function nudge(steps) { S.t = Math.max(12, Math.min(S.cur.meta.steps - 1, S.t + steps)); dirty(); }
scrub.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft') { nudge(-12); e.preventDefault(); }
  if (e.key === 'ArrowRight') { nudge(12); e.preventDefault(); }
});
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, select, dialog, button')) return;
  if (e.key === ' ') { $('play').click(); e.preventDefault(); }
});
window.addEventListener('resize', dirty);

// ---------- live only: sign in, approvals, mode ----------
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + S.token }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) { S.token = null; throw Object.assign(new Error('signed out'), { status: 401 }); }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Request failed');
  return r.json();
}
function authed(fn) {
  if (S.token) return fn().catch((e) => { if (e.status === 401) authed(fn); else alertLine(e.message); });
  S.pendingAfterLogin = fn; $('loginErr').hidden = true; $('login').showModal();
}
function alertLine(msg) { $('source').textContent = msg; }
const answer = (id, verdict) => authed(() => post(`../api/v1/engine/approvals/${id}/${verdict}`).then(pollLive));
$('loginCancel').addEventListener('click', () => $('login').close());
$('loginForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    const r = await fetch('../api/v1/auth/login', { method: 'POST', body: new URLSearchParams(f) });
    if (!r.ok) throw new Error('Wrong username or password.');
    S.token = (await r.json()).access_token;
    $('login').close(); e.target.reset();
    const fn = S.pendingAfterLogin; S.pendingAfterLogin = null;
    if (fn) authed(fn);
  } catch (err) { $('loginErr').textContent = err.message; $('loginErr').hidden = false; }
});

boot();
