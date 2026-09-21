// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let activeServer = null;
let ws = null;
let currentProps = {};
let playerPollTimer = null;
let isStartingLocal = false;
let pollingEnabled = true;
let currentPlayerTarget = "";

// Single source of truth for all panels (drives sw() deactivation)
const PANELS = [
  'instances', 'cluster', 'overview', 'console', 'players',
  'properties', 'world', 'files', 'upload', 'mods', 'debug', 'automation'
];

// Panels that work with no server selected. Everything else needs a target,
// and the sidebar hides those entries until one is picked. sw() still checks,
// so a stale deep link or a server deleted out from under the user cannot land
// on an empty panel.
const GLOBAL_PANELS = new Set(['instances', 'cluster']);
const TARGET_PANELS = new Set(PANELS.filter(p => !GLOBAL_PANELS.has(p)));

const WORLD_KEYS = [
  'level-name', 'level-seed', 'level-type', 'allow-nether', 'max-build-height',
  'spawn-protection', 'spawn-monsters', 'spawn-animals', 'spawn-npcs',
  'generate-structures', 'generator-settings', 'max-world-size'
];
const SKIP_PROPS = new Set(WORLD_KEYS);

// server.properties is a flat wall of ~60 keys; grouping it is what keeps the
// Properties panel readable. Unlisted keys fall through to an "Other" group.
const PROP_GROUPS = [
  {
    name: 'Network & Access',
    keys: [
      'server-ip', 'server-port', 'server-name', 'motd', 'max-players', 'online-mode',
      'white-list', 'enforce-whitelist', 'prevent-proxy-connections', 'network-compression-threshold',
      'rate-limit', 'enable-status', 'hide-online-players', 'enforce-secure-profile',
      'accepts-transfers', 'log-ips', 'bug-report-link'
    ]
  },
  {
    name: 'Remote Admin',
    keys: [
      'enable-rcon', 'rcon.port', 'rcon.password', 'broadcast-rcon-to-ops',
      'enable-query', 'query.port', 'broadcast-console-to-ops', 'enable-jmx-monitoring', 'debug'
    ]
  },
  {
    name: 'Gameplay',
    keys: [
      'gamemode', 'force-gamemode', 'difficulty', 'hardcore', 'pvp', 'allow-flight',
      'player-idle-timeout', 'enable-command-block', 'op-permission-level',
      'function-permission-level', 'text-filtering-config', 'text-filtering-version'
    ]
  },
  {
    name: 'Resource Packs',
    keys: [
      'resource-pack', 'resource-pack-sha1', 'resource-pack-prompt', 'resource-pack-id',
      'require-resource-pack', 'initial-enabled-packs', 'initial-disabled-packs'
    ]
  },
  {
    name: 'Performance',
    keys: [
      'view-distance', 'simulation-distance', 'entity-broadcast-range-percentage',
      'max-tick-time', 'max-chained-neighbor-updates', 'sync-chunk-writes',
      'use-native-transport', 'pause-when-empty-seconds', 'region-file-compression'
    ]
  }
];

// Panels that auto-refresh on inactive servers – poll less aggressively
const PASSIVE_PANELS = new Set(['properties', 'world', 'files', 'upload', 'mods', 'debug', 'automation']);

// ---------------------------------------------------------------------------
// Theme (day / night)
//
// With nothing stored the CSS follows prefers-color-scheme on its own; a
// stored choice pins it via data-theme on <html>. index.html applies that
// attribute before first paint, so this only keeps the button in sync and
// handles the toggle.
// ---------------------------------------------------------------------------
const THEME_KEY = 'mc-theme';

function prefersDark() {
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}

function effectiveTheme() {
  const pinned = document.documentElement.getAttribute('data-theme');
  if (pinned === 'dark' || pinned === 'light') return pinned;
  return prefersDark() ? 'dark' : 'light';
}

// The button advertises the theme it will switch TO, not the current one.
function syncThemeButton() {
  const btn = document.getElementById('btn-theme');
  if (!btn) return;
  const isDark = effectiveTheme() === 'dark';
  btn.innerHTML = isDark
    ? '<i class="ti ti-sun"></i> DAY'
    : '<i class="ti ti-moon"></i> NIGHT';
  btn.title = isDark ? 'Switch to the day theme' : 'Switch to the night theme';
  btn.setAttribute('aria-pressed', String(isDark));
}

function toggleTheme() {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (e) {
    toast('Theme applied, but it could not be saved for next time.', 'info');
  }
  syncThemeButton();
}

function initTheme() {
  syncThemeButton();
  // Keep following the OS for as long as the user has not pinned a theme.
  if (window.matchMedia) {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => {
      if (!document.documentElement.hasAttribute('data-theme')) syncThemeButton();
    };
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
}

// ---------------------------------------------------------------------------
// Toast notification system
// ---------------------------------------------------------------------------
function toast(msg, type = 'info') {
  const container = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  // Trigger animation
  requestAnimationFrame(() => el.classList.add('toast-show'));
  setTimeout(() => {
    el.classList.remove('toast-show');
    el.addEventListener('transitionend', () => el.remove());
  }, 3200);
}

// ---------------------------------------------------------------------------
// Escape helpers
// ---------------------------------------------------------------------------
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/--/g, '&#45;&#45;');
}

// ---------------------------------------------------------------------------
// Initialisation
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  fetchNodes();
  connectWS();
  populateVersionsDynamically();
  renderRconCommands();
  renderModalTabs();
  initTheme();
  applyConsoleNoiseMode();
  applyTargetGate();
});

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------
function connectWS() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);

    // Resource bars
    if (data.type === 'sys_stats') {
      // Host
      setBar('bar-cpu', 'lbl-cpu', data.cpu);
      setBar('bar-ram', 'lbl-ram', data.ram);
      setBar('bar-disk', 'lbl-disk', data.disk);

      // Container & TPS
      document.getElementById('c-stat-cpu').textContent = data.c_cpu || '--';
      document.getElementById('c-stat-ram').textContent = data.c_ram || '--';

      const tpsEl = document.getElementById('stat-tps');
      if (tpsEl) {
        tpsEl.textContent = data.tps || '--';
        // Highlight red if TPS drops below 20 [cite: 7]
        tpsEl.style.color = (data.tps && parseFloat(data.tps) < 19.5) ? '#ff5555' : '#55ff55';
      }
    }

    // Player list
    if (data.type === 'player_list') {
      const players = data.players || [];
      const maxPlayers = currentProps['max-players'] || '20';
      document.getElementById('stat-players').textContent = `${players.length}/${maxPlayers}`;
      document.getElementById('p-count-lbl').textContent = `Online: ${players.length} / ${maxPlayers}`;

      renderPlayerRows(players);
    }

    // Player NBT
    if (data.type === 'player_data') {
      renderPlayerData(data.data);
    }

    // Docker log lines
    if (data.type === 'docker_log') {
      logContainerLine(data.data);
    }

    // Console RCON response (fallback if not streamed via log)
    if (data.type === 'console_response' && data.stdout) {
      data.stdout.split('\n').forEach(line => logContainerLine(line));
    }
  };

  ws.onclose = () => {
    console.warn('WebSocket closed – reconnecting in 5s');
    setTimeout(connectWS, 5000);
  };

  ws.onerror = (err) => console.error('WebSocket error:', err);
}
// ---------------------------------------------------------------------------
// RCON Command Configuration (Data-Driven)
// Add new buttons here!
// ---------------------------------------------------------------------------
const RCON_COMMANDS = [
  { label: "☀ Day", cmd: "time set day" },
  { label: "🌙 Night", cmd: "time set night" },
  { label: "☂ Clear Wx", cmd: "weather clear" },
  { label: "💾 Save All", cmd: "save-all" },
  { label: "👥 List", cmd: "list" },
  { label: "🛡 Whitelist On", cmd: "whitelist on" },
  { label: "⚠ Stop", cmd: "stop" }
];

function renderRconCommands() {
  const container = document.getElementById('rcon-quick-btns');
  if (!container) return;

  container.innerHTML = RCON_COMMANDS.map(btn =>
    `<button class="mc-btn sm" onclick="sendQuickCommand('${escapeHtml(btn.cmd)}')">${escapeHtml(btn.label)}</button>`
  ).join('');
}

function sendQuickCommand(cmd) {
  if (!activeServer) {
    toast("Select a server first.", "error");
    return;
  }
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method: 'docker:exec/command', target: activeServer, command: cmd }));
    logTerm(`> ${cmd}`);
  }
}

// ---------------------------------------------------------------------------
// Resource bar helper — green / yellow / red thresholds
// ---------------------------------------------------------------------------
function setBar(barId, lblId, pct) {
  const bar = document.getElementById(barId);
  const lbl = document.getElementById(lblId);
  if (!bar || !lbl) return;
  bar.style.width = `${pct}%`;
  lbl.textContent = `${pct.toFixed(1)}%`;
  bar.style.background = pct >= 90 ? '#ff5555' : pct >= 70 ? '#ffff55' : '#55aa22';
}

// ---------------------------------------------------------------------------
// Console terminal
// ---------------------------------------------------------------------------

// Housekeeping chatter the server emits on its own — the stats/player pollers
// open an RCON connection every few seconds and each one logs a thread start
// and shutdown, which buries the lines that actually came from the game.
const CONSOLE_NOISE = [
  /\[RCON (?:Listener|Client)[^\]]*\]:\s*Thread RCON Client .*(?:started|shutting down)/i,
  /\[Query Listener[^\]]*\]:\s*Thread Query Listener started/i,
  /Thread RCON Client .*(?:started|shutting down)\s*$/i
];

// Muted lines stay in the DOM so flipping the toggle reveals the backlog.
const CONSOLE_MAX_LINES = 2000;
let hideConsoleNoise = localStorage.getItem('mc-hide-console-noise') !== 'false';

function isConsoleNoise(line) {
  return CONSOLE_NOISE.some(re => re.test(line));
}

function consoleLevel(msg, isErr) {
  if (isErr || /\b(ERROR|FATAL|SEVERE)\b/.test(msg)) return 'error';
  if (/\bWARN(?:ING)?\b/.test(msg)) return 'warn';
  return 'info';
}

const LEVEL_COLOR = { error: '#ff5555', warn: '#ffff55', info: '#55ff55', noise: '#6d8d6d' };

function logTerm(msg, isErr = false, isNoise = false) {
  const el = document.getElementById('console-log');
  if (!el) return;
  const level = consoleLevel(msg, isErr);
  const d = document.createElement('div');
  d.className = isNoise ? 'term-line term-noise' : 'term-line';
  d.dataset.level = level;
  d.style.color = level === 'info' && isNoise ? LEVEL_COLOR.noise : LEVEL_COLOR[level];
  // Server lines already carry their own clock — don't stamp them twice.
  d.textContent = /^\[\d{1,2}:\d{2}:\d{2}\]/.test(msg)
    ? msg
    : `[${new Date().toLocaleTimeString()}] ${msg}`;
  applyFilterToLine(d);
  el.appendChild(d);

  while (el.childElementCount > CONSOLE_MAX_LINES) el.removeChild(el.firstElementChild);
  el.scrollTop = el.scrollHeight;
  updateNoiseCount();
  updateFilterCount();
}

// ---------------------------------------------------------------------------
// Console filtering
//
// Search and severity hide lines with a class rather than dropping them, so
// widening the filter brings the backlog straight back — same trick as the
// noise toggle, and the two compose.
// ---------------------------------------------------------------------------
const LEVEL_RANK = { info: 0, warn: 1, error: 2 };

function consoleFilterState() {
  const search = (document.getElementById('console-search')?.value || '').trim().toLowerCase();
  const level = document.getElementById('console-level')?.value || 'all';
  return { search, minRank: level === 'error' ? 2 : level === 'warn' ? 1 : 0 };
}

function applyFilterToLine(node, state) {
  const { search, minRank } = state || consoleFilterState();
  const levelOk = LEVEL_RANK[node.dataset.level || 'info'] >= minRank;
  const textOk = !search || node.textContent.toLowerCase().includes(search);
  node.classList.toggle('term-filtered', !(levelOk && textOk));
}

function applyConsoleFilter() {
  const el = document.getElementById('console-log');
  if (!el) return;
  const state = consoleFilterState();
  el.querySelectorAll('.term-line').forEach(node => applyFilterToLine(node, state));
  updateFilterCount();
}

function updateFilterCount() {
  const el = document.getElementById('console-log');
  const lbl = document.getElementById('console-filter-count');
  if (!el || !lbl) return;
  const { search, minRank } = consoleFilterState();
  if (!search && !minRank) { lbl.textContent = ''; return; }
  const total = el.querySelectorAll('.term-line').length;
  const hit = el.querySelectorAll('.term-line:not(.term-filtered)').length;
  lbl.textContent = `${hit} / ${total} shown`;
}

