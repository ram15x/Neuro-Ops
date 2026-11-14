// NeuroOps dashboard.
// One page, two data sources: a recording (the hosted demo) or the live API.
// Both hand over the same snapshot shape, so everything below draws from `S.cur`.

// colours are read from the stylesheet, so light and dark share one set of names
let C = {}, TRACES = [], FAMILY = {};
function readColors() {
  const cs = getComputedStyle(document.documentElement), v = (n) => cs.getPropertyValue('--' + n).trim();
  C = { cpu: v('cpu'), mem: v('mem'), disk: v('disk'), net: v('net'), danger: v('danger'), ok: v('ok'), wait: v('wait'),
    ink: v('ink'), muted: v('muted'), grid: v('grid'), sheet: v('sheet'), line: v('line'), dim: v('dim') };
  TRACES = [['NetworkIn', C.net, 1.2], ['disk_used_percent', C.disk, 1.8], ['mem_used_percent', C.mem, 1.8], ['CPUUtilization', C.cpu, 1.8]];
  FAMILY = { disk_fill: C.disk, memory_leak: C.mem, memory_step: C.mem, runaway_process: C.cpu, traffic_surge: C.cpu,
    cpu_contention: C.cpu, credit_exhaustion: C.cpu, network_loss: C.net };
}
const DAY = 288;

// plain words for what the engine reports
const CAUSE = { disk_fill: 'Disk filling up', memory_leak: 'Memory leak', memory_step: 'Memory jumped, then steady',
  runaway_process: 'Runaway process', traffic_surge: 'Traffic surge', cpu_contention: 'CPU contention',
  credit_exhaustion: 'Burst credits running out', network_loss: 'Traffic dropped' };
const ACTION = { disk_cleanup: 'clean up disk', restart_service: 'restart the service', reboot: 'reboot', scale_out: 'add an instance',
  migrate_host: 'move to another host', enable_unlimited: 'switch to unlimited credits', notify: 'tell a person', observe: 'keep watching', none: 'nothing' };
const FLAG = { disk_cleanup: 'C', restart_service: 'R', reboot: 'B', scale_out: '+', migrate_host: 'M', enable_unlimited: 'U' };
const TRUTH = { cpu_runaway: 'runaway cpu', noisy_neighbour: 'noisy neighbour', memory_leak: 'memory leak', disk_fill: 'disk fill',
  traffic_surge: 'traffic surge', network_drop: 'traffic drop', credit_drain: 'stuck hot worker', deploy_step: 'release (harmless)', cron_burst: 'cron job (harmless)' };

const S = { hover: null, hl: null, disp: {}, seen: new Set(), live: false, recs: {}, cur: null, ghost: null, from: null, blend: 1, scenario: 'clean', mode: 'shadow',
  t: 0, playing: true, speed: 12, focus: null, truth: false, token: null, netMax: {}, pendingAfterLogin: null, lanes: {} };
const $ = (id) => document.getElementById(id);
const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

// ---------- data ----------
async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw Object.assign(new Error(url + ' ' + r.status), { status: r.status });
  return r.json();
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('neuroops-theme'); } catch (e) { /* private mode, fine */ }
  const forced = new URLSearchParams(location.search).get('theme') || saved;
  const dark = forced ? forced === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  readColors();
}

