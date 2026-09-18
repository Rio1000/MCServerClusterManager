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
  'instances', 'overview', 'console', 'players',
  'properties', 'world', 'upload', 'mods', 'automation'
];

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
const PASSIVE_PANELS = new Set(['properties', 'world', 'upload', 'mods', 'automation']);

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

      const pListEl = document.getElementById('player-list');
      pListEl.innerHTML = players.length === 0
        ? `<div class="empty">No players currently online.</div>`
        : players.map(p => `
            <div class="p-row">
              <div class="p-head"></div>
              <div class="p-name">${escapeHtml(p)}</div>
              <button class="mc-btn sm" onclick="showPlayerData('${escapeHtml(p)}')">NBT</button>
            </div>`).join('');
    }

    // Player NBT
    if (data.type === 'player_data') {
      document.getElementById('modal-pdata').textContent = data.data;
    }

    // Docker log lines
    if (data.type === 'docker_log') {
      logTerm(data.data);
    }

    // Console RCON response (fallback if not streamed via log)
    if (data.type === 'console_response' && data.stdout) {
      data.stdout.split('\n').forEach(line => logTerm(line));
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
function logTerm(msg, isErr = false) {
  const el = document.getElementById('console-log');
  if (!el) return;
  const d = document.createElement('div');
  d.style.color = isErr ? '#ff5555' : '#55ff55';
  d.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  el.appendChild(d);
  el.scrollTop = el.scrollHeight;
}

function clearConsole() {
  const el = document.getElementById('console-log');
  if (el) el.innerHTML = '';
}

function sendTerminalCommand() {
  const inp = document.getElementById('console-input');
  const val = inp.value.trim();
  if (!val || !activeServer) return;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method: 'docker:exec/command', target: activeServer, command: val }));
    logTerm(`> ${val}`);
    inp.value = '';
  }
}

// ---------------------------------------------------------------------------
// Panel switcher — includes scroll-to-top on every switch
// ---------------------------------------------------------------------------
function sw(id, el) {
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
  if (id === 'automation') { loadBackups(); loadJobs(); }
  if (id === 'mods') { loadInstalledAddons(); refreshPackwizStatus(); }
  if (id === 'players') { requestPlayerList(); }
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
  logTerm(`Switched target to: ${name}`);
  loadPropsFromServer();
  loadInstalledAddons();

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ method: 'docker:logs/subscribe', target: name }));
    // NEW: Tell backend to start polling container stats
    ws.send(JSON.stringify({ method: 'set_active_target', target: name }));
  }

  // Reset console and subscribe to new container logs
  document.getElementById('console-log').innerHTML = '';
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

  // Client-side port conflict check
  const listRes = await fetch('/api/servers').catch(() => null);
  if (listRes && listRes.ok) {
    const listData = await listRes.json().catch(() => ({}));
    const conflict = (listData.servers || []).find(s => s.ports && s.ports.includes(`:${port}->`));
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
    fetchNodes();
    sw('instances', document.querySelector('[data-panel=instances]'));
  }
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
  if (activeServer === name) {
    activeServer = null;
    document.getElementById('active-target').textContent = 'None';
    updateServerStatusUI('offline');
  }
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
// Replace your existing loadBackups() with this:
async function loadBackups() {
  const container = document.getElementById('backup-list');
  container.innerHTML = '<div class="empty">Fetching archives…</div>';
  const res = await fetch('/api/backups').catch(() => null);

  if (!res || !res.ok) {
    container.innerHTML = '<div class="empty err">Failed to fetch backups.</div>';
    return;
  }

  const data = await res.json().catch(() => ({}));
  container.innerHTML = (!data.backups || !data.backups.length)
    ? '<div class="empty">No archives found in the _backups directory.</div>'
    : data.backups.map(b => `
        <div class="list-row">
          <div class="row-main mono">${escapeHtml(b)}</div>
          <button class="mc-btn orange sm" onclick="executeRestore('${escapeHtml(b)}')">RESTORE</button>
        </div>`).join('');
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