function downloadConsole() {
  const el = document.getElementById('console-log');
  if (!el || !el.childElementCount) { toast('Nothing in the console to save.', 'error'); return; }
  // What you see is what you get: muted and filtered-out lines stay out.
  const lines = [...el.querySelectorAll('.term-line')]
    .filter(n => n.offsetParent !== null)
    .map(n => n.textContent);
  if (!lines.length) { toast('Every line is filtered out.', 'error'); return; }
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${activeServer || 'console'}-${stamp}.log`;
  a.click();
  URL.revokeObjectURL(url);
  toast(`Saved ${lines.length} line(s).`, 'ok');
}

// Container log lines are the only ones eligible for muting — anything the
// manager itself prints always shows.
function logContainerLine(line) {
  logTerm(line, false, isConsoleNoise(line));
}

function applyConsoleNoiseMode() {
  const el = document.getElementById('console-log');
  const btn = document.getElementById('console-noise-btn');
  if (el) el.classList.toggle('hide-noise', hideConsoleNoise);
  if (btn) btn.textContent = hideConsoleNoise ? 'SHOW NOISE' : 'HIDE NOISE';
  updateNoiseCount();
}

function toggleConsoleNoise() {
  hideConsoleNoise = !hideConsoleNoise;
  localStorage.setItem('mc-hide-console-noise', hideConsoleNoise ? 'true' : 'false');
  applyConsoleNoiseMode();
}

function updateNoiseCount() {
  const el = document.getElementById('console-log');
  const lbl = document.getElementById('console-muted');
  if (!el || !lbl) return;
  const muted = el.querySelectorAll('.term-noise').length;
  lbl.textContent = hideConsoleNoise && muted ? `${muted} line${muted === 1 ? '' : 's'} muted` : '';
}

function clearConsole() {
  const el = document.getElementById('console-log');
  if (el) el.innerHTML = '';
  updateNoiseCount();
}

// ---------------------------------------------------------------------------
// Command history — up/down through what was sent, per browser, across reloads
// ---------------------------------------------------------------------------
const CMD_HISTORY_KEY = 'mc-cmd-history';
const CMD_HISTORY_MAX = 100;

let cmdHistory = loadCmdHistory();
let cmdCursor = cmdHistory.length;   // one past the end == the live input
let cmdDraft = '';                   // what was typed before arrowing away

function loadCmdHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(CMD_HISTORY_KEY) || '[]');
    return Array.isArray(raw) ? raw.filter(x => typeof x === 'string') : [];
  } catch (e) {
    return [];
  }
}

function pushCmdHistory(cmd) {
  // Repeating the last command should not grow the list.
  if (cmdHistory[cmdHistory.length - 1] !== cmd) cmdHistory.push(cmd);
  if (cmdHistory.length > CMD_HISTORY_MAX) cmdHistory = cmdHistory.slice(-CMD_HISTORY_MAX);
  cmdCursor = cmdHistory.length;
  cmdDraft = '';
  try {
    localStorage.setItem(CMD_HISTORY_KEY, JSON.stringify(cmdHistory));
  } catch (e) { /* private mode — history just won't survive the reload */ }
}

function consoleKeydown(ev) {
  const inp = ev.target;
  if (ev.key === 'Enter') { sendTerminalCommand(); return; }
  if (ev.key !== 'ArrowUp' && ev.key !== 'ArrowDown') return;
  if (!cmdHistory.length) return;
  ev.preventDefault();

  if (cmdCursor === cmdHistory.length) cmdDraft = inp.value;
  cmdCursor += ev.key === 'ArrowUp' ? -1 : 1;
  cmdCursor = Math.max(0, Math.min(cmdCursor, cmdHistory.length));
  inp.value = cmdCursor === cmdHistory.length ? cmdDraft : cmdHistory[cmdCursor];
  // Park the caret at the end, or typing lands in the middle of the recalled line.
  requestAnimationFrame(() => inp.setSelectionRange(inp.value.length, inp.value.length));
}

function sendTerminalCommand() {
  const inp = document.getElementById('console-input');
  const val = inp.value.trim();
  if (!val || !activeServer) return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method: 'docker:exec/command', target: activeServer, command: val }));
    logTerm(`> ${val}`);
    pushCmdHistory(val);
    inp.value = '';
  }
}

// ---------------------------------------------------------------------------
// Panel switcher — includes scroll-to-top on every switch
// ---------------------------------------------------------------------------
function sw(id, el) {
  if (TARGET_PANELS.has(id) && !activeServer) {
    toast('Select a server instance first.', 'error');
    id = 'instances';
    el = document.querySelector('.sb-item[data-panel=instances]');
  }

  PANELS.forEach(p => {
    const pEl = document.getElementById('panel-' + p);
    if (pEl) pEl.classList.remove('active');
  });
  document.querySelectorAll('.sb-item').forEach(s => s.classList.remove('active'));

  const target = document.getElementById('panel-' + id);
  if (target) target.classList.add('active');
  if (el) el.classList.add('active');

  // Scroll main content area back to top
  const main = document.getElementById('mc-main');
  if (main) main.scrollTop = 0;

  // Auto-fetch on panel open
  if (id === 'automation') { loadBackups(); loadJobs(); loadSettings(); }
  if (id === 'mods') { loadInstalledAddons(); refreshPackwizStatus(); seedModrinthFilters(); }
  if (id === 'players') { requestPlayerList(); loadAccessList(); }
  if (id === 'files') { fmReload(); }
  if (id === 'cluster') { loadClusterMetrics(); }
  if (id === 'overview') { loadServerMetrics(); }
  if (id === 'properties') { loadPropHistory(); }
  // The debug snapshot shells out to docker several times, so it is fetched
  // once per target and then only on request.
  if (id === 'debug' && !debugData) { loadDebug(); loadDebugLogs(); }
}

// ---------------------------------------------------------------------------
// Target gate — with nothing selected the only thing on offer is Instances
// ---------------------------------------------------------------------------
function applyTargetGate() {
  document.body.classList.toggle('no-target', !activeServer);
}

function clearTarget() {
  activeServer = null;
  isStartingLocal = false;
  if (playerPollTimer) { clearInterval(playerPollTimer); playerPollTimer = null; }
  document.getElementById('active-target').textContent = 'None';
  document.getElementById('stat-node').textContent = 'N/A';
  const pill = document.getElementById('status-pill');
  pill.textContent = 'NO SERVER SELECTED';
  pill.className = 'pill pill-off';
  applyTargetGate();
  sw('instances', document.querySelector('.sb-item[data-panel=instances]'));
}

// ---------------------------------------------------------------------------
// Version selector (Mojang manifest)
// ---------------------------------------------------------------------------
async function populateVersionsDynamically() {
  const sel = document.getElementById('new-version');
  try {
    const res = await fetch('https://piston-meta.mojang.com/mc/game/version_manifest_v2.json');
    if (!res.ok) throw new Error('Manifest fetch failed');
    const data = await res.json();
    const releases = data.versions.filter(v => v.type === 'release');
    const groups = new Map();
    releases.forEach(v => {
      const parts = v.id.split('.');
      const major = parts.length > 1 ? `${parts[0]}.${parts[1]}` : parts[0];
      if (!groups.has(major)) groups.set(major, []);
      groups.get(major).push(v.id);
    });
    let html = '<option value="LATEST">Latest Release</option>';
    groups.forEach((vers, majorKey) => {
      html += `<optgroup label="Version ${majorKey}.x">`;
      vers.forEach(ver => { html += `<option value="${ver}">${ver}</option>`; });
      html += `</optgroup>`;
    });
    sel.innerHTML = html;
  } catch {
    sel.innerHTML = `
      <option value="LATEST">Latest Release</option>
      <optgroup label="Offline Fallbacks">
        <option value="1.21.1">1.21.1</option>
        <option value="1.20.4">1.20.4</option>
        <option value="1.19.4">1.19.4</option>
      </optgroup>`;
  }
}

// ---------------------------------------------------------------------------
// Server status UI
// ---------------------------------------------------------------------------
function updateServerStatusUI(statusStr) {
  const pill = document.getElementById('status-pill');
  const netStat = document.getElementById('stat-network');
  const btnStart = document.getElementById('btn-start');
  const btnStop = document.getElementById('btn-stop');
  const btnRestart = document.getElementById('btn-restart');
  const lower = statusStr.toLowerCase();
  const isUp = lower.includes('up');
  const isStarting = lower.includes('starting');

  if (isStartingLocal || (isUp && isStarting)) {
    pill.textContent = '● STARTING...'; pill.className = 'pill pill-start';
    netStat.textContent = 'STARTING'; netStat.style.color = '#ffff55';
    btnStart.disabled = true; btnStop.disabled = false; btnRestart.disabled = true;
  } else if (isUp) {
    isStartingLocal = false;
    pill.textContent = '● RUNNING'; pill.className = 'pill pill-on';
    netStat.textContent = 'ONLINE'; netStat.style.color = '#55ff55';
    btnStart.disabled = true; btnStop.disabled = false; btnRestart.disabled = false;
  } else {
    isStartingLocal = false;
    pill.textContent = '● OFFLINE'; pill.className = 'pill pill-off';
    netStat.textContent = 'OFFLINE'; netStat.style.color = '#ff5555';
    btnStart.disabled = false; btnStop.disabled = true; btnRestart.disabled = true;
  }
}

function setServerTarget(name, status) {
  activeServer = name;
  isStartingLocal = false;
  document.getElementById('active-target').textContent = name;
  document.getElementById('stat-node').textContent = name;
  updateServerStatusUI(status);
  applyTargetGate();
  logTerm(`Switched target to: ${name}`);
  loadPropsFromServer();
  loadInstalledAddons();
  fmResetForServer();
  debugResetForServer();
  accessResetForServer();
  propHistoryResetForServer();

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method: 'docker:logs/subscribe', target: name }));
    // NEW: Tell backend to start polling container stats
    ws.send(JSON.stringify({ method: 'set_active_target', target: name }));
  }

  // Reset console and subscribe to new container logs
  clearConsole();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method: 'docker:logs/subscribe', target: name }));
  }

  if (playerPollTimer) clearInterval(playerPollTimer);
  if (status.toLowerCase().includes('up')) {
    playerPollTimer = setInterval(requestPlayerList, 15000);
    requestPlayerList();
  } else {
    document.getElementById('p-count-lbl').textContent = 'Server container offline.';
    document.getElementById('player-list').innerHTML =
      '<div class="empty">Instance must be running to parse player data.</div>';
    document.getElementById('stat-players').textContent = `0/${currentProps['max-players'] || '20'}`;
  }
}

// ---------------------------------------------------------------------------
// Node polling — reduced rate on passive panels
// ---------------------------------------------------------------------------
let _pollInterval = null;

function startNodePolling() {
  if (_pollInterval) clearInterval(_pollInterval);
  _pollInterval = setInterval(() => {
    // Skip aggressive polling when user is on a passive panel
    const activePanel = PANELS.find(p => {
      const el = document.getElementById('panel-' + p);
      return el && el.classList.contains('active');
    });
    if (PASSIVE_PANELS.has(activePanel)) return;
    fetchNodes();
  }, 3000);
}

startNodePolling();

async function fetchNodes() {
  const res = await fetch('/api/servers').catch(() => null);
  const container = document.getElementById('container-list');

  if (!res || !res.ok) {
    if (!activeServer) container.innerHTML = '<div class="empty err">Cannot reach daemon API.</div>';
    return;
  }

  const data = await res.json().catch(() => null);
  if (!data) return;

  if (!data.servers || data.servers.length === 0) {
    container.innerHTML = '<div class="empty">No managed containers found. Deploy one below.</div>';
    return;
  }

  container.innerHTML = data.servers.map(s => {
    const up = String(s.status).toLowerCase().includes('up');
    return `
    <div class="srv-card">
      <div class="row-main">
        <div class="srv-name"><span class="status-dot${up ? ' up' : ''}"></span>${escapeHtml(s.name)}</div>
        <div class="srv-meta">${escapeHtml(s.status)} &middot; ${escapeHtml(s.ports || 'no bindings')}</div>
      </div>
      <div class="btn-row">
        <button class="mc-btn" onclick="setServerTarget('${escapeHtml(s.name)}','${escapeHtml(s.status)}');sw('overview',document.querySelector('[data-panel=overview]'))">SELECT</button>
        <button class="mc-btn" onclick="openDuplicate('${escapeHtml(s.name)}')">DUPLICATE</button>
        <button class="mc-btn" onclick="openUpdate('${escapeHtml(s.name)}')">UPDATE</button>
        <button class="mc-btn red" onclick="deleteServer('${escapeHtml(s.name)}')">DELETE</button>
      </div>
    </div>`;
  }).join('');

  if (activeServer) {
    const node = data.servers.find(s => s.name === activeServer);
    if (node) {
      updateServerStatusUI(node.status);
      if (node.status.toLowerCase().includes('up') && !playerPollTimer) {
        playerPollTimer = setInterval(requestPlayerList, 15000);
        requestPlayerList();
      }
    } else {
      updateServerStatusUI('exited');
    }
  }
}

// ---------------------------------------------------------------------------
// Create server — with port conflict check and button lockout
// ---------------------------------------------------------------------------
async function createServer() {
  const name = document.getElementById('new-name').value.trim();
  const type = document.getElementById('new-type').value;
  const version = document.getElementById('new-version').value;
  const snapshot = document.getElementById('new-snapshot').checked;
  const port = document.getElementById('new-port').value.trim() || '25565';
  const memory = document.getElementById('new-memory').value.trim() || '2G';
  const extraPorts = document.getElementById('new-extra-ports').value.trim();

  if (!name) { toast('Container ID is required.', 'error'); return; }

  // Only the Java port is pre-checked here. Voice chat and Bedrock ports are
  // deliberately left to the server, which shifts them to the next free number
  // instead of refusing the deploy — see AUX_PORTS in web_manager.py.
  const listRes = await fetch('/api/servers').catch(() => null);
  if (listRes && listRes.ok) {
    const listData = await listRes.json().catch(() => ({}));
    const conflict = (listData.servers || []).find(
      s => s.ports && s.ports.includes(`:${port}->`) && s.ports.includes(`:${port}->25565/tcp`));
    if (conflict) {
      toast(`Port ${port} is already used by '${conflict.name}'.`, 'error');
      return;
    }
  }

  const btn = document.getElementById('btn-deploy');
  btn.disabled = true;
  btn.textContent = 'Deploying...';

  const res = await fetch('/api/server/create', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, type, version, snapshot, port, memory, extra_ports: extraPorts }),
  }).catch(() => null);

  btn.disabled = false;
  btn.textContent = 'Deploy Node';

  if (!res) { toast('Network error during deploy.', 'error'); return; }

  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) {
    toast(data.detail || data.error || 'Deploy failed.', 'error');
  } else {
    toast(data.message || `Server '${name}' deployed!`, 'ok');
    (data.moved_ports || []).forEach(m =>
      toast(`${m.service}: ${m.from} was busy, using ${m.to}.`, 'ok'));
    fetchNodes();
    sw('instances', document.querySelector('[data-panel=instances]'));
  }
}

// ---------------------------------------------------------------------------
// Duplicate server — same build, new port, fresh world
// ---------------------------------------------------------------------------
function openDuplicate(name) {
  document.getElementById('dup-source').textContent = name;
  document.getElementById('dup-name').value = `${name}-copy`;
  document.getElementById('dup-port').value = '';
  document.getElementById('dup-world').value = 'world';
  document.getElementById('dup-seed').value = '';
  document.getElementById('dup-modal').classList.add('active');
}

function closeDuplicate() {
  document.getElementById('dup-modal').classList.remove('active');
}

async function runDuplicate() {
  const source   = document.getElementById('dup-source').textContent;
  const newName  = document.getElementById('dup-name').value.trim();
  const port     = document.getElementById('dup-port').value.trim();
  const world    = document.getElementById('dup-world').value.trim() || 'world';
  const seed     = document.getElementById('dup-seed').value.trim();

  if (!newName) { toast('The copy needs a name.', 'error'); return; }
  if (!port)    { toast('The copy needs a port.', 'error'); return; }

  const btn = document.getElementById('btn-dup-go');
  btn.disabled = true;
  btn.textContent = 'Cloning...';

  const res = await fetch(`/api/server/${encodeURIComponent(source)}/duplicate`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_name: newName, port, level_name: world, seed }),
  }).catch(() => null);

  btn.disabled = false;
  btn.textContent = 'Clone Node';

  if (!res) { toast('Network error during clone.', 'error'); return; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.error) {
    toast(data.detail || data.error || 'Clone failed.', 'error');
    return;
  }
  closeDuplicate();
  toast(data.message || `'${newName}' cloned.`, 'ok');
  (data.moved_ports || []).forEach(m =>
    toast(`${m.service}: ${m.from} was busy, using ${m.to}.`, 'ok'));
  fetchNodes();
}

// ---------------------------------------------------------------------------
// Update server — version / mods / world, via mc-update.py and packwiz
// ---------------------------------------------------------------------------
let updateTarget = null;
let updatePoll = null;

async function openUpdate(name) {
  updateTarget = name;
  document.getElementById('upd-source').textContent = name;
  document.getElementById('upd-report').innerHTML = '';
  document.getElementById('upd-log').textContent = '';
  document.getElementById('upd-modal').classList.add('active');

  const box = document.getElementById('upd-scopes');
  box.innerHTML = '<div class="empty">Checking what this server supports…</div>';

  const res = await fetch(`/api/server/${encodeURIComponent(name)}/update/options`)
    .catch(() => null);
  if (!res || !res.ok) {
    // A 404 here almost always means the page is newer than the running
    // service: static files are read from disk per request, routes only load
    // at startup.
    const detail = !res ? 'network error'
      : res.status === 404 ? 'HTTP 404 — the manager service is running older code; restart it'
      : `HTTP ${res.status} — ${(await res.json().catch(() => ({}))).detail || res.statusText}`;
    box.innerHTML = `<div class="empty">Could not read update options (${escapeHtml(detail)}).</div>`;
    return;
  }
  const opt = await res.json();
  document.getElementById('upd-current').textContent = opt.current_version || '?';

  const labels = {
    version: ['Server version', 'Resolves a target version, verifies every mod against it, takes a borg backup, then recreates the container.'],
    mods:    ['Mods', 'Runs packwiz update across the pack, pulling the newest build of each mod for the current version.'],
    world:   ['World / chunks (plan only)', 'Runs mc-chunkdiff.py plan — reads the seed, datapacks and version out of the live world and prints the recipe for building a reference. Read-only. Deleting untouched chunks is a separate, deliberate step on the command line.'],
  };

  box.innerHTML = Object.entries(opt.scopes).map(([key, s]) => {
    const [title, desc] = labels[key] || [key, ''];
    const dis = s.available ? '' : 'disabled';
    return `
      <label class="check-row upd-scope ${s.available ? '' : 'is-disabled'}">
        <input type="radio" name="upd-scope" value="${key}" ${dis}>
        <span>
          <strong>${escapeHtml(title)}</strong>
          <span class="card-hint" style="display:block;">${escapeHtml(desc)}</span>
          ${s.available ? '' :
            `<span class="card-hint" style="display:block;color:var(--warn,#c90);">
               Unavailable — ${escapeHtml(s.reason)}</span>`}
        </span>
      </label>`;
  }).join('');

  const first = box.querySelector('input[name=upd-scope]:not([disabled])');
  if (first) first.checked = true;
  pollUpdateStatus(true);
}

function closeUpdate() {
  document.getElementById('upd-modal').classList.remove('active');
  if (updatePoll) { clearInterval(updatePoll); updatePoll = null; }
}

function selectedScope() {
  const el = document.querySelector('input[name=upd-scope]:checked');
  return el ? el.value : null;
}

async function runUpdateCheck() {
  if (!updateTarget) return;
  const target = document.getElementById('upd-target').value.trim();
  const report = document.getElementById('upd-report');
  report.innerHTML = '<div class="empty">Resolving mods against the target version…</div>';

  const res = await fetch(`/api/server/${encodeURIComponent(updateTarget)}/update/check`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target }),
  }).catch(() => null);

  if (!res) { report.innerHTML = '<div class="empty">Network error.</div>'; return; }
  const d = await res.json().catch(() => ({}));
  if (!res.ok) {
    report.innerHTML = `<div class="empty">${escapeHtml(d.detail || 'Check failed.')}</div>`;
    return;
  }

  const blockers = d.blockers || [];
  const verdict = d.up_to_date
    ? `Already on ${escapeHtml(d.current)} — nothing to do.`
    : d.can_update
      ? `${escapeHtml(d.current)} → ${escapeHtml(d.target)}: all ${(d.mods || []).length} mods resolve.`
      : `${escapeHtml(d.current)} → ${escapeHtml(d.target)} is blocked by ${blockers.length} mod(s).`;

  report.innerHTML = `
    <div class="srv-meta" style="margin-bottom:6px;">${verdict}</div>
    ${blockers.length ? `<ul class="upd-blockers">${
      blockers.map(b => `<li>${escapeHtml(b)}</li>`).join('')}</ul>` : ''}
    ${(d.loose_jars || []).length ? `<div class="card-hint">Loose jars packwiz cannot
       track: ${escapeHtml((d.loose_jars || []).join(', '))}</div>` : ''}`;
}

async function runUpdateApply() {
  if (!updateTarget) return;
  const scope = selectedScope();
  if (!scope) { toast('Pick what to update.', 'error'); return; }

  const body = { scope, target: document.getElementById('upd-target').value.trim() };
  if (scope === 'world') body.dimension = document.getElementById('upd-dimension').value;

  if (scope === 'version' &&
      !confirm(`Update the server version for '${updateTarget}'?\n\n` +
               `This takes a backup, recreates the container and watches the boot. ` +
               `It can take several minutes.`)) return;

  const btn = document.getElementById('btn-upd-go');
  btn.disabled = true; btn.textContent = 'Starting…';

  const res = await fetch(`/api/server/${encodeURIComponent(updateTarget)}/update/apply`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => null);

  btn.disabled = false; btn.textContent = 'Run Update';

  const d = res ? await res.json().catch(() => ({})) : {};
  if (!res || !res.ok) { toast(d.detail || 'Could not start the update.', 'error'); return; }
  toast(d.message || 'Update started.', 'ok');
  pollUpdateStatus();
}

async function pollUpdateStatus(once) {
  if (updatePoll) { clearInterval(updatePoll); updatePoll = null; }

  const tick = async () => {
    if (!updateTarget) return;
    const res = await fetch(`/api/server/${encodeURIComponent(updateTarget)}/update/status`)
      .catch(() => null);
    if (!res || !res.ok) return;
    const d = await res.json().catch(() => ({}));
    const log = document.getElementById('upd-log');
    const badge = document.getElementById('upd-state');

    if (d.state === 'idle') { badge.textContent = ''; return; }
    badge.textContent = `${d.scope || ''} — ${d.state}`;
    if (d.output) log.textContent = d.output;

    if (d.state !== 'running') {
      if (updatePoll) { clearInterval(updatePoll); updatePoll = null; }
      toast(d.state === 'done' ? 'Update finished.' : 'Update failed — see the log.',
            d.state === 'done' ? 'ok' : 'error');
      fetchNodes();
    }
  };

  await tick();
  if (!once) updatePoll = setInterval(tick, 3000);
}

// ---------------------------------------------------------------------------
// Delete server
// ---------------------------------------------------------------------------
async function deleteServer(name) {
  if (!confirm(`Permanently delete '${name}'?`)) return;
  const res = await fetch(`/api/server/${name}/action`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'delete' }),
  }).catch(() => null);

  if (!res || !res.ok) { toast('Delete failed.', 'error'); return; }
  if (activeServer === name) clearTarget();
  toast(`'${name}' removed.`, 'ok');
  fetchNodes();
}

// ---------------------------------------------------------------------------
// Server actions (start / stop / restart)
// ---------------------------------------------------------------------------
async function sendAction(action) {
  if (!activeServer) return;
  if (action === 'start' || action === 'restart') {
    isStartingLocal = true;
    updateServerStatusUI('starting local');
  }
  const res = await fetch(`/api/server/${activeServer}/action`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  }).catch(() => null);

  if (!res || !res.ok) {
    logTerm('Docker daemon communication failure.', true);
    isStartingLocal = false;
    return;
  }
  const data = await res.json().catch(() => ({}));
  logTerm(data.message || data.detail || data.error || '', !!data.error);
  toast(data.message || `${action} sent.`, data.error ? 'error' : 'ok');
  fetchNodes();
}

// ---------------------------------------------------------------------------
// Player polling
// ---------------------------------------------------------------------------
function togglePolling() {
  pollingEnabled = !pollingEnabled;
  const btn = document.getElementById('btn-poll-toggle');
  btn.textContent = pollingEnabled ? 'POLL: ON' : 'POLL: OFF';
  btn.className = pollingEnabled ? 'mc-btn sm green' : 'mc-btn sm';
  if (pollingEnabled) requestPlayerList();
}

function requestPlayerList() {
  if (!activeServer || !pollingEnabled) return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method: 'player:list', target: activeServer }));
  }
}

// ---------------------------------------------------------------------------
// Player moderation
//
// Rows carry the player name in a data attribute and the list delegates
// clicks, so a name never has to survive a round trip through an interpolated
// onclick handler.
// ---------------------------------------------------------------------------
// Confirmed actions are the ones that visibly hit someone or hand out power.
const PLAYER_ROW_ACTIONS = [
  { act: 'nbt',              label: 'NBT',   cls: '' },
  { act: 'kick',            label: 'KICK',  cls: 'orange', confirm: 'Kick' },
  { act: 'ban',             label: 'BAN',   cls: 'red',    confirm: 'Ban', reason: true },
  { act: 'op',              label: 'OP',    cls: '',       confirm: 'Grant operator to' },
  { act: 'whitelist_add',   label: 'WL+',   cls: '' },
];

const ACTION_VERB = {
  kick: 'Kicked', ban: 'Banned', pardon: 'Unbanned', op: 'Opped', deop: 'De-opped',
  whitelist_add: 'Whitelisted', whitelist_remove: 'Removed from whitelist',
  gamemode: 'Gamemode set for', kill: 'Killed',
};

function renderPlayerRows(players) {
  const el = document.getElementById('player-list');
  if (!el) return;
  el.innerHTML = players.length === 0
    ? `<div class="empty">No players currently online.</div>`
    : players.map(p => `
        <div class="p-row" data-player="${escapeHtml(p)}">
          <div class="p-head"></div>
          <div class="p-name">${escapeHtml(p)}</div>
          <div class="p-acts">
            ${PLAYER_ROW_ACTIONS.map(a =>
              `<button class="mc-btn sm ${a.cls}" data-act="${a.act}">${a.label}</button>`).join('')}
          </div>
        </div>`).join('');
}

async function playerAction(player, action, arg = '') {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const data = await apiCall(`/api/server/${activeServer}/player/action`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ player, action, arg }),
  });
  if (!data) return;
  logTerm(`> ${data.command}`);
  if (data.message) logTerm(data.message);
  toast(`${ACTION_VERB[action] || action} ${player}.`, 'ok');
  requestPlayerList();
  // A ban or whitelist change is only visible once the list is re-read.
  if (['ban', 'pardon', 'op', 'deop', 'whitelist_add', 'whitelist_remove'].includes(action)) {
    loadAccessList();
  }
}

function handlePlayerRowClick(ev) {
  const hit = ev.target.closest('[data-act]');
  if (!hit) return;
  const row = hit.closest('.p-row');
  if (!row) return;
  const player = row.dataset.player;
  const act = hit.dataset.act;

  if (act === 'nbt') { showPlayerData(player); return; }

  const spec = PLAYER_ROW_ACTIONS.find(a => a.act === act);
  if (spec && spec.confirm && !confirm(`${spec.confirm} ${player}?`)) return;
  const reason = spec && spec.reason ? (prompt(`Reason for banning ${player}:`, '') ?? '') : '';
  playerAction(player, act, reason);
}

// The reason box only means something for kick and ban; gamemode reuses it as
// the mode, so the placeholder has to say which one is being asked for.
function syncModArg() {
  const action = document.getElementById('mod-action').value;
  const arg = document.getElementById('mod-arg');
  if (action === 'gamemode') {
    arg.placeholder = 'survival | creative | adventure | spectator';
    arg.disabled = false;
  } else if (action === 'kick' || action === 'ban') {
    arg.placeholder = 'Reason (optional)';
    arg.disabled = false;
  } else {
    arg.placeholder = 'Not used for this action';
    arg.value = '';
    arg.disabled = true;
  }
}

function runModAction() {
  const player = document.getElementById('mod-player').value.trim();
  const action = document.getElementById('mod-action').value;
  const arg = document.getElementById('mod-arg').value.trim();
  if (!player) { toast('Enter a player name.', 'error'); return; }
  playerAction(player, action, arg);
}

// ---------------------------------------------------------------------------
// Access lists — whitelist / ops / bans
// ---------------------------------------------------------------------------
const ACCESS_KINDS = [
  { id: 'whitelist',      label: 'Whitelist' },
  { id: 'ops',            label: 'Operators' },
  { id: 'banned-players', label: 'Banned Players' },
  { id: 'banned-ips',     label: 'Banned IPs' },
];

let accessKind = 'whitelist';

function renderAccessTabs() {
  const el = document.getElementById('access-tabs');
  if (!el) return;
  el.innerHTML = ACCESS_KINDS.map(k =>
    `<button class="mc-btn sm tab${k.id === accessKind ? ' active' : ''}"
       data-kind="${k.id}" onclick="switchAccessKind('${k.id}')">${k.label}</button>`).join('');
}

function switchAccessKind(kind) {
  accessKind = kind;
  const val = document.getElementById('access-value');
  const reason = document.getElementById('access-reason');
  if (val) val.placeholder = kind === 'banned-ips' ? 'IP address' : 'Player name';
  if (reason) reason.disabled = !kind.startsWith('banned');
  renderAccessTabs();
  loadAccessList();
}

function accessResetForServer() {
  const el = document.getElementById('access-list');
  if (el) el.innerHTML = '<div class="empty">Loading…</div>';
  if (document.getElementById('panel-players').classList.contains('active')) loadAccessList();
}

async function loadAccessList() {
  if (!activeServer) return;
  renderAccessTabs();
  const el = document.getElementById('access-list');
  if (!el) return;
  el.innerHTML = '<div class="empty">Reading the volume…</div>';

  const data = await apiCall(`/api/server/${activeServer}/access/${accessKind}`);
  if (!data) { el.innerHTML = '<div class="empty err">Could not read that list.</div>'; return; }

  const mode = document.getElementById('access-mode');
  if (mode) {
    mode.textContent = data.running ? 'live via RCON' : 'offline — editing the file';
  }

  const rows = data.entries || [];
  el.innerHTML = !rows.length
    ? `<div class="empty">Nothing in ${escapeHtml(data.file || accessKind)}.</div>`
    : rows.map(r => {
        const main = r.name || r.ip || '(unnamed)';
        const bits = [];
        if (r.uuid) bits.push(r.uuid);
        if (r.level !== undefined) bits.push(`level ${r.level}`);
        if (r.reason) bits.push(r.reason);
        if (r.expires && r.expires !== 'forever') bits.push(`expires ${r.expires}`);
        return `
          <div class="list-row" data-value="${escapeHtml(main)}">
            <div class="row-main">
              <div>${escapeHtml(main)}</div>
              ${bits.length ? `<div class="row-sub mono">${escapeHtml(bits.join(' · '))}</div>` : ''}
            </div>
            <button class="mc-btn red sm" data-act="access-remove">REMOVE</button>
          </div>`;
      }).join('');
}

async function accessEdit(op, value, reason = '') {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const data = await apiCall(`/api/server/${activeServer}/access/${accessKind}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ op, value, reason }),
  });
  if (!data) return;
  logTerm(data.message);
  toast(data.message, 'ok');
  loadAccessList();
}