async function boot() {
  initTheme();
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
  if (scenario !== S.scenario) S.seen.clear();
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

// four numbers for the top strip. with a shadow recording alongside, each one gets its "without healing" twin.
function faultStats(d, t) {
  const real = (d.truth || []).filter((e) => !e.benign && e.start_i <= t);
  const breached = real.filter((e) => e.breach_i != null && e.breach_i <= t && (e.stopped_i == null || e.stopped_i > e.breach_i)).length;
  let steps = 0;
  for (const e of real) steps += Math.max(0, Math.min(t, e.stopped_i != null ? e.stopped_i : e.end_i) - e.start_i);
  return { faults: real.length, breached, hours: steps * 5 / 60 };
}

function kpiSet(now) {
  const d = S.cur, t = S.t;
  const verified = d.audit.filter((e) => e.i <= t && e.type === 'verified').length;
  if (!d.truth) {
    const open = now.list.filter((s) => !s.closed).length;
    return [
      { id: 'open', cls: 'mid', n: open, l: 'Open now', s: 'incidents the engine is working on' },
      { id: 'act', cls: 'good', n: now.actions, l: 'Actions taken', s: `<b>${verified}</b> checked and worked` },
      { id: 'wait', cls: 'wait', n: now.waiting, l: 'Waiting for approval', s: 'a person has to answer' },
      { id: 'all', cls: 'mid', n: d.incidents.filter((i) => i.opened_i <= t).length, l: 'Incidents so far', s: 'since this run started' }];
  }
  const me = faultStats(d, t), twin = S.ghost ? faultStats(S.ghost, t) : null;
  return [
    { id: 'faults', cls: 'mid', n: me.faults, l: 'Faults so far', s: 'injected into the simulation' },
    { id: 'breach', cls: me.breached ? 'bad' : 'good', n: me.breached, l: 'Reached the danger line', s: twin ? `without healing: <b>${twin.breached}</b>` : 'slow ramps that hit 90%' },
    { id: 'hours', cls: 'wait', n: me.hours, dec: 1, unit: 'h', l: 'Time spent in a fault', s: twin ? `without healing: <b>${twin.hours.toFixed(1)} h</b>` : 'added up over all faults' },
    { id: 'act', cls: 'good', n: now.actions, l: 'Actions taken', s: `<b>${verified}</b> checked and worked` + (now.waiting ? `, <b>${now.waiting}</b> waiting` : '') }];
}

function drawKpis(now) {
  const host = $('kpis'), set = kpiSet(now);
  if (host.children.length !== set.length || host.dataset.key !== set.map((k) => k.id).join()) {
    host.dataset.key = set.map((k) => k.id).join();
    host.innerHTML = set.map((k) => `<div class="kpi" data-k="${k.id}"><div class="n"></div><div class="l">${k.l}</div><div class="s"></div></div>`).join('');
  }
  let moving = false;
  set.forEach((k, i) => {
    const el = host.children[i], dec = k.dec || 0;
    // the number eases toward its value, so a jump reads as a change and not a flicker
    const cur = S.disp[k.id] == null ? k.n : S.disp[k.id], next = reduced ? k.n : cur + (k.n - cur) * 0.2;
    S.disp[k.id] = Math.abs(next - k.n) < 0.05 ? k.n : next;
    if (S.disp[k.id] !== k.n) moving = true;
    el.className = 'kpi ' + k.cls;
    el.querySelector('.n').innerHTML = S.disp[k.id].toFixed(dec) + (k.unit ? `<small>${k.unit}</small>` : '');
    el.querySelector('.s').innerHTML = k.s;
  });
  return moving;
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
        <span class="state">Normal</span>
        <span class="vals"><span class="c"></span><span class="m"></span><span class="d"></span></span>
      </button><canvas></canvas>`;
    host.appendChild(row);
    const canvas = row.querySelector('canvas'), btn = row.querySelector('.who');
    btn.addEventListener('click', () => { S.focus = S.focus === inst.id ? null : inst.id; chrome(); });
    canvas.addEventListener('mousemove', (ev) => hover(ev, S.lanes[inst.id]));
    canvas.addEventListener('mouseleave', () => { $('tip').hidden = true; S.hover = null; setHl(null); dirty(); });
    S.lanes[inst.id] = { row, canvas, btn, inst, hits: [], name: `${inst.role} ${n}`, stateText: '' };
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
  lane.hits = []; lane.geom = { W, win, t0 };
  g.clearRect(0, 0, W, H);

  // the time before the engine starts watching
  const wf = S.cur.meta.watching_from || 0;
  if (wf > t0) { g.fillStyle = C.dim; g.fillRect(0, 0, Math.min(W, X(wf)), H); }

  // graph paper: a line every 3 hours, heavier at midnight
  for (let i = Math.ceil(t0 / 36) * 36; i <= t; i += 36) {
    const dt = when(i), mid = dt.getUTCHours() === 0;
    g.strokeStyle = mid ? C.grid : C.line; g.lineWidth = 1;
    g.beginPath(); g.moveTo(X(i) + 0.5, 0); g.lineTo(X(i) + 0.5, H); g.stroke();
    if (big) { label(g, 10); g.fillStyle = C.muted; g.fillText(mid ? dayName(dt) : hhmm(dt), X(i) + 3, H - 5); }
  }
  g.strokeStyle = C.line; g.beginPath(); g.moveTo(0, Y(50)); g.lineTo(W, Y(50)); g.stroke();
  g.strokeStyle = C.danger; g.setLineDash([2, 3]); g.beginPath(); g.moveTo(0, Y(90)); g.lineTo(W - 46, Y(90)); g.stroke(); g.setLineDash([]);

  // an incident under the pointer (or its card) lights up the whole column
  const mine = now.list.filter((s) => s.inc.target === inst.id || s.inc.target === inst.role);
  for (const s of mine) {
    if (S.hl !== s.inc.id) continue;
    const a = Math.max(0, X(s.inc.opened_i)), b = X(s.inc.closed_i != null && s.inc.closed_i <= t ? s.inc.closed_i : t);
    g.fillStyle = FAMILY[s.inc.cause] || C.ink; g.globalAlpha = 0.1; g.fillRect(a, 0, Math.max(3, b - a), H); g.globalAlpha = 1;
  }

  // injected faults, hatched along the bottom
  if (S.truth && S.cur.truth) {
    for (const e of S.cur.truth) {
      if (e.instance_id !== inst.id || e.end_i < t0 || e.start_i > t) continue;
      const a = Math.max(0, X(e.start_i)), b = X(Math.min(e.end_i, t));
      g.save(); g.beginPath(); g.rect(a, bottom - 9, b - a, 9); g.clip();
      g.strokeStyle = e.benign ? C.muted : C.ink; g.lineWidth = 1;
      for (let x = a - 10; x < b; x += 5) { g.beginPath(); g.moveTo(x, bottom); g.lineTo(x + 9, bottom - 9); g.stroke(); }
      g.restore();
      lane.hits.push({ x0: a, x1: b, y0: bottom - 9, y1: bottom, html: `<b>Injected fault: ${TRUTH[e.kind] || e.kind}</b><span class="g">${e.benign ? 'harmless, the engine should leave it alone' : 'a real fault the engine has to find'}</span>` });
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
  let hit = null;
  for (const s of mine) {
    const inc = s.inc;
    const end = inc.closed_i != null && inc.closed_i <= t ? inc.closed_i : t;
    if (end < t0) continue;
    const a = Math.max(0, X(inc.opened_i)), b = X(end), lit = S.hl === inc.id;
    g.fillStyle = FAMILY[inc.cause] || C.ink;
    g.globalAlpha = (S.hl != null && !lit) ? 0.25 : s.closed ? 0.4 : 1;
    g.fillRect(a, 0, Math.max(2, b - a), lit ? 9 : 5);
    g.globalAlpha = 1;
    if (!s.closed && inc.cause !== 'memory_step') hit = s;
    if (big && b - a > 70) { label(g, 10, 700); g.fillStyle = FAMILY[inc.cause]; g.fillText(CAUSE[inc.cause], a + 2, 20); }
    const [, said] = verdictLine(s);
    lane.hits.push({ x0: a, x1: Math.max(a + 6, b), y0: 0, y1: 20, inc: inc.id,
      html: `<b>${CAUSE[inc.cause]}</b><span>${esc((inc.evidence || [])[0] || '')}</span><span class="g">${esc(said)}</span>` +
        (inc.logs && inc.logs[0] ? `<span class="k">${esc(inc.logs[0].template)}</span>` : '') });
  }
  lane.row.classList.toggle('hit', !!hit);
  const stateText = hit ? CAUSE[hit.inc.cause] : 'Normal';
  if (stateText !== lane.stateText) {
    lane.stateText = stateText;
    const el = lane.row.querySelector('.state');
    el.textContent = stateText; el.style.color = hit ? (FAMILY[hit.inc.cause] || C.danger) : '';
  }

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
    lane.hits.push({ x0: x - 8, x1: x + 8, y0: 4, y1: 22, inc: e.incident, front: true,
      html: `<b>Ran: ${esc(ACTION[e.action] || e.action)}</b><span>${hhmm(when(e.i))}${e.approved_by ? ', approved by ' + esc(e.approved_by === 'evaluation-auto-approver' ? 'the stand-in operator' : e.approved_by) : ', no approval needed'}</span>` +
        `<span class="g">${!ok ? 'checking whether it worked' : ok.type === 'verified' ? 'check passed, the problem went away' : 'check failed, moved to the next step'}</span>` });
  }

  // the pointer: one line through every lane, and the value on each trace
  const hv = S.hover;
  if (hv != null && hv >= t0 && hv <= t) {
    const x = X(hv);
    g.strokeStyle = C.ink; g.globalAlpha = 0.45; g.lineWidth = 1; g.beginPath(); g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, H); g.stroke(); g.globalAlpha = 1;
    for (const [metric, color] of TRACES.slice(1)) {
      const v = shown(inst.id, metric, Math.round(hv));
      g.fillStyle = C.sheet; g.beginPath(); g.arc(x + 0.5, Y(v), 4, 0, 7); g.fill();
      g.strokeStyle = color; g.lineWidth = 2; g.stroke();
    }
  }

  // current values at the pen tip (or under the pointer)
  const tip = Math.floor(hv != null && hv <= t ? hv : t);
  if (hv == null || hv > t) {
    label(g, 11, 700);
    let lastY = -99;
    for (const [metric, color] of TRACES.slice(1).reverse()) {
      const v = shown(inst.id, metric, tip);
      g.fillStyle = color; g.beginPath(); g.arc(X(tip), Y(v), 2.6, 0, 7); g.fill();
      if (big) { let y = Y(v) + 4; if (Math.abs(y - lastY) < 11) y = lastY + 11; lastY = y; g.fillText(v.toFixed(0) + '%', X(tip) + 7, y); }
    }
  }
  const vals = lane.row.querySelectorAll('.vals span');
  vals[0].textContent = 'cpu ' + shown(inst.id, 'CPUUtilization', tip).toFixed(0);
  vals[1].textContent = 'mem ' + shown(inst.id, 'mem_used_percent', tip).toFixed(0);
  vals[2].textContent = 'disk ' + shown(inst.id, 'disk_used_percent', tip).toFixed(0);
}

function hover(ev, lane) {
  const { inst, canvas } = lane, r = canvas.getBoundingClientRect(), g = lane.geom;
  const x = ev.clientX - r.left, y = ev.clientY - r.top;
  const i = Math.round(g.t0 + x / (g.W - 46) * g.win);
  const tip = $('tip');
  if (i < 0 || i > S.t) { tip.hidden = true; S.hover = null; setHl(null); dirty(); return; }
  S.hover = i;
  const hit = lane.hits.filter((h) => x >= h.x0 && x <= h.x1 && y >= h.y0 && y <= h.y1).sort((a, b) => (b.front ? 1 : 0) - (a.front ? 1 : 0))[0];
  setHl(hit && hit.inc != null ? hit.inc : null);
  canvas.style.cursor = hit ? 'pointer' : 'crosshair';
  const dt = when(i), m = S.cur.metrics[inst.id];
  let html;
  if (hit) html = hit.html;
  else {
    html = `<b>${dayName(dt)}, ${hhmm(dt)}</b>
    <span>cpu ${m.CPUUtilization[i].toFixed(1)}%</span><span>memory ${m.mem_used_percent[i].toFixed(1)}%</span>
    <span>disk ${m.disk_used_percent[i].toFixed(1)}%</span><span>traffic in ${m.NetworkIn[i].toFixed(2)} MB per 5 min</span>`;
    if (m.CPUCreditBalance) html += `<span>burst credits ${m.CPUCreditBalance[i].toFixed(0)}</span>`;
    if (S.ghost) {
      const gm = S.ghost.metrics[inst.id];
      for (const [k, name] of [['CPUUtilization', 'cpu'], ['mem_used_percent', 'memory'], ['disk_used_percent', 'disk']]) {
        if (Math.abs(gm[k][i] - m[k][i]) > 1.5) html += `<span class="g">without healing: ${name} ${gm[k][i].toFixed(1)}%</span>`;
      }
    }
  }
  tip.innerHTML = html; tip.hidden = false;
  tip.style.left = Math.min(window.innerWidth - 320, ev.clientX + 14) + 'px';
  tip.style.top = (ev.clientY + 14) + 'px';
  dirty();
}

// the incident under the pointer and its card light up together
function setHl(id) {
  if (S.hl === id) return;
  S.hl = id;
  document.querySelectorAll('#open .card').forEach((c) => c.classList.toggle('hl', id != null && +c.dataset.inc === id));
  dirty();
}

// ---------- scrubber ----------
function drawScrub(now) {
  const canvas = $('scrub'), [g, W, H] = fit(canvas), d = S.cur, N = d.meta.steps;
  const X = (i) => i / (N - 1) * W;
  g.clearRect(0, 0, W, H);
  const wf = d.meta.watching_from || 0;
  if (wf) {
    g.fillStyle = C.dim; g.fillRect(0, 0, X(wf), H);
    label(g, 10); g.fillStyle = C.muted; g.fillText('learning', 5, H - 5);
  }
  for (let i = 0; i < N; i += 12) {
    const dt = when(i);
    if (dt.getUTCHours() !== 0 || dt.getUTCMinutes() !== 0) continue;
    g.strokeStyle = C.grid; g.beginPath(); g.moveTo(X(i) + 0.5, 0); g.lineTo(X(i) + 0.5, H); g.stroke();
    label(g, 10); g.fillStyle = C.muted; g.fillText(dayName(dt), X(i) + 4, 11);
  }
  // one row per instance, one bar per incident
  const rows = d.instances, rh = (H - 22) / rows.length;
  rows.forEach((inst, r) => {
    const y = 16 + r * rh;
    if (S.truth && d.truth) {
      g.fillStyle = C.grid;
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
  const key = open.map((s) => s.inc.id + s.verdict + (s.approval ? 'a' : '') + (s.inc.logs ? s.inc.logs.length : '')).join('|') + '#' + (events[0] ? events[0].i + events[0].type + events.length : '') + S.mode + S.scenario;
  if (key === railKey) return;
  railKey = key;

  const host = $('open');
  host.innerHTML = open.length ? '' : '<p class="empty">Nothing open. The fleet looks normal.</p>';
  for (const s of open) {
    const inc = s.inc, [cls, text] = verdictLine(s);
    const where = inc.scope === 'tier' ? `whole ${inc.target} tier` : (S.lanes[inc.target] ? S.lanes[inc.target].name : inc.target);
    const mins = (tNow - inc.opened_i) * 5;
    const card = document.createElement('div');
    card.className = 'card'; card.style.setProperty('--fam', FAMILY[inc.cause] || C.ink);
    card.tabIndex = 0; card.setAttribute('role', 'button'); card.dataset.inc = inc.id;
    // new cards slide in while the recording plays, not when you scrub or jump
    if (!S.seen.has(inc.id) && S.playing && !reduced) card.classList.add('enter');
    S.seen.add(inc.id);
    if (S.hl === inc.id) card.classList.add('hl');
    const said = [...S.cur.audit].reverse().find((e) => e.type === 'notified' && e.incident === inc.id && e.i <= tNow && e.text);
    card.innerHTML = `<span class="head"><span class="cause">${CAUSE[inc.cause] || inc.cause}</span>
        <span class="where">${where}, ${mins < 60 ? mins + ' min' : (mins / 60).toFixed(1) + ' h'}</span></span>
      <span class="why">${esc((inc.evidence || [])[0] || '')}</span>
      <span class="verdict ${cls}">${text}</span>
      ${['needs_approval', 'blocked', 'person', 'notify', 'shadow'].includes(s.verdict) && s.reasons.length ? `<ul class="reasons">${s.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}
      ${inc.change ? `<span class="change">A release went out ${inc.change.minutes_before} min before this started: ${esc(inc.change.text)}</span>` : ''}
      ${inc.logs && inc.logs.length ? `<ul class="lines" aria-label="Log lines that are new or louder than usual">${inc.logs.slice(0, 3).map((c) => `<li class="${c.level}${c.new ? ' fresh' : ''}"><i></i><code>${esc(c.template)}</code><em>${c.new ? 'new' : c.in_window + ' now, ' + c.in_baseline + ' in a day'}</em></li>`).join('')}</ul>` : ''}`;
    if (said) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'peek';
      b.textContent = S.live ? 'See the alert' : 'See the alert it would send';
      b.onclick = (ev) => { ev.stopPropagation(); showAlert(said); };
      card.appendChild(b);
    }
    card.addEventListener('mouseenter', () => setHl(inc.id));
    card.addEventListener('mouseleave', () => setHl(null));
    card.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' && ev.target === card) card.click(); });
    if (S.live && s.approval) {
      const row = document.createElement('div'); row.className = 'answer';
      row.innerHTML = '<button type="button">Approve</button><button type="button" class="quiet">Reject</button>';
      row.children[0].onclick = () => answer(s.approval.id, 'approve');
      row.children[1].onclick = () => answer(s.approval.id, 'reject');
      card.appendChild(row);
    }
    card.addEventListener('click', (ev) => { if (ev.target.closest('.answer, .peek')) return; const iid = inc.scope === 'tier' ? S.cur.instances.find((x) => x.role === inc.target).id : inc.target; S.focus = iid; chrome(); S.lanes[iid].row.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' }); });
    host.appendChild(card);
  }

  S.logEvents = events;
  $('log').innerHTML = events.map((e, n) => {
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
      case 'incident_resolved': text = `Closed: ${CAUSE[e.cause] || e.cause} on ${who}, ${e.how}` + (S.cur.postmortems && S.cur.postmortems[e.incident] ? ' (postmortem)' : ''); break;
      case 'reopened': text = `Back again: ${CAUSE[e.cause] || e.cause} on ${who}`; break;
      case 'notified': text = e.ok ? (e.channel === 'demo (not sent)' ? `Alert drafted: ${e.subject}` : `Alert sent by ${e.channel}: ${e.subject}`) : `Alert not sent: ${e.error || 'held back'}`; break;
      case 'mode_changed': text = `Mode changed from ${e.old} to ${e.new} by ${e.by}`; break;
      default: text = e.type;
    }
    const dt = when(e.i);
    const pm = e.type === 'incident_resolved' && S.cur.postmortems && S.cur.postmortems[e.incident];
    return `<li${e.text || pm ? ` class="clickable" data-n="${n}" title="${pm ? 'Open the postmortem draft' : 'Open the message'}"` : ''}><time>${dt.getUTCDate()} ${dt.toLocaleDateString('en-GB', { month: 'short', timeZone: 'UTC' })} ${hhmm(dt)}</time><span class="${cls}">${esc(text)}</span></li>`;
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
  const rel = d.reliability, box = $('summary');
  box.hidden = !rel;
  if (rel) {
    const row = (k, v) => `<dt>${k}</dt><dd>${v}</dd>`;
    $('facts').innerHTML = row('Incidents', rel.incidents) + row('Fixed by the engine', rel.fixed_by_engine) +
      row('Handed to a person', rel.handed_to_person) + row('Average time open', rel.mean_minutes_open == null ? 'n/a' : rel.mean_minutes_open + ' min') +
      row('Average time to first action', rel.mean_minutes_to_first_action == null ? 'none taken' : rel.mean_minutes_to_first_action + ' min') +
      row('Alerts drafted', rel.alerts_sent);
  }
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
    if (drawKpis(now)) needsDraw = true;
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

// ---------- theme, jumping, alert preview ----------
$('theme').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme !== 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  try { localStorage.setItem('neuroops-theme', dark ? 'dark' : 'light'); } catch (e) { /* not worth failing for */ }
  readColors(); railKey = ''; dirty();
});

function jump(dir) {
  if (!S.cur) return;
  const opens = S.cur.incidents.filter((i) => i.cause !== 'memory_step').map((i) => i.opened_i).sort((a, b) => a - b);
  const here = S.t - 6;   // land a little after detection, so the card and its evidence are already up
  const to = dir > 0 ? opens.find((o) => o > here + 1) : [...opens].reverse().find((o) => o < here - 1);
  if (to == null) return;
  S.t = Math.min(S.cur.meta.steps - 1, to + 6); S.playing = false; S.seen.clear();
  chrome();
}
$('nextInc').addEventListener('click', () => jump(1));
$('prevInc').addEventListener('click', () => jump(-1));

function showAlert(e) {
  $('alertSubject').textContent = e.subject;
  $('alertText').textContent = e.text;
  $('alertNote').textContent = e.channel === 'demo (not sent)'
    ? 'This is what the engine would send to the on-call channel. The hosted demo sends nothing.'
    : `Sent through ${e.channel}.`;
  $('alertDlg').showModal();
}
function showPostmortem(id) {
  $('pmText').textContent = S.cur.postmortems[id];
  $('pmDlg').showModal();
}
$('pmClose').addEventListener('click', () => $('pmDlg').close());
$('pmCopy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('pmText').textContent); $('pmCopy').textContent = 'Copied'; setTimeout(() => { $('pmCopy').textContent = 'Copy'; }, 1500); }
  catch (e) { $('pmCopy').textContent = 'Select the text and copy'; }
});
$('alertClose').addEventListener('click', () => $('alertDlg').close());
$('log').addEventListener('click', (ev) => {
  const li = ev.target.closest('li[data-n]');
  const e = li && S.logEvents[+li.dataset.n];
  if (!e) return;
  if (e.type === 'incident_resolved') showPostmortem(e.incident); else showAlert(e);
});
// a pointer over the overview shows the time under it
scrub.addEventListener('mousemove', (ev) => {
  if (dragging || !S.cur) return;
  const r = scrub.getBoundingClientRect(), i = Math.round((ev.clientX - r.left) / r.width * (S.cur.meta.steps - 1)), dt = when(i);
  const inc = S.cur.incidents.filter((x) => x.opened_i <= i && (x.closed_i == null || x.closed_i >= i)).map((x) => CAUSE[x.cause]);
  const tip = $('tip');
  tip.innerHTML = `<b>${dayName(dt)}, ${hhmm(dt)}</b>` + (inc.length ? `<span>${esc([...new Set(inc)].join(', '))}</span>` : '<span class="g">nothing open</span>') + '<span class="g">click to jump here</span>';
  tip.hidden = false; tip.style.left = Math.min(window.innerWidth - 320, ev.clientX + 14) + 'px'; tip.style.top = (r.bottom + 8) + 'px';
});
scrub.addEventListener('mouseleave', () => { $('tip').hidden = true; });
document.addEventListener('keydown', (e) => {
  if (e.target.closest('input, select, dialog, button, [role=button]')) return;
  if (e.key === 'j') jump(1);
  if (e.key === 'k') jump(-1);
});

// ?hover=<step> and ?hl=<incident> exist so a screenshot can show the hover states
{ const q = new URLSearchParams(location.search);
  if (q.get('hover')) S.hover = +q.get('hover');
  if (q.get('hl')) S.hl = +q.get('hl'); }
boot();