function accessAdd() {
  const val = document.getElementById('access-value');
  const reason = document.getElementById('access-reason');
  const value = val.value.trim();
  if (!value) { toast('Enter a name or IP first.', 'error'); return; }
  accessEdit('add', value, reason.value.trim());
  val.value = '';
}

// ---------------------------------------------------------------------------
// Metrics charts
//
// Hand-rolled SVG rather than a charting library: the page ships no bundler
// and these are line charts over a few hundred points. The viewBox is a fixed
// grid stretched by CSS, so paths are laid out in chart units and strokes are
// kept honest with non-scaling-stroke.
// ---------------------------------------------------------------------------
const CHART_W = 600;

function niceMax(values, floor = 1) {
  const peak = Math.max(floor, ...values);
  // Round up to 1/2/5 x 10^n so the top gridline reads as a round number.
  const mag = Math.pow(10, Math.floor(Math.log10(peak)));
  const step = peak / mag <= 1 ? 1 : peak / mag <= 2 ? 2 : peak / mag <= 5 ? 5 : 10;
  return step * mag;
}

function chartPaths(samples, key, h, maxVal) {
  const n = samples.length;
  const xAt = i => (n < 2 ? CHART_W : (i / (n - 1)) * CHART_W);
  const yAt = v => h - Math.min(v / maxVal, 1) * h;

  // A null sample means the server was down — lift the pen so the line breaks
  // instead of drawing a straight run across the gap.
  const segments = [];
  let current = [];
  samples.forEach((s, i) => {
    const v = s[key];
    if (v === null || v === undefined) {
      if (current.length) segments.push(current);
      current = [];
      return;
    }
    current.push([xAt(i), yAt(v)]);
  });
  if (current.length) segments.push(current);

  const line = segments.map(seg =>
    seg.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('')).join(' ');
  const fill = segments.filter(s => s.length > 1).map(seg => {
    const pts = seg.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('');
    return `${pts}L${seg[seg.length - 1][0].toFixed(1)},${h}L${seg[0][0].toFixed(1)},${h}Z`;
  }).join(' ');
  return { line, fill, points: segments.reduce((a, s) => a + s.length, 0) };
}

function renderChart(samples, key, opts = {}) {
  const { height = 60, color = 'var(--mc-green)', unit = '', floor = 1 } = opts;
  const values = samples.map(s => s[key]).filter(v => v !== null && v !== undefined);
  if (!values.length) {
    return `<div class="chart-empty">No samples yet${unit ? ` for ${escapeHtml(unit)}` : ''}.</div>`;
  }
  const max = niceMax(values, floor);
  const { line, fill } = chartPaths(samples, key, height, max);
  const id = `g${Math.random().toString(36).slice(2, 8)}`;
  return `
    <svg class="chart" viewBox="0 0 ${CHART_W} ${height}" preserveAspectRatio="none"
         role="img" aria-label="${escapeHtml(unit)} over time">
      <defs>
        <linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stop-color="${color}" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <line class="chart-grid" x1="0" y1="${height / 2}" x2="${CHART_W}" y2="${height / 2}"/>
      <path d="${fill}" fill="url(#${id})" stroke="none"/>
      <path d="${line}" fill="none" stroke="${color}" stroke-width="1.5"
            vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    </svg>`;
}

function seriesSummary(samples, key, unit, digits = 0) {
  const values = samples.map(s => s[key]).filter(v => v !== null && v !== undefined);
  if (!values.length) return { now: '—', avg: '—', max: '—' };
  const fmt = v => `${v.toFixed(digits)}${unit}`;
  return {
    now: fmt(values[values.length - 1]),
    avg: fmt(values.reduce((a, b) => a + b, 0) / values.length),
    max: fmt(Math.max(...values)),
  };
}

function sparkBlock(title, samples, key, opts) {
  const s = seriesSummary(samples, key, opts.unit || '', opts.digits || 0);
  return `
    <div class="spark-block">
      <div class="spark-head">
        <span class="spark-title">${escapeHtml(title)}</span>
        <span class="spark-stats">
          <span>now <b>${escapeHtml(s.now)}</b></span>
          <span>avg ${escapeHtml(s.avg)}</span>
          <span>peak ${escapeHtml(s.max)}</span>
        </span>
      </div>
      ${renderChart(samples, key, opts)}
    </div>`;
}

// ---------------------------------------------------------------------------
// Overview history (active server)
// ---------------------------------------------------------------------------
async function loadServerMetrics() {
  if (!activeServer) return;
  const box = document.getElementById('hist-spark');
  if (!box) return;
  const minutes = document.getElementById('hist-range')?.value || '60';
  const data = await apiCall(
    `/api/server/${activeServer}/metrics?minutes=${encodeURIComponent(minutes)}`);
  if (!data) { box.innerHTML = '<div class="empty err">Could not load history.</div>'; return; }

  const samples = data.samples || [];
  if (!samples.length) {
    box.innerHTML = `<div class="empty">Nothing sampled yet — the daemon records every
      ${data.interval || 15}s, so check back shortly.</div>`;
    return;
  }
  const hasPlayers = samples.some(s => s.players !== null && s.players !== undefined);
  box.innerHTML = [
    sparkBlock('Container CPU', samples, 'cpu', { unit: '%', digits: 1, color: 'var(--mc-green)', floor: 10 }),
    sparkBlock('Container Memory', samples, 'mem_mb', { unit: ' MB', digits: 0, color: 'var(--mc-cyan)', floor: 64 }),
    hasPlayers
      ? sparkBlock('Players Online', samples, 'players', { unit: '', digits: 0, color: 'var(--mc-yellow)', floor: 4 })
      : `<div class="spark-block"><div class="spark-head"><span class="spark-title">Players Online</span></div>
         <div class="chart-empty">Recorded while the Players panel is polling — open it to start
         building this series.</div></div>`,
  ].join('');
}

// ---------------------------------------------------------------------------
// Cluster load (every server)
// ---------------------------------------------------------------------------
let clusterMetrics = null;

async function loadClusterMetrics() {
  const box = document.getElementById('cluster-graphs');
  if (!box) return;
  box.innerHTML = '<div class="empty">Reading samples…</div>';
  const minutes = document.getElementById('cluster-range')?.value || '60';
  const data = await apiCall(`/api/metrics?minutes=${encodeURIComponent(minutes)}`);
  if (!data) { box.innerHTML = '<div class="empty err">Could not load cluster metrics.</div>'; return; }
  clusterMetrics = data;
  renderClusterMetrics();
}

function renderClusterMetrics() {
  const box = document.getElementById('cluster-graphs');
  if (!box || !clusterMetrics) return;
  const metric = document.getElementById('cluster-metric')?.value || 'cpu';
  const isCpu = metric === 'cpu';
  const opts = isCpu
    ? { unit: '%', digits: 1, color: 'var(--mc-green)', floor: 10, height: 70 }
    : { unit: ' MB', digits: 0, color: 'var(--mc-cyan)', floor: 64, height: 70 };

  const servers = clusterMetrics.servers || [];
  if (!servers.length) {
    box.innerHTML = '<div class="empty">No managed containers found.</div>';
    return;
  }

  box.innerHTML = servers.map(s => {
    const samples = s.samples || [];
    const running = /^up/i.test(s.status || '');
    const summary = seriesSummary(samples, metric, opts.unit, opts.digits);
    const body = samples.length
      ? renderChart(samples, metric, opts)
      : `<div class="chart-empty">${running
          ? 'Waiting for the first sample.'
          : 'Not running — nothing to sample.'}</div>`;
    return `
      <div class="graph-card${running ? '' : ' dim'}">
        <div class="graph-head">
          <button class="graph-name" data-target="${escapeHtml(s.server)}"
            title="Make this the active instance">${escapeHtml(s.server)}</button>
          <span class="pill ${running ? 'pill-on' : 'pill-off'}">${running ? 'UP' : 'DOWN'}</span>
          <span class="spark-stats">
            <span>now <b>${escapeHtml(summary.now)}</b></span>
            <span>avg ${escapeHtml(summary.avg)}</span>
            <span>peak ${escapeHtml(summary.max)}</span>
          </span>
        </div>
        ${body}
      </div>`;
  }).join('');

  const hint = document.getElementById('cluster-hint');
  if (hint) {
    const total = servers.reduce((a, s) => a + (s.samples || []).length, 0);
    hint.textContent = `${servers.length} instance(s), ${total} sample(s) over the last `
      + `${clusterMetrics.minutes} minutes — one docker stats call every `
      + `${clusterMetrics.interval}s, no RCON involved.`;
  }
}

// ---------------------------------------------------------------------------
// Player NBT modal
// ---------------------------------------------------------------------------
const NBT_CATEGORIES = [
  { id: 'all', label: 'Raw NBT' },
  { id: 'identity', label: 'Identity' },
  { id: 'location', label: 'Location' },
  { id: 'inventory', label: 'Inventory' },
  { id: 'vitals', label: 'Vitals' },
  { id: 'mechanics', label: 'Mechanics' },
  { id: 'metadata', label: 'Metadata' }
];

function renderModalTabs() {
  const el = document.getElementById('modal-tabs');
  if (!el) return;
  el.innerHTML = NBT_CATEGORIES.map(c =>
    `<button class="mc-btn sm tab" data-cat="${c.id}" onclick="fetchCategory('${c.id}')">${c.label}</button>`
  ).join('');
}

function showPlayerData(playerName) {
  currentPlayerTarget = playerName;
  document.getElementById('player-modal').classList.add('active');
  document.getElementById('modal-pname').textContent = `${playerName} – Live NBT Query`;
  fetchCategory('all');
}

function fetchCategory(cat) {
  document.querySelectorAll('#modal-tabs .tab').forEach(t =>
    t.classList.toggle('active', t.dataset.cat === cat));
  document.getElementById('modal-pdata').textContent = `Pulling [${cat}] from RCON...`;
  if (ws && ws.readyState === WebSocket.OPEN && activeServer && currentPlayerTarget) {
    ws.send(JSON.stringify({
      method: 'player:data', target: activeServer, player: currentPlayerTarget, category: cat,
    }));
  }
}

function closeModal() {
  document.getElementById('player-modal').classList.remove('active');
  document.getElementById('modal-pdata').textContent = '';
  nbtRaw = '';
}

// ---------------------------------------------------------------------------
// SNBT
//
// `data get entity` answers with stringified NBT on one line — a few thousand
// characters of nested compounds with no whitespace, which is unreadable as
// text. This parses it into a tree so the modal can render it properly, and
// falls back to the raw string if anything unexpected turns up.
//
// Grammar: compounds {k: v}, lists [v, v], typed arrays [I; 1, 2], quoted and
// bare strings, and numbers carrying a type suffix (0b, 3s, 1.5f, 20.0d, 9L).
// ---------------------------------------------------------------------------
const SNBT_PREFIX_RE = /^.*?\bhas the following entity data:\s*/s;
const SNBT_BARE = /[A-Za-z0-9._+\-]/;
const SNBT_NUMBER = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?([bslfdBSLFD])?$/;

function parseSNBT(text) {
  let i = 0;
  const s = text;

  const fail = msg => { throw new Error(`${msg} at offset ${i}`); };
  const ws = () => { while (i < s.length && /\s/.test(s[i])) i++; };

  function parseQuoted() {
    const quote = s[i++];
    let out = '';
    while (i < s.length) {
      const c = s[i++];
      if (c === '\\') { out += s[i++] ?? ''; continue; }
      if (c === quote) return out;
      out += c;
    }
    fail('unterminated string');
  }

  function parseBare() {
    const start = i;
    while (i < s.length && SNBT_BARE.test(s[i])) i++;
    if (i === start) fail(`unexpected character '${s[i]}'`);
    return s.slice(start, i);
  }

  function parseScalar() {
    const token = parseBare();
    if (token === 'true' || token === 'false') {
      return { type: 'bool', value: token === 'true' };
    }
    const m = SNBT_NUMBER.exec(token);
    if (m) {
      const suffix = m[1] || '';
      return {
        type: 'number',
        value: suffix ? token.slice(0, -1) : token,
        suffix: suffix.toLowerCase(),
      };
    }
    return { type: 'string', value: token };
  }

  function parseCompound() {
    i++;                       // {
    const entries = [];
    ws();
    if (s[i] === '}') { i++; return { type: 'compound', entries }; }
    for (;;) {
      ws();
      const key = (s[i] === '"' || s[i] === "'") ? parseQuoted() : parseBare();
      ws();
      if (s[i] !== ':') fail(`expected ':' after key '${key}'`);
      i++;
      entries.push({ key, node: parseValue() });
      ws();
      if (s[i] === ',') { i++; continue; }
      if (s[i] === '}') { i++; return { type: 'compound', entries }; }
      fail('expected ,or } in compound');
    }
  }

  function parseList() {
    i++;                       // [
    ws();
    // Typed arrays announce themselves as [I; …], [B; …] or [L; …].
    let arrayType = null;
    if (/^[IBL];/.test(s.slice(i, i + 2))) {
      arrayType = s[i];
      i += 2;
    }
    const items = [];
    ws();
    if (s[i] === ']') { i++; return { type: 'list', items, arrayType }; }
    for (;;) {
      items.push(parseValue());
      ws();
      if (s[i] === ',') { i++; continue; }
      if (s[i] === ']') { i++; return { type: 'list', items, arrayType }; }
      fail('expected , or ] in list');
    }
  }

  function parseValue() {
    ws();
    if (i >= s.length) fail('unexpected end of input');
    if (s[i] === '{') return parseCompound();
    if (s[i] === '[') return parseList();
    if (s[i] === '"' || s[i] === "'") return { type: 'string', value: parseQuoted() };
    return parseScalar();
  }

  const root = parseValue();
  ws();
  if (i < s.length) fail('trailing characters after value');
  return root;
}

// ---------------------------------------------------------------------------
// NBT rendering
// ---------------------------------------------------------------------------
const NBT_TYPE_WORD = { b: 'byte', s: 'short', l: 'long', f: 'float', d: 'double' };

// Deep branches start collapsed; the top two levels stay open so the modal
// opens on something readable rather than a wall of triangles.
const NBT_OPEN_DEPTH = 1;

function nbtCount(node) {
  if (node.type === 'compound') return `${node.entries.length} key${node.entries.length === 1 ? '' : 's'}`;
  if (node.type === 'list') return `${node.items.length} item${node.items.length === 1 ? '' : 's'}`;
  return '';
}

// A collapsed row is far more useful with a hint of what is inside it, so the
// shapes that show up most in player data get a one-line summary.
function nbtPreview(node) {
  if (node.type === 'list') {
    if (!node.items.length) return 'empty';
    if (node.arrayType) {
      const head = node.items.slice(0, 4).map(n => n.value).join(', ');
      return node.items.length > 4 ? `${head}, …` : head;
    }
    return '';
  }
  if (node.type !== 'compound') return '';
  const find = key => {
    const hit = node.entries.find(e => e.key === key);
    return hit && hit.node.type !== 'compound' && hit.node.type !== 'list' ? hit.node.value : null;
  };
  const id = find('id');
  if (id) {
    const count = find('Count') || find('count');
    return count ? `${id} x${count}` : String(id);
  }
  const name = find('Name');
  if (name) return String(name);
  return '';
}

function nbtLeafHtml(node) {
  if (node.type === 'number') {
    const word = NBT_TYPE_WORD[node.suffix];
    return `<span class="nbt-val nbt-num">${escapeHtml(node.value)}</span>`
      + (word ? `<span class="nbt-type">${word}</span>` : '');
  }
  if (node.type === 'bool') {
    return `<span class="nbt-val nbt-bool">${node.value}</span>`;
  }
  return `<span class="nbt-val nbt-str">${escapeHtml(node.value)}</span>`;
}

function nbtNodeHtml(node, key, depth) {
  const label = key === null
    ? ''
    : `<span class="nbt-key">${escapeHtml(key)}</span>`;

  if (node.type === 'compound' || node.type === 'list') {
    const preview = nbtPreview(node);
    const kids = node.type === 'compound'
      ? node.entries.map(e => nbtNodeHtml(e.node, e.key, depth + 1)).join('')
      : node.items.map((n, idx) => nbtNodeHtml(n, String(idx), depth + 1)).join('');
    const empty = node.type === 'compound' ? !node.entries.length : !node.items.length;
    const typeWord = node.type === 'list'
      ? (node.arrayType ? `${node.arrayType}-array` : 'list')
      : 'compound';
    return `
      <details class="nbt-node"${depth <= NBT_OPEN_DEPTH && !empty ? ' open' : ''}>
        <summary>
          ${label}
          <span class="nbt-type">${typeWord}</span>
          <span class="nbt-count">${escapeHtml(nbtCount(node))}</span>
          ${preview ? `<span class="nbt-preview">${escapeHtml(preview)}</span>` : ''}
        </summary>
        <div class="nbt-children">${kids || '<div class="nbt-leaf nbt-dim">(empty)</div>'}</div>
      </details>`;
  }

  return `<div class="nbt-leaf">${label}${nbtLeafHtml(node)}</div>`;
}

let nbtRaw = '';
let nbtMode = 'tree';

function renderPlayerData(raw) {
  nbtRaw = raw || '';
  const box = document.getElementById('modal-pdata');
  if (!box) return;

  if (nbtMode === 'raw') {
    box.className = 'modal-content mono';
    box.textContent = nbtRaw;
    return;
  }

  const body = nbtRaw.replace(SNBT_PREFIX_RE, '').trim();
  if (!body) { box.className = 'modal-content'; box.textContent = '(no data)'; return; }

  let tree;
  try {
    tree = parseSNBT(body);
  } catch (err) {
    // Not SNBT — an error message from the server, or a shape this parser does
    // not know. Showing it verbatim beats showing nothing.
    box.className = 'modal-content mono';
    box.textContent = nbtRaw;
    return;
  }
  box.className = 'modal-content nbt-tree';
  box.innerHTML = nbtNodeHtml(tree, null, 0);
}

function toggleNbtView() {
  nbtMode = nbtMode === 'tree' ? 'raw' : 'tree';
  const btn = document.getElementById('nbt-view-btn');
  if (btn) btn.textContent = nbtMode === 'tree' ? 'RAW' : 'TREE';
  renderPlayerData(nbtRaw);
}

function expandNbt(open) {
  document.querySelectorAll('#modal-pdata .nbt-node').forEach(d => { d.open = open; });
}

function copyNbt() {
  if (!nbtRaw) { toast('Nothing to copy yet.', 'error'); return; }
  navigator.clipboard.writeText(nbtRaw)
    .then(() => toast('Raw NBT copied.', 'ok'))
    .catch(() => toast('Clipboard blocked by the browser.', 'error'));
}

// ---------------------------------------------------------------------------
// Properties
// ---------------------------------------------------------------------------
async function loadPropsFromServer() {
  if (!activeServer) return;
  const res = await fetch(`/api/server/${activeServer}/properties`).catch(() => null);
  const pGrid = document.getElementById('props-grid');
  const wGrid = document.getElementById('world-grid');
  if (!res || !res.ok) {
    const msg = '<div class="empty err">Could not load server.properties.</div>';
    pGrid.innerHTML = msg; wGrid.innerHTML = msg; return;
  }
  const data = await res.json().catch(() => ({}));
  if (data.error || data.detail) {
    const msg = `<div class="empty">${escapeHtml(data.error || data.detail)}</div>`;
    pGrid.innerHTML = msg; wGrid.innerHTML = msg; return;
  }
  currentProps = data;
  renderBothGrids();
}

function propSlot(k, v) {
  return `
      <div class="prop-slot" data-key="${escapeHtml(k)}">
        <div class="prop-lbl" title="${escapeHtml(k)}">${escapeHtml(k)}</div>
        <input class="mc-input" id="prop-${escapeHtml(k)}" value="${escapeHtml(v)}"
          onchange="updatePropMemory('${escapeHtml(k)}',this.value)">
      </div>`;
}

function renderBothGrids() {
  // Bucket every known key into its group; anything unrecognised lands in Other,
  // so a new Mojang property still shows up instead of silently disappearing.
  const buckets = PROP_GROUPS.map(g => ({ name: g.name, keys: [] }));
  const other = { name: 'Other', keys: [] };

  Object.keys(currentProps)
    .filter(k => !SKIP_PROPS.has(k))
    .sort()
    .forEach(k => {
      const idx = PROP_GROUPS.findIndex(g => g.keys.includes(k));
      (idx >= 0 ? buckets[idx] : other).keys.push(k);
    });

  const groups = buckets.concat([other]).filter(g => g.keys.length);
  document.getElementById('props-grid').innerHTML = groups.length === 0
    ? '<div class="empty">No properties returned by the daemon.</div>'
    : groups.map((g, i) => `
      <details class="prop-group"${i === 0 ? ' open' : ''}>
        <summary>${escapeHtml(g.name)}<span class="prop-count">${g.keys.length}</span></summary>
        <div class="prop-grid">${g.keys.map(k => propSlot(k, currentProps[k])).join('')}</div>
      </details>`).join('');

  document.getElementById('world-grid').innerHTML = WORLD_KEYS
    .map(k => propSlot(k, currentProps[k] !== undefined ? currentProps[k] : ''))
    .join('');

  // Re-apply any active filter so the view does not jump back to "everything".
  const filterEl = document.getElementById('props-filter');
  if (filterEl && filterEl.value) filterProps(filterEl.value);

  updateOverviewTags();
}

function filterProps(term) {
  const q = term.trim().toLowerCase();
  document.querySelectorAll('#props-grid .prop-group').forEach(group => {
    let shown = 0;
    group.querySelectorAll('.prop-slot').forEach(slot => {
      const hit = !q || slot.dataset.key.toLowerCase().includes(q);
      slot.hidden = !hit;
      if (hit) shown++;
    });
    group.hidden = shown === 0;
    const count = group.querySelector('.prop-count');
    if (count) count.textContent = shown;
    if (q) group.open = true;
  });
}

function expandAllProps(open) {
  document.querySelectorAll('#props-grid .prop-group').forEach(g => { g.open = open; });
}

function updatePropMemory(key, val) {
  currentProps[key] = val;
  updateOverviewTags();
  ['save-hint-props', 'save-hint-world'].forEach(id => {
    const el = document.getElementById(id);
    el.textContent = '* Unsaved changes';
    el.className = 'save-hint dirty';
  });
}

function updateOverviewTags() {
  const mode = currentProps['gamemode'] || 'N/A';
  const diff = currentProps['difficulty'] || 'N/A';
  const port = currentProps['server-port'] || 'N/A';
  document.getElementById('tag-mode').textContent = mode.charAt(0).toUpperCase() + mode.slice(1);
  document.getElementById('tag-diff').textContent = diff.charAt(0).toUpperCase() + diff.slice(1);
  document.getElementById('tag-port').textContent = port;
}

async function saveProps() {
  if (!activeServer) return;
  const res = await fetch(`/api/server/${activeServer}/properties/save`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(currentProps),
  }).catch(() => null);

  if (!res || !res.ok) { toast('Save failed.', 'error'); return; }
  const data = await res.json().catch(() => ({}));
  logTerm(data.message || data.detail || data.error || '', !!data.error);
  toast(data.message || 'Properties saved.', data.error ? 'error' : 'ok');

  ['save-hint-props', 'save-hint-world'].forEach(id => {
    const el = document.getElementById(id);
    el.textContent = '[SAVED]';
    el.className = 'save-hint saved';
    setTimeout(() => {
      el.textContent = '';
      el.className = 'save-hint';
    }, 3000);
  });
}

// ---------------------------------------------------------------------------
// Upload / paste
// ---------------------------------------------------------------------------
function parsePropsText(rawText) {
  const dict = {};
  rawText.split('\n').forEach(line => {
    const t = line.trim();
    if (!t || t[0] === '#') return;
    const idx = t.indexOf('=');
    if (idx < 0) return;
    dict[t.slice(0, idx).trim()] = t.slice(idx + 1).trim();
  });
  return dict;
}

function handleDrop(e) {
  e.preventDefault();
  document.getElementById('upload-zone').classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if (file) processFileBlock(file);
}
function handleFileInput(e) { const f = e.target.files[0]; if (f) processFileBlock(f); }

function processFileBlock(file) {
  if (!activeServer) {
    document.getElementById('upload-status').innerHTML = '<span class="err">Select an active server first.</span>';
    return;
  }
  const reader = new FileReader();
  reader.onload = e => {
    const dict = parsePropsText(e.target.result);
    const statusBox = document.getElementById('upload-status');
    if (!Object.keys(dict).length) {
      statusBox.innerHTML = '<span class="err">No valid property keys found.</span>'; return;
    }
    currentProps = { ...currentProps, ...dict };
    renderBothGrids(); saveProps();
    statusBox.innerHTML = `<span class="ok">[OK] Injected ${Object.keys(dict).length} parameters.</span>`;
  };
  reader.readAsText(file);
}

function loadFromPaste() {
  if (!activeServer) { toast('Select a container target first.', 'error'); return; }
  const text = document.getElementById('paste-area').value.trim();
  if (!text) return;
  const dict = parsePropsText(text);
  if (!Object.keys(dict).length) { toast('No valid property keys in text.', 'error'); return; }
  currentProps = { ...currentProps, ...dict };
  renderBothGrids(); saveProps();
  toast('Properties merged from paste.', 'ok');
}

// ---------------------------------------------------------------------------
// Addons / Mods
// ---------------------------------------------------------------------------
async function loadInstalledAddons() {
  if (!activeServer) return;
  const loading = '<div class="empty">Scanning volume…</div>';
  ['registry-datapacks', 'registry-mods', 'registry-plugins'].forEach(id => {
    document.getElementById(id).innerHTML = loading;
  });

  const res = await fetch(`/api/server/${activeServer}/addons`).catch(() => null);
  if (!res || !res.ok) {
    const err = '<div class="empty err">API error – check backend.</div>';
    ['registry-datapacks', 'registry-mods', 'registry-plugins'].forEach(id => {
      document.getElementById(id).innerHTML = err;
    });
    return;
  }
  const data = await res.json().catch(() => ({}));
  const renderList = items => {
    if (!items || !items.length) return '<div class="empty">None detected</div>';
    return items.map(item =>
      `<div class="list-row"><div class="row-main mono">${escapeHtml(item)}</div></div>`).join('');
  };
  document.getElementById('registry-datapacks').innerHTML = renderList(data.datapacks);
  document.getElementById('registry-mods').innerHTML = renderList(data.mods);
  document.getElementById('registry-plugins').innerHTML = renderList(data.plugins);
}

function handleDatapackDrop(e) {
  e.preventDefault();
  document.getElementById('dp-upload-zone').classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if (file) uploadDatapackFile(file);
}
function handleDatapackInput(e) { const f = e.target.files[0]; if (f) uploadDatapackFile(f); }

async function uploadDatapackFile(file) {
  if (!activeServer) {
    document.getElementById('dp-upload-status').innerHTML = '<span class="err">Select a target container first.</span>';
    return;
  }
  const statusBox = document.getElementById('dp-upload-status');
  statusBox.textContent = 'Uploading...';
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`/api/server/${activeServer}/datapacks/upload`, {
    method: 'POST', body: formData,
  }).catch(() => null);
  if (!res || !res.ok) {
    const data = res ? await res.json().catch(() => ({})) : {};
    statusBox.innerHTML = `<span class="err">${escapeHtml(data.detail || 'Upload failed.')}</span>`;
    return;
  }
  const data = await res.json().catch(() => ({}));
  statusBox.innerHTML = `<span class="ok">${escapeHtml(data.message || 'Uploaded.')}</span>`;
  toast(data.message || 'Datapack uploaded.', 'ok');
  loadInstalledAddons();
}

// ---------------------------------------------------------------------------
// Packwiz — host binary + per-server pack
//
// Upstream publishes no tagged release (its CI only uploads GitHub Actions
// artifacts, which need an authenticated fetch), so the daemon builds packwiz
// from source with `go install`. That takes minutes on first run.
// ---------------------------------------------------------------------------
function addExtraPort(spec) {
  const el = document.getElementById('new-extra-ports');
  if (!el) return;
  const parts = el.value.split(/[,\s]+/).filter(Boolean);
  if (!parts.includes(spec)) parts.push(spec);
  el.value = parts.join(', ');
}

async function refreshPackwizStatus() {
  const el = document.getElementById('packwiz-status');
  if (!el) return;
  const res = await fetch('/api/packwiz/status').catch(() => null);
  if (!res || !res.ok) {
    el.innerHTML = '<span class="err">Could not reach the daemon.</span>';
    return;
  }
  const d = await res.json().catch(() => ({}));
  if (d.installed) {
    el.innerHTML = `<span class="ok">Installed${d.version ? ' — ' + escapeHtml(d.version) : ''}</span>`;
  } else if (d.go_available) {
    el.textContent = 'Not installed. Setting up builds it from source — allow a few minutes.';
  } else {
    el.innerHTML = '<span class="err">Not installed, and the host has no Go toolchain to '
      + 'build it with. Install Go 1.24+ on the host first.</span>';
  }
}

async function setupPackwiz() {
  if (!activeServer) { toast('Select a target server first.', 'error'); return; }
  const btn = document.getElementById('btn-packwiz-setup');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'SETTING UP — THIS CAN TAKE A FEW MINUTES';
  logTerm(`Setting up packwiz for ${activeServer}...`);

  const res = await fetch(`/api/server/${activeServer}/packwiz/setup`, { method: 'POST' })
    .catch(() => null);

  btn.disabled = false;
  btn.textContent = label;

  if (!res || !res.ok) {
    const d = res ? await res.json().catch(() => ({})) : {};
    const msg = d.detail || 'Packwiz setup failed.';
    logTerm(msg, true);
    toast(msg, 'error');
    refreshPackwizStatus();
    return;
  }
  const d = await res.json().catch(() => ({}));
  logTerm(d.message || 'Packwiz ready.');
  toast(d.message || 'Packwiz ready.', 'ok');
  refreshPackwizStatus();
}

async function runPackwiz(action) {
  if (!activeServer) return;
  const mod = document.getElementById('mod-slug').value.trim();
  if (!mod) return;
  logTerm(`Packwiz ${action}: ${mod}`);
  const res = await fetch(`/api/server/${activeServer}/packwiz`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mod, action }),
  }).catch(() => null);
  if (!res || !res.ok) {
    const data = res ? await res.json().catch(() => ({})) : {};
    logTerm(data.detail || 'Packwiz request failed.', true);
    toast(data.detail || 'Packwiz failed.', 'error');
    return;
  }
  const data = await res.json().catch(() => ({}));
  logTerm(data.message || data.error || '', !!data.error);
  toast(data.message || `Packwiz ${action} done.`, 'ok');
}

// ---------------------------------------------------------------------------
// Automation — Backups
// ---------------------------------------------------------------------------
async function loadBackups() {
  const container = document.getElementById('backup-list');
  container.innerHTML = '<div class="empty">Fetching archives…</div>';
  const data = await apiCall('/api/backups');
  if (!data) {
    container.innerHTML = '<div class="empty err">Failed to fetch backups.</div>';
    return;
  }

  const total = document.getElementById('backup-total');
  if (total) {
    total.textContent = data.entries && data.entries.length
      ? `${data.entries.length} file(s), ${data.total_human}`
      : '';
  }
  if (data.settings) applyRetentionInputs(data.settings);

  const rows = data.entries || [];
  container.innerHTML = !rows.length
    ? '<div class="empty">No archives found in the _backups directory.</div>'
    : rows.map(b => `
        <div class="list-row" data-backup="${escapeHtml(b.name)}">
          <div class="row-main">
            <div class="mono">${escapeHtml(b.name)}</div>
            <div class="row-sub">${escapeHtml(b.human)} · ${escapeHtml(fmtTime(b.mtime))}</div>
          </div>
          <button class="mc-btn orange sm" data-act="restore">RESTORE</button>
          <button class="mc-btn red sm" data-act="delete-backup">DEL</button>
        </div>`).join('');
}

// ---------------------------------------------------------------------------
// Backup retention
// ---------------------------------------------------------------------------
function applyRetentionInputs(s) {
  const count = document.getElementById('ret-count');
  const days = document.getElementById('ret-days');
  const on = document.getElementById('ret-enabled');
  if (count) count.value = s.backup_keep_count;
  if (days) days.value = s.backup_keep_days;
  if (on) on.checked = !!s.backup_prune_enabled;
}

async function saveRetention() {
  const body = {
    backup_keep_count: Number(document.getElementById('ret-count').value || 0),
    backup_keep_days: Number(document.getElementById('ret-days').value || 0),
    backup_prune_enabled: document.getElementById('ret-enabled').checked,
  };
  const data = await apiCall('/api/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!data) return;
  toast('Retention policy saved.', 'ok');
}

async function previewPrune() {
  const data = await apiCall('/api/backups/prune', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dry_run: true }),
  });
  if (!data) return;
  if (!data.removed.length) { toast(data.message, 'ok'); return; }
  logTerm(`Prune preview — ${data.removed.length} archive(s) would go:`);
  data.removed.forEach(f => logTerm(`  ${f}`));
  toast(`${data.removed.length} archive(s) would be pruned — see the console.`, 'info');
}

async function runPrune() {
  if (!confirm('Delete every archive outside the retention policy? This cannot be undone.')) return;
  const data = await apiCall('/api/backups/prune', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!data) return;
  logTerm(data.message);
  toast(data.message, 'ok');
  loadBackups();
}

async function deleteBackup(filename) {
  if (!confirm(`Permanently delete ${filename}?`)) return;
  const data = await apiCall(`/api/backups/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  if (!data) return;
  toast(data.message, 'ok');
  loadBackups();
}

// ---------------------------------------------------------------------------
// Settings — watchdog + notifications
// ---------------------------------------------------------------------------
const SETTINGS_FIELDS = [
  ['wd-enabled', 'watchdog_enabled', 'check'],
  ['wd-restart', 'watchdog_restart', 'check'],
  ['wd-max', 'watchdog_max_restarts', 'num'],
  ['wd-window', 'watchdog_window_minutes', 'num'],
  ['hook-url', 'webhook_url', 'text'],
  ['hook-kind', 'webhook_kind', 'text'],
  ['nt-crash', 'notify_crash', 'check'],
  ['nt-restart', 'notify_restart', 'check'],
  ['nt-backup', 'notify_backup', 'check'],
  ['nt-state', 'notify_state', 'check'],
];

async function loadSettings() {
  const data = await apiCall('/api/settings');
  if (!data) return;
  SETTINGS_FIELDS.forEach(([id, key, kind]) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (kind === 'check') el.checked = !!data[key];
    else el.value = data[key];
  });
  applyRetentionInputs(data);
}

async function saveSettings() {
  const body = {};
  SETTINGS_FIELDS.forEach(([id, key, kind]) => {
    const el = document.getElementById(id);
    if (!el) return;
    body[key] = kind === 'check' ? el.checked : kind === 'num' ? Number(el.value || 0) : el.value;
  });
  const hint = document.getElementById('settings-hint');
  const data = await apiCall('/api/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!data) { if (hint) { hint.textContent = 'Save failed.'; hint.className = 'save-hint dirty'; } return; }
  if (hint) {
    hint.textContent = '[SAVED]';
    hint.className = 'save-hint saved';
    setTimeout(() => { hint.textContent = ''; hint.className = 'save-hint'; }, 3000);
  }
  toast('Settings saved.', 'ok');
}

async function testWebhook() {
  const data = await apiCall('/api/settings/test-webhook', { method: 'POST' });
  if (!data) return;
  toast(data.message, 'ok');
}

// ---------------------------------------------------------------------------
// server.properties revision history
// ---------------------------------------------------------------------------
let propRevision = null;

function propHistoryResetForServer() {
  propRevision = null;
  const diff = document.getElementById('prop-diff');
  if (diff) { diff.hidden = true; diff.innerHTML = ''; }
  const list = document.getElementById('prop-history');
  if (list) list.innerHTML = '<div class="empty">Loading…</div>';
  if (document.getElementById('panel-properties').classList.contains('active')) loadPropHistory();
}

async function loadPropHistory() {
  if (!activeServer) return;
  const el = document.getElementById('prop-history');
  if (!el) return;
  const data = await apiCall(`/api/server/${activeServer}/properties/history`);
  if (!data) { el.innerHTML = '<div class="empty err">Could not read revisions.</div>'; return; }

  const revs = data.revisions || [];
  el.innerHTML = !revs.length
    ? '<div class="empty">No revisions yet — the next save creates one.</div>'
    : revs.map(r => `
        <div class="list-row" data-rev="${escapeHtml(r.id)}">
          <div class="row-main">
            <div>${escapeHtml(fmtTime(r.saved))}</div>
            <div class="row-sub">${escapeHtml(r.note || 'saved')} · ${r.keys} keys ·
              ${r.changes === 0 ? 'identical to current' : `${r.changes} differ from current`}</div>
          </div>
          <button class="mc-btn sm" data-act="prop-view">DIFF</button>
          <button class="mc-btn orange sm" data-act="prop-rollback"
            ${r.changes === 0 ? 'disabled' : ''}>REVERT</button>
        </div>`).join('');
}

async function viewPropRevision(rev) {
  if (!activeServer) return;
  const box = document.getElementById('prop-diff');
  if (!box) return;
  const data = await apiCall(
    `/api/server/${activeServer}/properties/history/${encodeURIComponent(rev)}`);
  if (!data) return;
  propRevision = rev;

  const rows = data.diff || [];
  box.hidden = false;
  box.innerHTML = `
    <h4 class="section-heading">Reverting to ${escapeHtml(fmtTime(data.saved))} would change
      ${rows.length} key${rows.length === 1 ? '' : 's'}</h4>
    ${!rows.length
      ? '<div class="empty">This revision matches the file on disk exactly.</div>'
      : `<div class="diff-list">${rows.map(r => `
          <div class="diff-row diff-${escapeHtml(r.change)}">
            <span class="diff-key mono">${escapeHtml(r.key)}</span>
            <span class="diff-before mono">${r.before === null ? '—' : escapeHtml(r.before)}</span>
            <span class="diff-arrow">→</span>
            <span class="diff-after mono">${r.after === null ? '—' : escapeHtml(r.after)}</span>
          </div>`).join('')}</div>`}`;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function rollbackProps(rev) {
  if (!activeServer) return;
  if (!confirm(`Roll ${activeServer}'s server.properties back to ${rev}?\n\n`
    + 'The current file is snapshotted first, so this can be undone.')) return;
  const data = await apiCall(`/api/server/${activeServer}/properties/rollback`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: rev }),
  });
  if (!data) return;
  logTerm(data.message);
  toast(data.message, 'ok');
  const diff = document.getElementById('prop-diff');
  if (diff) { diff.hidden = true; diff.innerHTML = ''; }
  await loadPropsFromServer();
  loadPropHistory();
}

// ---------------------------------------------------------------------------
// Modrinth search
// ---------------------------------------------------------------------------
// Seed the filters from the instance itself, so a search is scoped correctly
// without the user re-entering what the server already knows. Only ever fills
// blanks — whatever the user typed wins.
async function seedModrinthFilters() {
  if (!activeServer) return;
  const loader = document.getElementById('mr-loader');
  const version = document.getElementById('mr-version');
  const type = document.getElementById('mr-type');
  if (!loader || !version) return;

  const res = await fetch(`/api/server/${activeServer}/runtime`).catch(() => null);
  if (!res || !res.ok) return;
  const d = await res.json().catch(() => ({}));
  if (!loader.value && d.loader) loader.value = d.loader;
  if (!version.value && /^\d+\.\d+(\.\d+)?$/.test(d.version || '')) version.value = d.version;
  if (type && d.project_type && !type.dataset.touched) type.value = d.project_type;
}

async function searchModrinth() {
  const box = document.getElementById('mr-results');
  if (!box) return;
  const q = document.getElementById('mr-query').value.trim();
  const params = new URLSearchParams({
    q,
    project_type: document.getElementById('mr-type').value,
    loader: document.getElementById('mr-loader').value.trim(),
    version: document.getElementById('mr-version').value.trim(),
  });
  box.innerHTML = '<div class="empty">Searching Modrinth…</div>';
  const data = await apiCall(`/api/modrinth/search?${params}`);
  if (!data) { box.innerHTML = '<div class="empty err">Search failed.</div>'; return; }

  const hits = data.hits || [];
  box.innerHTML = !hits.length
    ? '<div class="empty">Nothing matched. Try loosening the loader or version filter.</div>'
    : hits.map(h => `
        <div class="mr-row" data-slug="${escapeHtml(h.slug)}">
          ${h.icon_url
            ? `<img class="mr-icon" src="${escapeHtml(h.icon_url)}" alt="" loading="lazy">`
            : '<span class="mr-icon mr-icon-blank"><i class="ti ti-package"></i></span>'}
          <div class="mr-main">
            <div class="mr-title">
              ${escapeHtml(h.title)}
              <span class="mr-slug mono">${escapeHtml(h.slug)}</span>
            </div>
            <div class="mr-desc">${escapeHtml(h.description)}</div>
            <div class="mr-meta">
              <span>${formatDownloads(h.downloads)} downloads</span>
              ${h.server_side === 'unsupported'
                ? '<span class="mr-warn">client-side only</span>'
                : `<span>server: ${escapeHtml(h.server_side)}</span>`}
              ${h.categories.length ? `<span>${escapeHtml(h.categories.join(', '))}</span>` : ''}
            </div>
          </div>
          <button class="mc-btn green sm" data-act="mr-add">ADD</button>
        </div>`).join('');
}

function formatDownloads(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n || 0);
}

function modrinthAdd(slug) {
  const input = document.getElementById('mod-slug');
  if (input) input.value = slug;
  runPackwiz('add');
}

// Add the new execution function:
async function executeRestore(filename) {
  if (!activeServer) {
    toast('Select a target server first.', 'error');
    return;
  }

  if (!confirm(`WARNING: Restoring will overwrite current world data for '${activeServer}' and restart the server. Proceed?`)) {
    return;
  }

  logTerm(`Initiating volume restore from ${filename}...`);
  const res = await fetch(`/api/server/${activeServer}/restore`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename })
  }).catch(() => null);

  if (!res || !res.ok) {
    const data = res ? await res.json().catch(() => ({})) : {};
    logTerm(data.detail || 'Restore failed.', true);
    toast(data.detail || 'Restore failed.', 'error');
    return;
  }

  const data = await res.json().catch(() => ({}));
  logTerm(data.message || 'Restore completed.');
  toast(data.message || 'Volume restored.', 'ok');
}

async function triggerBackup() {
  if (!activeServer) { toast('Select a target server first.', 'error'); return; }
  logTerm(`Initiating volume snapshot for ${activeServer}...`);
  const res = await fetch(`/api/server/${activeServer}/backup`, { method: 'POST' }).catch(() => null);
  if (!res || !res.ok) {
    const data = res ? await res.json().catch(() => ({})) : {};
    logTerm(data.detail || 'Snapshot failed.', true);
    toast(data.detail || 'Snapshot failed.', 'error');
    return;
  }
  const data = await res.json().catch(() => ({}));
  logTerm(data.message || '');
  toast(data.message || 'Snapshot created.', 'ok');
  loadBackups();
}

// ---------------------------------------------------------------------------
// Automation — Cron Jobs
// ---------------------------------------------------------------------------
async function loadJobs() {
  const container = document.getElementById('job-list');
  container.innerHTML = '<div class="empty">Fetching scheduled routines…</div>';
  const res = await fetch('/api/jobs').catch(() => null);
  if (!res || !res.ok) {
    container.innerHTML = '<div class="empty err">Failed to fetch jobs.</div>'; return;
  }
  const data = await res.json().catch(() => ({}));
  container.innerHTML = (!data.jobs || !data.jobs.length)
    ? '<div class="empty">No active routines scheduled.</div>'
    : data.jobs.map(j => `
        <div class="list-row">
          <div class="row-main">
            <div>${escapeHtml(j.action.toUpperCase())} &rarr; ${escapeHtml(j.target)}</div>
            <div class="row-sub mono">${escapeHtml(j.cron)}</div>
          </div>
          <button class="mc-btn red sm" onclick="deleteJob('${escapeHtml(j.id)}')">DEL</button>
        </div>`).join('');
}

async function createJob() {
  if (!activeServer) { toast('Select a target server first.', 'error'); return; }
  const cronStr = document.getElementById('cron-str').value.trim();
  const actionStr = document.getElementById('cron-action').value;
  if (!cronStr) { toast("Provide a valid cron string (e.g., '0 4 * * *').", 'error'); return; }

  const res = await fetch('/api/jobs/create', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: activeServer, action: actionStr, cron: cronStr }),
  }).catch(() => null);

  if (!res || !res.ok) {
    const data = res ? await res.json().catch(() => ({})) : {};
    toast(data.detail || 'Failed to schedule task.', 'error'); return;
  }
  const data = await res.json().catch(() => ({}));
  logTerm(data.message || '');
  toast(data.message || 'Task scheduled.', 'ok');
  document.getElementById('cron-str').value = '';
  loadJobs();
}

async function deleteJob(jobId) {
  const res = await fetch(`/api/jobs/${jobId}`, { method: 'DELETE' }).catch(() => null);
  if (!res || !res.ok) { toast('Failed to remove task.', 'error'); return; }
  toast('Task removed.', 'ok');
  loadJobs();
}

// ---------------------------------------------------------------------------
// Shared fetch helper for the file manager and debug panels
// ---------------------------------------------------------------------------
async function apiCall(url, opts = {}) {
  const res = await fetch(url, opts).catch(() => null);
  if (!res) { toast('Cannot reach the daemon API.', 'error'); return null; }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    toast(data.detail || data.message || `Request failed (${res.status}).`, 'error');
    return null;
  }
  return data;
}

function fmtTime(epoch) {
  if (!epoch) return '—';
  return new Date(epoch * 1000).toLocaleString();
}

// ---------------------------------------------------------------------------
// File manager
//
// Rows carry their path in data attributes and the list delegates clicks, so a
// filename containing a quote or a backslash cannot break out of a handler the
// way it would with an interpolated onclick.
// ---------------------------------------------------------------------------
let fmPath = '';
let fmEntries = [];
let fmSelected = new Set();
let fmEditing = null;     // { path, mtime } of the file open in the editor
let fmDirty = false;

function fmJoin(dir, name) { return dir ? `${dir}/${name}` : name; }
function fmParent(path) {
  const i = path.lastIndexOf('/');
  return i === -1 ? '' : path.slice(0, i);
}
function fmStatus(msg, cls = '') {
  const el = document.getElementById('fm-status');
  if (el) el.innerHTML = cls ? `<span class="${cls}">${escapeHtml(msg)}</span>` : escapeHtml(msg);
}

function fmResetForServer() {
  fmPath = '';
  fmEntries = [];
  fmSelected.clear();
  // Switching target drops the editor without asking: the buffer belongs to a
  // server that is no longer the one on screen, so there is nowhere to save it.
  fmForceCloseEditor();
  const list = document.getElementById('fm-list');
  if (list) list.innerHTML = '<div class="empty">Loading…</div>';
  if (document.getElementById('panel-files').classList.contains('active')) fmReload();
}

function fmReload() { fmNavigate(fmPath); }

async function fmNavigate(path) {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const data = await apiCall(
    `/api/server/${activeServer}/files/list?path=${encodeURIComponent(path || '')}`);
  if (!data) {
    // A deleted folder should not strand the browser on a dead path.
    if (path) fmNavigate(fmParent(path));
    return;
  }
  fmPath = data.path || '';
  fmEntries = data.entries || [];
  fmSelected.clear();
  document.getElementById('fm-up-btn').disabled = !fmPath;
  fmRenderCrumbs();
  fmRenderList();
  fmStatus(`${fmEntries.length} item(s) in ${activeServer}/${fmPath || ''} — drop files on the list to upload here.`);
}

function fmUp() { if (fmPath) fmNavigate(fmParent(fmPath)); }

function fmRenderCrumbs() {
  const el = document.getElementById('fm-crumbs');
  const parts = fmPath ? fmPath.split('/') : [];
  let acc = '';
  const crumbs = [`<button class="fm-crumb" data-nav="">${escapeHtml(activeServer || '/')}</button>`];
  parts.forEach(p => {
    acc = fmJoin(acc, p);
    crumbs.push(`<span class="fm-crumb-sep">/</span>` +
      `<button class="fm-crumb" data-nav="${escapeHtml(acc)}">${escapeHtml(p)}</button>`);
  });
  el.innerHTML = crumbs.join('');
}

function fmRenderList() {
  const el = document.getElementById('fm-list');
  const term = (document.getElementById('fm-filter').value || '').toLowerCase();
  const rows = fmEntries.filter(e => !term || e.name.toLowerCase().includes(term));

  if (!rows.length) {
    el.innerHTML = `<div class="empty">${fmEntries.length ? 'Nothing matches that filter.' : 'This folder is empty.'}</div>`;
    fmSyncSelectionUI();
    return;
  }

  el.innerHTML = rows.map(e => {
    const icon = e.dir ? 'ti-folder' : (e.text ? 'ti-file-text' : 'ti-file');
    const link = e.link ? `<span class="fm-flag">${e.broken ? 'broken link' : 'link'}</span>` : '';
    const acts = [
      e.dir ? '' : `<button class="mc-btn sm" data-act="download">GET</button>`,
      (!e.dir && e.text) ? `<button class="mc-btn sm" data-act="edit">EDIT</button>` : '',
      `<button class="mc-btn sm" data-act="rename">REN</button>`,
      `<button class="mc-btn sm red" data-act="delete">DEL</button>`,
    ].join('');
    return `
      <div class="fm-row" data-path="${escapeHtml(e.path)}" data-dir="${e.dir ? '1' : '0'}">
        <span class="fm-c-check">
          <input type="checkbox" data-act="select" ${fmSelected.has(e.path) ? 'checked' : ''}>
        </span>
        <span class="fm-c-name">
          <button class="fm-name" data-act="open"><i class="ti ${icon}"></i>${escapeHtml(e.name)}</button>${link}
        </span>
        <span class="fm-c-size">${e.dir ? '—' : escapeHtml(e.human)}</span>
        <span class="fm-c-time">${escapeHtml(fmtTime(e.mtime))}</span>
        <span class="fm-c-mode mono">${escapeHtml(e.mode)}</span>
        <span class="fm-c-act">${acts}</span>
      </div>`;
  }).join('');
  fmSyncSelectionUI();
}

function fmSyncSelectionUI() {
  const del = document.getElementById('fm-del-btn');
  if (del) {
    del.disabled = fmSelected.size === 0;
    del.textContent = fmSelected.size ? `DELETE (${fmSelected.size})` : 'DELETE';
  }
  const all = document.getElementById('fm-check-all');
  if (all) all.checked = fmEntries.length > 0 && fmSelected.size === fmEntries.length;
}

function fmToggleAll(on) {
  fmSelected.clear();
  if (on) fmEntries.forEach(e => fmSelected.add(e.path));
  fmRenderList();
}

function fmEntryFor(path) { return fmEntries.find(e => e.path === path); }

document.addEventListener('DOMContentLoaded', () => {
  const list = document.getElementById('fm-list');
  if (list) {
    list.addEventListener('click', ev => {
      const hit = ev.target.closest('[data-act]');
      if (!hit) return;
      const row = hit.closest('.fm-row');
      if (!row) return;
      const path = row.dataset.path;
      const isDir = row.dataset.dir === '1';
      switch (hit.dataset.act) {
        case 'select':
          if (hit.checked) fmSelected.add(path); else fmSelected.delete(path);
          fmSyncSelectionUI();
          break;
        case 'open':
          if (isDir) fmNavigate(path);
          else if (fmEntryFor(path)?.text) fmEdit(path);
          else fmDownload(path);
          break;
        case 'edit':     fmEdit(path); break;
        case 'download': fmDownload(path); break;
        case 'rename':   fmRename(path); break;
        case 'delete':   fmDelete([path]); break;
      }
    });
  }
  const crumbs = document.getElementById('fm-crumbs');
  if (crumbs) {
    crumbs.addEventListener('click', ev => {
      const hit = ev.target.closest('[data-nav]');
      if (hit) fmNavigate(hit.dataset.nav);
    });
  }
});

function fmDownload(path) {
  window.location.href =
    `/api/server/${activeServer}/files/download?path=${encodeURIComponent(path)}`;
}

async function fmRename(path) {
  const entry = fmEntryFor(path);
  const current = entry ? entry.name : path.split('/').pop();
  const next = prompt(`Rename '${current}' to (a path with / moves it):`, current);
  if (next === null) return;
  const clean = next.trim();
  if (!clean || clean === current) return;
  // A bare name stays put; anything with a slash is treated as a path from the
  // volume root, which is how a move is expressed.
  const to = clean.includes('/') ? clean.replace(/^\/+/, '') : fmJoin(fmParent(path), clean);
  const data = await apiCall(`/api/server/${activeServer}/files/rename`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, to }),
  });
  if (!data) return;
  toast(data.message, 'ok');
  if (fmEditing && fmEditing.path === path) fmCloseEditor();
  fmReload();
}

function fmDeleteSelected() { fmDelete([...fmSelected]); }

async function fmDelete(paths) {
  if (!paths.length) return;
  const label = paths.length === 1 ? `'${paths[0]}'` : `${paths.length} items`;
  if (!confirm(`Permanently delete ${label} from '${activeServer}'? This cannot be undone.`)) return;
  const data = await apiCall(`/api/server/${activeServer}/files/delete`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths }),
  });
  if (!data) return;
  toast(data.message, data.failed && data.failed.length ? 'error' : 'ok');
  if (fmEditing && paths.includes(fmEditing.path)) fmCloseEditor();
  fmReload();
}

async function fmNewFolder() {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const name = prompt('New folder name:');
  if (!name || !name.trim()) return;
  const data = await apiCall(`/api/server/${activeServer}/files/mkdir`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: fmJoin(fmPath, name.trim()) }),
  });
  if (data) { toast(data.message, 'ok'); fmReload(); }
}

async function fmNewFile() {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const name = prompt('New file name:');
  if (!name || !name.trim()) return;
  const path = fmJoin(fmPath, name.trim());
  const data = await apiCall(`/api/server/${activeServer}/files/write`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content: '' }),
  });
  if (!data) return;
  toast(`Created ${path}.`, 'ok');
  await fmNavigate(fmPath);
  fmEdit(path);
}

// ---- Upload ---------------------------------------------------------------
function fmUploadInput(ev) {
  const files = ev.target.files;
  if (files && files.length) fmUpload(files);
  ev.target.value = '';
}

function fmHandleDrop(ev) {
  ev.preventDefault();
  document.getElementById('fm-drop').classList.remove('drag');
  const files = ev.dataTransfer && ev.dataTransfer.files;
  if (files && files.length) fmUpload(files);
}

async function fmUpload(fileList) {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const form = new FormData();
  form.append('path', fmPath);
  [...fileList].forEach(f => form.append('files', f, f.name));
  fmStatus(`Uploading ${fileList.length} file(s) to ${fmPath || '/'}…`);
  const data = await apiCall(`/api/server/${activeServer}/files/upload`,
    { method: 'POST', body: form });
  if (!data) { fmStatus('Upload failed.', 'err'); return; }
  toast(data.message, 'ok');
  fmReload();
}

// ---- Inline editor --------------------------------------------------------
async function fmEdit(path) {
  if (fmDirty && !confirm('Discard unsaved changes to the open file?')) return;
  const data = await apiCall(
    `/api/server/${activeServer}/files/read?path=${encodeURIComponent(path)}`);
  if (!data) return;
  fmEditing = { path: data.path, mtime: data.mtime };
  fmDirty = false;
  document.getElementById('fm-edit-path').textContent = data.path;
  document.getElementById('fm-editor').value = data.content;
  document.getElementById('fm-editor-card').hidden = false;
  const hint = document.getElementById('fm-edit-hint');
  hint.className = 'save-hint';
  hint.textContent = `${data.size} bytes — last modified ${fmtTime(data.mtime)}`;
  document.getElementById('fm-editor-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function fmMarkDirty() {
  if (!fmEditing || fmDirty) return;
  fmDirty = true;
  const hint = document.getElementById('fm-edit-hint');
  hint.className = 'save-hint dirty';
  hint.textContent = 'Unsaved changes';
}

function fmForceCloseEditor() {
  fmEditing = null;
  fmDirty = false;
  const card = document.getElementById('fm-editor-card');
  if (!card) return;
  card.hidden = true;
  document.getElementById('fm-editor').value = '';
}

function fmCloseEditor() {
  if (fmDirty && !confirm('Discard unsaved changes?')) return;
  fmForceCloseEditor();
}

async function fmSaveEditor() {
  if (!fmEditing) return;
  const content = document.getElementById('fm-editor').value;
  const data = await apiCall(`/api/server/${activeServer}/files/write`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: fmEditing.path, content }),
  });
  if (!data) return;
  fmDirty = false;
  fmEditing.mtime = data.mtime;
  const hint = document.getElementById('fm-edit-hint');
  hint.className = 'save-hint saved';
  hint.textContent = `Saved at ${fmtTime(data.mtime)}`;
  toast(data.message, 'ok');
  // server.properties is mirrored in two other panels — keep them honest.
  if (fmEditing.path === 'server.properties') loadPropsFromServer();
  fmNavigate(fmPath);
}

// ---------------------------------------------------------------------------
// Debug panel
// ---------------------------------------------------------------------------
let debugData = null;
let debugLogs = null;
let debugDisk = null;
let debugAutoTimer = null;
let debugProblemsOnly = false;

const DBG_LEVEL_ICON = { error: 'ti-alert-triangle', warn: 'ti-alert-circle', info: 'ti-info-circle', ok: 'ti-circle-check' };

function debugResetForServer() {
  debugData = null; debugLogs = null; debugDisk = null;
  const disk = document.getElementById('dbg-disk');
  if (disk) disk.innerHTML =
    '<div class="empty">Not measured — walking a large world takes a while, so it is on demand.</div>';
  if (document.getElementById('panel-debug').classList.contains('active')) {
    loadDebug(); loadDebugLogs();
  }
}

function renderKV(id, pairs) {
  const el = document.getElementById(id);
  if (!el) return;
  const rows = pairs.filter(([, v]) => v !== undefined && v !== null && v !== '');
  el.innerHTML = rows.length
    ? rows.map(([k, v, cls]) =>
      `<div class="kv-k">${escapeHtml(k)}</div>` +
      `<div class="kv-v ${cls || ''}">${escapeHtml(String(v))}</div>`).join('')
    : '<div class="empty">Nothing reported.</div>';
}

async function loadDebug() {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const data = await apiCall(`/api/server/${activeServer}/debug`);
  if (!data) return;
  debugData = data;
  document.getElementById('dbg-generated').textContent = `snapshot ${data.generated}`;

  // ---- Diagnosis ----------------------------------------------------------
  document.getElementById('dbg-checks').innerHTML = (data.checks || []).map(c => `
    <div class="dbg-check lvl-${escapeHtml(c.level)}">
      <i class="ti ${DBG_LEVEL_ICON[c.level] || 'ti-info-circle'}"></i>
      <div>
        <div class="dbg-check-title">${escapeHtml(c.title)}</div>
        <div class="dbg-check-detail">${escapeHtml(c.detail)}</div>
      </div>
    </div>`).join('') || '<div class="empty">No findings.</div>';

  // ---- Container ----------------------------------------------------------
  const c = data.container || {};
  const stateCls = c.running ? 'ok' : (c.exists ? 'err' : 'err');
  renderKV('dbg-container', [
    ['State', c.exists ? c.status : 'no such container', stateCls],
    ['Container ID', c.id],
    ['Image', c.image],
    ['Exit code', c.exists && !c.running ? c.exit_code : ''],
    ['Docker error', c.error, 'err'],
    ['OOM killed', c.oom_killed ? 'yes' : ''],
    ['Restarts', c.restart_count],
    ['Restart policy', c.restart_policy],
    ['PID', c.pid || ''],
    ['Created', c.created],
    ['Started', c.started_at],
    ['Finished', c.running ? '' : c.finished_at],
    ['Memory limit', c.memory_limit],
    ['CPU limit', c.cpu_limit],
    ['Compose service', c.compose_service],
    ['Health', (c.health || {}).status],
    ['Health failures', (c.health || {}).failing_streak || ''],
    ...((c.health || {}).log || []).map((h, i) => [`Health probe ${i + 1}`, `exit ${h.exit}: ${h.output}`]),
  ]);

  // ---- Ports --------------------------------------------------------------
  const ports = data.ports || [];
  document.getElementById('dbg-ports').innerHTML = ports.length ? ports.map(p => `
    <div class="list-row">
      <div class="row-main mono">${escapeHtml(p.ip)}:${escapeHtml(p.host || '—')} → ${escapeHtml(p.container)}/${escapeHtml(p.proto)}</div>
      <span class="dbg-pill ${p.published ? (p.listening ? 'ok' : 'warn') : 'off'}">
        ${p.published ? (p.listening ? 'LISTENING' : 'NO ANSWER') : 'UNPUBLISHED'}</span>
    </div>`).join('') : '<div class="empty">No ports mapped.</div>';

  // ---- Stats / processes --------------------------------------------------
  const s = data.stats || {};
  renderKV('dbg-stats', [
    ['CPU', s.cpu], ['Memory', s.mem], ['Memory %', s.mem_perc],
    ['Network I/O', s.net_io], ['Block I/O', s.block_io], ['Threads', s.pids],
  ]);
  if (!Object.keys(s).length) {
    document.getElementById('dbg-stats').innerHTML =
      '<div class="empty">Container is not running.</div>';
  }
  document.getElementById('dbg-top').textContent = data.top || '—';

  // ---- Environment / properties ------------------------------------------
  renderKV('dbg-env', Object.entries(data.env || {}));
  renderKV('dbg-props', Object.entries(data.properties || {}));

  // ---- Files --------------------------------------------------------------
  document.getElementById('dbg-files').innerHTML = (data.files || []).map(f => `
    <div class="list-row">
      <div class="row-main mono">${escapeHtml(f.path)}</div>
      <div class="row-sub">${f.exists ? `${escapeHtml(f.human || '')} · ${escapeHtml(fmtTime(f.mtime))}` : 'missing'}</div>
      <span class="dbg-pill ${f.exists ? 'ok' : 'off'}">${f.exists ? 'PRESENT' : 'ABSENT'}</span>
    </div>`).join('') || '<div class="empty">Volume not readable.</div>';

  document.getElementById('dbg-crash').innerHTML = (data.crash_reports || []).map(f => `
    <div class="list-row">
      <div class="row-main mono">${escapeHtml(f.name)}</div>
      <div class="row-sub">${escapeHtml(f.human)} · ${escapeHtml(fmtTime(f.mtime))}</div>
      <button class="mc-btn sm" data-dbgfile="crash-reports/${escapeHtml(f.name)}">VIEW</button>
    </div>`).join('') || '<div class="empty">No crash reports — good sign.</div>';

  document.getElementById('dbg-logfiles').innerHTML = (data.log_files || []).map(f => `
    <div class="list-row">
      <div class="row-main mono">${escapeHtml(f.name)}</div>
      <div class="row-sub">${escapeHtml(f.human)} · ${escapeHtml(fmtTime(f.mtime))}</div>
      <button class="mc-btn sm" data-dbgfile="logs/${escapeHtml(f.name)}">VIEW</button>
    </div>`).join('') || '<div class="empty">No log files on the volume yet.</div>';

  // ---- Host ---------------------------------------------------------------
  const h = data.host || {};
  const d = h.disk || {};
  renderKV('dbg-host', [
    ['Docker', h.docker_version],
    ['Image', `${h.image}${h.image_present ? '' : ' (NOT PULLED)'}`, h.image_present ? '' : 'err'],
    ['Volume', h.volume, h.volume_exists ? '' : 'err'],
    ['Volume exists', h.volume_exists ? 'yes' : 'no', h.volume_exists ? 'ok' : 'err'],
    ['Data dir', h.data_dir],
    ['Host CPU', h.cpu !== undefined ? `${h.cpu}%` : ''],
    ['Host RAM', h.ram !== undefined ? `${h.ram}%` : ''],
    ['Load avg', (h.load || []).join('  ')],
    ['Disk', d.total ? `${d.used} used / ${d.total} (${d.percent}%) — ${d.free} free` : ''],
    ['packwiz', h.packwiz || 'not installed'],
    ['Manager Python', h.python],
  ]);

  document.getElementById('dbg-audit').innerHTML = (data.audit || []).map(l =>
    `<div class="list-row"><div class="row-main mono">${escapeHtml(l)}</div></div>`
  ).join('') || '<div class="empty">Nothing logged for this instance yet.</div>';
}

// A crash report is just a file on the volume — hand it to the file manager
// rather than building a second viewer.
document.addEventListener('DOMContentLoaded', () => {
  ['dbg-crash', 'dbg-logfiles'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', ev => {
      const hit = ev.target.closest('[data-dbgfile]');
      if (hit) debugOpenFile(hit.dataset.dbgfile);
    });
  });
});

function debugOpenFile(path) {
  sw('files', document.querySelector('[data-panel=files]'));
  fmNavigate(fmParent(path)).then(() => fmEdit(path));
}

async function loadDebugLogs() {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const lines = document.getElementById('dbg-log-lines').value || '500';
  const box = document.getElementById('dbg-logs');
  box.textContent = 'Fetching…';
  const data = await apiCall(
    `/api/server/${activeServer}/debug/logs?lines=${encodeURIComponent(lines)}` +
    `&problems=${debugProblemsOnly}`);
  if (!data) { box.textContent = 'Log stream unavailable (does the container exist?).'; return; }
  debugLogs = data;
  box.textContent = (data.lines || []).join('\n') ||
    (debugProblemsOnly ? 'No errors or warnings in this window.' : 'No output.');
  box.scrollTop = box.scrollHeight;
  document.getElementById('dbg-log-meta').textContent =
    `${data.total} line(s) scanned · ${data.problems} flagged` +
    (data.filtered ? ' · showing flagged only' : '');
}

function toggleDebugProblems() {
  debugProblemsOnly = !debugProblemsOnly;
  const btn = document.getElementById('dbg-problems-btn');
  btn.className = debugProblemsOnly ? 'mc-btn sm orange' : 'mc-btn sm';
  btn.textContent = debugProblemsOnly ? 'SHOWING PROBLEMS' : 'PROBLEMS ONLY';
  loadDebugLogs();
}

function toggleDebugAuto() {
  const btn = document.getElementById('dbg-auto-btn');
  if (debugAutoTimer) {
    clearInterval(debugAutoTimer);
    debugAutoTimer = null;
    btn.className = 'mc-btn sm';
    btn.textContent = 'AUTO: OFF';
    return;
  }
  debugAutoTimer = setInterval(() => {
    if (!activeServer || !document.getElementById('panel-debug').classList.contains('active')) return;
    loadDebug(); loadDebugLogs();
  }, 10000);
  btn.className = 'mc-btn sm green';
  btn.textContent = 'AUTO: ON';
}

async function loadDebugDisk() {
  if (!activeServer) { toast('Select a server first.', 'error'); return; }
  const el = document.getElementById('dbg-disk');
  el.innerHTML = '<div class="empty">Measuring…</div>';
  const data = await apiCall(`/api/server/${activeServer}/debug/disk`);
  if (!data) { el.innerHTML = '<div class="empty err">Measurement failed.</div>'; return; }
  debugDisk = data;
  const max = Math.max(1, ...(data.entries || []).map(e => e.bytes));
  el.innerHTML =
    `<div class="card-hint">Volume total: <strong>${escapeHtml(data.total_human)}</strong></div>` +
    ((data.entries || []).map(e => `
      <div class="dbg-usage">
        <div class="dbg-usage-lbl">
          <span class="mono">${escapeHtml(e.name)}${e.dir ? '/' : ''}</span>
          <span>${escapeHtml(e.human)}</span>
        </div>
        <div class="h-bar-bg"><div class="h-bar bar-g" style="width:${(e.bytes / max * 100).toFixed(1)}%"></div></div>
      </div>`).join('') || '<div class="empty">Volume is empty.</div>');
}

// ---- Plain-text export ----------------------------------------------------
function copyDebugReport() {
  if (!debugData) { toast('Run a snapshot first.', 'error'); return; }
  const d = debugData;
  const L = [];
  const section = (t) => { L.push('', `== ${t} ==`); };
  const kv = (o) => Object.entries(o || {}).forEach(([k, v]) => L.push(`  ${k}: ${v}`));

  L.push(`MC Cluster Manager — debug report for '${d.server}'`, `generated ${d.generated}`);
  section('Findings');
  (d.checks || []).forEach(c => L.push(`  [${c.level.toUpperCase()}] ${c.title} — ${c.detail}`));
  section('Container');
  kv({ ...d.container, health: JSON.stringify(d.container.health) });
  section('Ports');
  (d.ports || []).forEach(p => L.push(
    `  ${p.ip}:${p.host || '-'} -> ${p.container}/${p.proto} ` +
    `[${p.published ? (p.listening ? 'listening' : 'no answer') : 'unpublished'}]`));
  section('Resources'); kv(d.stats);
  section('Environment'); kv(d.env);
  section('Properties'); kv(d.properties);
  section('Key files');
  (d.files || []).forEach(f => L.push(
    `  ${f.path}: ${f.exists ? `${f.human} @ ${fmtTime(f.mtime)}` : 'missing'}`));
  section('Crash reports');
  (d.crash_reports || []).forEach(f => L.push(`  ${f.name} (${f.human} @ ${fmtTime(f.mtime)})`));
  section('Host'); kv({ ...d.host, disk: JSON.stringify(d.host.disk), load: (d.host.load || []).join(' ') });
  section('Recent manager actions');
  (d.audit || []).forEach(l => L.push(`  ${l}`));
  if (debugDisk) {
    section(`Volume usage (total ${debugDisk.total_human})`);
    (debugDisk.entries || []).forEach(e => L.push(`  ${e.human.padStart(10)}  ${e.name}`));
  }
  section('Processes'); L.push(d.top || '  (container not running)');
  if (debugLogs) {
    section(`Log tail${debugLogs.filtered ? ' (flagged lines only)' : ''}`);
    (debugLogs.lines || []).forEach(l => L.push(`  ${l}`));
  }

  const text = L.join('\n');
  const done = () => toast('Debug report copied to the clipboard.', 'ok');
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done,
      () => toast('Clipboard blocked by the browser.', 'error'));
    return;
  }
  // The manager is usually served over plain http, where the async clipboard
  // API is unavailable.
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy') ? done() : toast('Copy failed.', 'error'); }
  catch (e) { toast('Copy failed.', 'error'); }
  document.body.removeChild(ta);
}

// ---------------------------------------------------------------------------
// Delegated click handlers for the lists added above
//
// Same reasoning as the file manager: names, filenames and revision ids ride
// in data attributes instead of being interpolated into onclick strings, so a
// value containing a quote cannot break out of its handler.
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  const on = (id, handler) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', handler);
  };

  on('player-list', handlePlayerRowClick);

  on('access-list', ev => {
    const hit = ev.target.closest('[data-act="access-remove"]');
    if (!hit) return;
    const value = hit.closest('.list-row').dataset.value;
    if (confirm(`Remove ${value} from ${accessKind}?`)) accessEdit('remove', value);
  });

  on('backup-list', ev => {
    const hit = ev.target.closest('[data-act]');
    if (!hit) return;
    const file = hit.closest('.list-row').dataset.backup;
    if (hit.dataset.act === 'restore') executeRestore(file);
    if (hit.dataset.act === 'delete-backup') deleteBackup(file);
  });

  on('prop-history', ev => {
    const hit = ev.target.closest('[data-act]');
    if (!hit) return;
    const rev = hit.closest('.list-row').dataset.rev;
    if (hit.dataset.act === 'prop-view') viewPropRevision(rev);
    if (hit.dataset.act === 'prop-rollback') rollbackProps(rev);
  });

  on('mr-results', ev => {
    const hit = ev.target.closest('[data-act="mr-add"]');
    if (!hit) return;
    modrinthAdd(hit.closest('.mr-row').dataset.slug);
  });

  on('cluster-graphs', ev => {
    const hit = ev.target.closest('.graph-name');
    if (!hit) return;
    // Jumping straight to the instance that spiked is the whole point of
    // having every server on one screen.
    const name = hit.dataset.target;
    const row = (clusterMetrics?.servers || []).find(s => s.server === name);
    setServerTarget(name, row ? row.status : 'unknown');
    sw('overview', document.querySelector('[data-panel=overview]'));
  });

  // Picking a project type by hand should stop the server's own flavour from
  // overwriting it the next time the panel opens.
  const mrType = document.getElementById('mr-type');
  if (mrType) mrType.addEventListener('change', () => { mrType.dataset.touched = '1'; });

  renderAccessTabs();
  syncModArg();
});
