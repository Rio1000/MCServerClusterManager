import asyncio
import collections
import hashlib
import hmac
import json
import os
import re
import psutil
import datetime
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager

from fastapi import (FastAPI, WebSocket, Request, UploadFile, File, Form, Path,
                     HTTPException)
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import uvicorn

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Where server directories, backups and state live. Defaults to the parent of
# this checkout, which is what an in-place install already looks like, so an
# existing deployment keeps working without being told anything.
DATA_DIR      = os.environ.get("MCSCM_DATA_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
DOCKER_IMAGE  = os.environ.get("MCSCM_DOCKER_IMAGE", "itzg/minecraft-server")
LISTEN_HOST   = os.environ.get("MCSCM_HOST", "0.0.0.0")
LISTEN_PORT   = int(os.environ.get("MCSCM_PORT", "8080"))
BACKUP_DIR    = os.path.join(DATA_DIR, "_backups")
JOBS_FILE     = os.path.join(DATA_DIR, "_jobs.json")
TEMPLATES_FILE = os.path.join(DATA_DIR, "_templates.json")
AUDIT_LOG     = os.path.join(DATA_DIR, "_audit.log")
SETTINGS_FILE = os.path.join(DATA_DIR, "_settings.json")
PROP_HISTORY_DIR = os.path.join(DATA_DIR, "_prop_history")
AUTH_FILE     = os.path.join(DATA_DIR, "_auth.json")

# Sessions live in memory only, so a restart signs everyone out. That is the
# safer default for a box that reboots rarely: nothing to steal off disk.
SESSION_TTL      = 7 * 24 * 3600
SESSION_COOKIE   = "mcscm_session"
MIN_PASSWORD_LEN = 8

# Set MCSCM_PASSWORD to provision the password without using the setup screen
# (it is only consulted while no password file exists).
BOOTSTRAP_PASSWORD = os.environ.get("MCSCM_PASSWORD", "")

# How often the background sampler records container CPU/memory, and how many
# samples it keeps per server. 15s x 720 ≈ 3 hours of history, in memory only —
# it is telemetry for the graphs, not a metrics store worth persisting.
METRICS_INTERVAL = 15
METRICS_KEEP     = 720

# The watchdog compares container state between ticks, so it needs to run often
# enough to catch a crash but not so often that it hammers the daemon.
WATCHDOG_INTERVAL = 30

# packwiz has no tagged releases upstream — its CI only publishes GitHub
# Actions artifacts, which need an authenticated API call to fetch. So the
# only official, unauthenticated way to obtain it is to build from source.
# The binary is kept beside the server data rather than installed system-wide,
# so this never needs root and never touches anything outside DATA_DIR.
PACKWIZ_MODULE  = "github.com/packwiz/packwiz@latest"
PACKWIZ_BIN_DIR = os.path.join(DATA_DIR, "_bin")
PACKWIZ_BIN     = os.path.join(PACKWIZ_BIN_DIR, "packwiz")

# packwiz knows about mod loaders, not server software; Paper/Spigot/Vanilla
# and the proxies all init with no loader.
PACKWIZ_LOADERS = {
    "FABRIC": "fabric", "FORGE": "forge", "NEOFORGE": "neoforge", "QUILT": "quilt",
}

_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$')

def validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise HTTPException(400, "Invalid name: letters, digits, hyphens, underscores (max 64).")
    return name

def safe_path(base: str, *parts: str) -> str:
    joined = os.path.realpath(os.path.join(base, *parts))
    if not joined.startswith(os.path.realpath(base)):
        raise HTTPException(400, "Path traversal detected.")
    return joined

def audit(action: str, target: str, detail: str = ""):
    """Append a line to the audit log."""
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {action} | target={target} | {detail}\n"
    try:
        with open(AUDIT_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Authentication
#
# One admin password, PBKDF2-hashed on disk, exchanged for an in-memory
# session cookie. There is no user table because there is one operator; what
# this defends against is anything that can reach the port getting arbitrary
# file writes, RCON and container control for free.
#
# Everything is stdlib: hashlib for the KDF, secrets for tokens.
# ---------------------------------------------------------------------------
PBKDF2_ITERATIONS = 240_000

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"

def _verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, want_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), int(iters))
    except (ValueError, AttributeError):
        return False
    # Constant time — a timing side channel is cheap to avoid here.
    return hmac.compare_digest(dk.hex(), want_hex)

def _load_auth() -> dict:
    if not os.path.exists(AUTH_FILE):
        return {}
    try:
        with open(AUTH_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}

def _save_auth(data: dict) -> None:
    # Written 0600 before any content lands in it, so the hash is never briefly
    # world-readable.
    fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)

def _is_configured() -> bool:
    return bool(_load_auth().get("password"))

def _provision_from_env() -> None:
    """Honour MCSCM_PASSWORD on boot, but never overwrite an existing password."""
    if not BOOTSTRAP_PASSWORD or _is_configured():
        return
    if len(BOOTSTRAP_PASSWORD) < MIN_PASSWORD_LEN:
        print(f"[auth] MCSCM_PASSWORD is shorter than {MIN_PASSWORD_LEN} characters — ignored.")
        return
    _save_auth({"password": _hash_password(BOOTSTRAP_PASSWORD), "created": time.time()})
    print("[auth] Password provisioned from MCSCM_PASSWORD.")

# --- Sessions --------------------------------------------------------------
_sessions: dict[str, float] = {}        # token -> expires_at

def _new_session() -> str:
    _prune_sessions()
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token

def _prune_sessions() -> None:
    now = time.time()
    for token in [t for t, exp in _sessions.items() if exp <= now]:
        _sessions.pop(token, None)

def _session_valid(token: str | None) -> bool:
    if not token:
        return False
    expires = _sessions.get(token)
    if expires is None:
        return False
    if expires <= time.time():
        _sessions.pop(token, None)
        return False
    return True

def _set_session_cookie(response: JSONResponse, token: str) -> None:
    # secure=False deliberately: this is served over plain http on a LAN or a
    # Tailscale address, and a Secure cookie would simply never be sent.
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL,
                        httponly=True, samesite="lax", path="/")

# --- Login throttling ------------------------------------------------------
LOGIN_MAX_FAILURES = 8
LOGIN_LOCKOUT      = 300

_login_failures: dict[str, list[float]] = {}

def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"

def _login_locked(ip: str) -> int:
    """Seconds remaining on a lockout, or 0."""
    cutoff = time.time() - LOGIN_LOCKOUT
    hits = [t for t in _login_failures.get(ip, []) if t > cutoff]
    _login_failures[ip] = hits
    if len(hits) < LOGIN_MAX_FAILURES:
        return 0
    return int(hits[0] + LOGIN_LOCKOUT - time.time()) + 1

def _record_login_failure(ip: str) -> None:
    _login_failures.setdefault(ip, []).append(time.time())

# --- The gate --------------------------------------------------------------
# Reachable signed out: the login screen and what it needs to render, plus the
# auth endpoints themselves. Everything else requires a session. The middleware
# and the endpoints are registered further down, once `app` exists.
PUBLIC_PATHS = {
    # The login screen and what it needs to render. The favicons are here so
    # the tab icon shows on the sign-in page too — redirecting an icon request
    # to /login.html would just hand the browser HTML it cannot draw.
    "/login.html", "/style.css",
    "/favicon.ico", "/favicon.svg", "/favicon.png",
    "/api/auth/status", "/api/auth/login", "/api/auth/setup",
}

# ---------------------------------------------------------------------------
# Settings
#
# One small JSON file for the knobs that used to be hard-coded: backup
# retention, the crash watchdog and where notifications go. Anything missing
# falls back to DEFAULT_SETTINGS, so an older install picks up new keys without
# being migrated.
# ---------------------------------------------------------------------------
DEFAULT_SETTINGS: dict = {
    # Backups: 0 disables that half of the policy. Both can run together —
    # a file has to survive the count rule *and* the age rule to be kept.
    # Pruning is off until it is turned on: the numbers are only a suggestion
    # until someone has looked at them, and the cost of a wrong default here is
    # a deleted archive.
    "backup_keep_count": 10,
    "backup_keep_days": 0,
    "backup_prune_enabled": False,
    # Watchdog
    "watchdog_enabled": False,
    "watchdog_restart": True,
    "watchdog_max_restarts": 3,      # per server, per window
    "watchdog_window_minutes": 60,
    # Notifications
    "webhook_url": "",
    "webhook_kind": "discord",       # discord | slack | ntfy | raw
    "notify_crash": True,
    "notify_restart": True,
    "notify_backup": False,
    "notify_state": False,           # ordinary start/stop transitions
}

_settings_lock = asyncio.Lock()

def _load_settings() -> dict:
    merged = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                merged.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
        except Exception:
            pass
    return merged

def _save_settings(settings: dict) -> None:
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

def _coerce_settings(raw: dict) -> dict:
    """Clamp incoming settings to the shape and range each key expects."""
    out = _load_settings()
    for key, default in DEFAULT_SETTINGS.items():
        if key not in raw:
            continue
        val = raw[key]
        if isinstance(default, bool):
            out[key] = bool(val)
        elif isinstance(default, int):
            try:
                out[key] = max(0, min(int(val), 100000))
            except (TypeError, ValueError):
                pass
        else:
            out[key] = str(val)[:500]
    if out["webhook_kind"] not in ("discord", "slack", "ntfy", "raw"):
        out["webhook_kind"] = "discord"
    url = out["webhook_url"].strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Webhook URL must start with http:// or https://.")
    out["webhook_url"] = url
    return out

# ---------------------------------------------------------------------------
# Notifications
#
# urllib rather than a new dependency: one POST, no retries, and every failure
# is swallowed — a dead webhook must never take down a backup or a restart.
# ---------------------------------------------------------------------------
def _notify_blocking(text: str, settings: dict | None = None) -> bool:
    s = settings or _load_settings()
    url = (s.get("webhook_url") or "").strip()
    if not url:
        return False
    kind = s.get("webhook_kind", "discord")
    if kind == "discord":
        body, ctype = json.dumps({"content": text[:1900]}).encode(), "application/json"
    elif kind == "slack":
        body, ctype = json.dumps({"text": text[:3000]}).encode(), "application/json"
    elif kind == "ntfy":
        body, ctype = text[:3000].encode(), "text/plain"
    else:
        body, ctype = json.dumps({"text": text[:3000]}).encode(), "application/json"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": ctype,
                                          "User-Agent": "MCServerClusterManager"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False

async def notify(event: str, text: str) -> None:
    """Fire a webhook if this event class is enabled. Never raises."""
    s = _load_settings()
    if event and not s.get(f"notify_{event}", False):
        return
    try:
        await asyncio.to_thread(_notify_blocking, text, s)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
scheduler  = AsyncIOScheduler()
_jobs_lock = asyncio.Lock()

psutil.cpu_percent(interval=None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(PROP_HISTORY_DIR, exist_ok=True)
    _provision_from_env()
    if not _is_configured():
        print("[auth] No password set yet — the first visit to the web UI will ask "
              "you to create one.")
    for j in _load_jobs_raw():
        try:
            scheduler.add_job(
                _execute_job, CronTrigger.from_crontab(j["cron"]),
                args=[j["action"], j["target"]], id=j["id"], replace_existing=True,
            )
        except Exception as exc:
            print(f"[startup] job {j['id']} failed: {exc}")

    # System jobs use reserved ids so a user cron job can never collide with
    # them and the delete-job endpoint cannot remove them.
    scheduler.add_job(_sample_metrics, IntervalTrigger(seconds=METRICS_INTERVAL),
                      id="_sys_metrics", replace_existing=True,
                      max_instances=1, coalesce=True)
    scheduler.add_job(_watchdog_tick, IntervalTrigger(seconds=WATCHDOG_INTERVAL),
                      id="_sys_watchdog", replace_existing=True,
                      max_instances=1, coalesce=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)

SYSTEM_JOB_IDS = {"_sys_metrics", "_sys_watchdog"}

app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Auth gate + endpoints
#
# Registered here rather than beside the helpers above because both need `app`.
# The middleware runs before every HTTP route, including the static mount, so
# index.html and script.js are behind the password too.
# ---------------------------------------------------------------------------
@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or _session_valid(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not signed in."}, status_code=401)
    # A page request: send the browser somewhere it can actually sign in.
    return RedirectResponse("/login.html", status_code=302)

@app.get("/api/auth/status")
async def auth_status(request: Request):
    return JSONResponse({
        "configured":    _is_configured(),
        "authenticated": _session_valid(request.cookies.get(SESSION_COOKIE)),
        "min_length":    MIN_PASSWORD_LEN,
    })

@app.post("/api/auth/setup")
async def auth_setup(request: Request):
    """First-run password creation. Refuses once a password exists."""
    if _is_configured():
        raise HTTPException(409, "A password is already set.")
    data = await request.json()
    password = data.get("password", "")
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    await asyncio.to_thread(_save_auth, {"password": _hash_password(password),
                                         "created": time.time()})
    audit("AUTH_SETUP", "system", f"from={_client_ip(request)}")
    resp = JSONResponse({"message": "Password set. You are signed in."})
    _set_session_cookie(resp, _new_session())
    return resp

@app.post("/api/auth/login")
async def auth_login(request: Request):
    ip = _client_ip(request)
    wait = _login_locked(ip)
    if wait:
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait}s.")
    if not _is_configured():
        raise HTTPException(409, "No password is set yet.")
    data = await request.json()
    stored = _load_auth().get("password", "")
    if not await asyncio.to_thread(_verify_password, data.get("password", ""), stored):
        _record_login_failure(ip)
        audit("AUTH_FAIL", "system", f"from={ip}")
        raise HTTPException(401, "Incorrect password.")
    _login_failures.pop(ip, None)
    audit("AUTH_LOGIN", "system", f"from={ip}")
    resp = JSONResponse({"message": "Signed in."})
    _set_session_cookie(resp, _new_session())
    return resp

@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    _sessions.pop(request.cookies.get(SESSION_COOKIE) or "", None)
    resp = JSONResponse({"message": "Signed out."})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

@app.post("/api/auth/change-password")
async def auth_change_password(request: Request):
    data = await request.json()
    current, new = data.get("current", ""), data.get("new", "")
    stored = _load_auth().get("password", "")
    if not await asyncio.to_thread(_verify_password, current, stored):
        audit("AUTH_CHANGE_FAIL", "system", f"from={_client_ip(request)}")
        raise HTTPException(401, "Current password is incorrect.")
    if len(new) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"New password must be at least {MIN_PASSWORD_LEN} characters.")
    await asyncio.to_thread(_save_auth, {"password": _hash_password(new),
                                         "created": time.time()})
    # Every other session is now stale — changing the password should kick out
    # whoever else was signed in with the old one.
    keep = request.cookies.get(SESSION_COOKIE)
    for token in [t for t in _sessions if t != keep]:
        _sessions.pop(token, None)
    audit("AUTH_CHANGE", "system", f"from={_client_ip(request)}")
    return JSONResponse({"message": "Password changed. Other sessions were signed out."})

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    if not shutil.which(cmd[0]):
        return 1, "", f"'{cmd[0]}' not found on host."
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        # The debug panel shells out to docker a lot; a hung daemon must not
        # wedge the request that asked about it.
        return 124, "", f"'{cmd[0]}' timed out after {timeout}s."
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def _get_containers() -> list[dict]:
    code, out, _ = _run([
        "docker", "ps", "-a",
        "--filter", f"ancestor={DOCKER_IMAGE}",
        "--format", "{{.Names}}|{{.Status}}|{{.Ports}}"
    ])
    if code != 0:
        return []
    servers = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 2:
            servers.append({"name": parts[0], "status": parts[1], "ports": parts[2] if len(parts) > 2 else ""})
    return servers

# ---------------------------------------------------------------------------
# Metrics history
#
# A per-server ring buffer fed by one `docker stats` call on a timer. It is
# deliberately RCON-free: the player and TPS numbers come from RCON, and
# polling those for every server on a timer is what floods the console with
# connection churn. Player counts are recorded opportunistically instead,
# whenever the UI's own poller asks for a list.
# ---------------------------------------------------------------------------
_metrics: dict[str, collections.deque] = {}

_MEM_RE = re.compile(r'^\s*([\d.]+)\s*([KMGT]?i?B)\s*/', re.I)
_MEM_UNITS = {"B": 1 / 1048576, "KIB": 1 / 1024, "MIB": 1, "GIB": 1024, "TIB": 1048576,
              "KB": 1 / 1024, "MB": 1, "GB": 1024, "TB": 1048576}

def _mem_to_mb(usage: str) -> float | None:
    """'1.523GiB / 4GiB' -> 1559.6"""
    m = _MEM_RE.match(usage or "")
    if not m:
        return None
    try:
        return round(float(m.group(1)) * _MEM_UNITS.get(m.group(2).upper(), 1), 1)
    except (TypeError, ValueError):
        return None

def _pct(raw: str) -> float | None:
    try:
        return round(float((raw or "").strip().rstrip("%")), 2)
    except (TypeError, ValueError):
        return None

def _bucket(server: str) -> collections.deque:
    if server not in _metrics:
        _metrics[server] = collections.deque(maxlen=METRICS_KEEP)
    return _metrics[server]

def _record_players(server: str, count: int) -> None:
    """Stamp a player count onto the most recent sample for this server."""
    bucket = _metrics.get(server)
    if bucket and bucket[-1].get("players") is None:
        bucket[-1]["players"] = count

async def _sample_metrics() -> None:
    managed = {c["name"] for c in await asyncio.to_thread(_get_containers)}
    if not managed:
        return
    code, out, _ = await asyncio.to_thread(_run, [
        "docker", "stats", "--no-stream", "--format",
        "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
    ], 30)
    if code != 0:
        return
    ts = int(time.time())
    seen = set()
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 4 or parts[0] not in managed:
            continue
        seen.add(parts[0])
        _bucket(parts[0]).append({
            "t": ts,
            "cpu": _pct(parts[1]),
            "mem_mb": _mem_to_mb(parts[2]),
            "mem_pct": _pct(parts[3]),
            "players": None,
        })
    # A stopped server gets an explicit null sample so its graph shows a gap
    # instead of drawing a straight line across the downtime.
    for name in managed - seen:
        if name in _metrics:
            _bucket(name).append({"t": ts, "cpu": None, "mem_mb": None,
                                  "mem_pct": None, "players": None})

# ---------------------------------------------------------------------------
# Crash watchdog
# ---------------------------------------------------------------------------
_EXIT_CODE_RE = re.compile(r'Exited\s*\((\d+)\)')

_container_prev_state: dict[str, str] = {}
_restart_history: dict[str, list[float]] = {}
# Stops the manager itself asked for. Without this the watchdog would treat
# every deliberate shutdown as a crash and start the server straight back up.
_intentional_stops: set[str] = set()

def _state_of(status: str) -> tuple[str, int | None]:
    s = (status or "").strip()
    if s.lower().startswith("up"):
        return "running", None
    m = _EXIT_CODE_RE.search(s)
    if m:
        return "exited", int(m.group(1))
    if s.lower().startswith("restarting"):
        return "restarting", None
    return "other", None

def _mark_intentional(name: str) -> None:
    _intentional_stops.add(name)

def _under_restart_limit(name: str, settings: dict) -> bool:
    window = max(1, int(settings.get("watchdog_window_minutes", 60))) * 60
    cutoff = time.time() - window
    hist = [t for t in _restart_history.get(name, []) if t > cutoff]
    _restart_history[name] = hist
    return len(hist) < max(1, int(settings.get("watchdog_max_restarts", 3)))

async def _watchdog_tick() -> None:
    try:
        containers = await asyncio.to_thread(_get_containers)
    except Exception:
        return
    settings = _load_settings()
    enabled  = bool(settings.get("watchdog_enabled"))

    for c in containers:
        name = c["name"]
        state, exit_code = _state_of(c.get("status", ""))
        prev = _container_prev_state.get(name)
        _container_prev_state[name] = state

        # State and the intentional-stop flags are tracked even while the
        # watchdog is off, so turning it on mid-flight neither mistakes the
        # first transition it sees for a crash nor trips over a stale flag.
        if prev is None or prev == state:
            continue

        if state == "running" and prev in ("exited", "restarting"):
            _intentional_stops.discard(name)
            if enabled:
                await notify("state", f"✅ `{name}` is up.")
            continue

        if state != "exited" or prev != "running":
            continue

        if name in _intentional_stops:
            _intentional_stops.discard(name)
            if enabled:
                await notify("state", f"⏹️ `{name}` stopped.")
            continue

        if not enabled:
            continue

        # Unexpected exit.
        detail = f"exit code {exit_code}" if exit_code is not None else "unknown exit"
        audit("WATCHDOG_CRASH", name, detail)
        await notify("crash", f"💥 `{name}` exited unexpectedly ({detail}).")

        if not settings.get("watchdog_restart", True):
            continue
        if not _under_restart_limit(name, settings):
            audit("WATCHDOG_GIVEUP", name, "restart limit reached")
            await notify("crash", f"🛑 `{name}` hit its restart limit — not restarting again.")
            continue

        _restart_history.setdefault(name, []).append(time.time())
        code, _, err = await asyncio.to_thread(_run, ["docker", "start", name], 60)
        if code == 0:
            audit("WATCHDOG_RESTART", name, detail)
            await notify("restart", f"🔁 `{name}` crashed ({detail}) — restarted automatically.")
        else:
            audit("WATCHDOG_RESTART_FAIL", name, err[:200])
            await notify("crash", f"⚠️ `{name}` crashed and the restart failed: {err[:200]}")

# Accepts "19132", "19132/udp", "25566:25565" or "19132:19132/udp".
_PORT_SPEC_RE = re.compile(r'^(?:(\d{1,5}):)?(\d{1,5})(?:/(tcp|udp))?$', re.I)

# docker ps renders bindings as "0.0.0.0:25565->25565/tcp, :::25565->25565/tcp"
_BOUND_PORT_RE = re.compile(r':(\d{1,5})->\d{1,5}/(tcp|udp)')


def _parse_port_spec(spec: str) -> dict:
    m = _PORT_SPEC_RE.match(str(spec).strip())
    if not m:
        raise HTTPException(
            400, f"Invalid port '{spec}'. Use PORT, PORT/udp, or HOST:CONTAINER/udp.")
    container = int(m.group(2))
    host      = int(m.group(1) or container)
    proto     = (m.group(3) or "tcp").lower()
    for value in (host, container):
        if not (1 <= value <= 65535):
            raise HTTPException(400, f"Port {value} is out of range (1-65535).")
    return {"host": host, "container": container, "proto": proto}


# Ports belonging to a per-server side service rather than the Java listener.
# Two containers can never share a host port — the kernel refuses the second
# bind — but each server can run its own copy of these on a *different* number.
# The catch is that both services announce their port to the client during the
# handshake, so the published host port and the in-container port have to be
# the same value, and the server's own config has to agree with it. Anything
# that only remaps the host side leaves clients dialling the original number
# and landing on whichever server got there first.
AUX_PORTS = {
    24454: "voicechat",   # Simple Voice Chat
    19132: "bedrock",     # Geyser / Bedrock
}


def _host_listeners() -> set[tuple[int, str]]:
    """(port, proto) already listening on the host, container or otherwise."""
    listening: set[tuple[int, str]] = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if not c.laddr:
                continue
            proto = "udp" if c.type == socket.SOCK_DGRAM else "tcp"
            # UDP sockets have no listen state; TCP only counts when listening.
            if proto == "udp" or c.status == psutil.CONN_LISTEN:
                listening.add((c.laddr.port, proto))
    except Exception:
        pass  # psutil needs privileges it may not have; docker view still applies
    return listening


def _bound_host_ports() -> set[tuple[int, str]]:
    """Every (host port, protocol) already spoken for on this host.

    Deliberately wider than the managed-container list: a deploy also fails if
    some unrelated container or a plain host process holds the port, and those
    are invisible to the ancestor-filtered container query.
    """
    bound: set[tuple[int, str]] = set()
    code, out, _ = _run(["docker", "ps", "--format", "{{.Ports}}"])
    if code == 0:
        for host, proto in _BOUND_PORT_RE.findall(out):
            bound.add((int(host), proto))
    return bound | _host_listeners()


def _next_free_port(start: int, proto: str, taken: set[tuple[int, str]]) -> int:
    for candidate in range(start, 65536):
        if (candidate, proto) not in taken:
            return candidate
    raise HTTPException(409, f"No free {proto} port at or above {start}.")


def _seed_aux_config(server_path: str, service: str, port: int) -> None:
    """Point a side service's own config at the port it will be published on.

    Written before first boot so the service picks it up on its initial load;
    both files are plain key=value and are rewritten in place if they already
    exist (the duplicate flow copies them from the source server).
    """
    if service == "voicechat":
        cfg_dir = os.path.join(server_path, "config", "voicechat")
        cfg     = os.path.join(cfg_dir, "voicechat-server.properties")
        key     = "port"
        os.makedirs(cfg_dir, exist_ok=True)
    elif service == "bedrock":
        # Geyser names its config directory after the platform it is running on
        # (Geyser-Fabric, Geyser-Spigot, Geyser-Velocity, …), so the directory
        # is discovered rather than assumed — guessing wrong silently leaves
        # Geyser on its old port while the container publishes the new one.
        cfg, key = None, None
        cfg_root = os.path.join(server_path, "config")
        if os.path.isdir(cfg_root):
            for entry in sorted(os.listdir(cfg_root)):
                candidate = os.path.join(cfg_root, entry, "config.yml")
                if entry.lower().startswith("geyser") and os.path.isfile(candidate):
                    cfg = candidate
                    break
        if not cfg:
            return
    else:
        return

    if key is None:
        # Geyser nests the port under `bedrock:`; only rewritten when the file
        # is already there, since generating a valid config from scratch is the
        # plugin's job and a stub would override more than intended.
        with open(cfg) as f:
            lines = f.readlines()
        in_bedrock = False
        for i, line in enumerate(lines):
            if re.match(r'^bedrock:\s*$', line):
                in_bedrock = True
                continue
            if in_bedrock:
                if line.strip() and not line.startswith((" ", "\t")):
                    break
                if re.match(r'^\s+port:\s', line):
                    indent = line[:len(line) - len(line.lstrip())]
                    lines[i] = f"{indent}port: {port}\n"
                    break
        with open(cfg, "w") as f:
            f.writelines(lines)
        return

    lines, replaced = [], False
    if os.path.isfile(cfg):
        with open(cfg) as f:
            for line in f:
                if re.match(rf'^\s*{key}\s*=', line):
                    lines.append(f"{key}={port}\n")
                    replaced = True
                else:
                    lines.append(line)
    if not replaced:
        lines.append(f"{key}={port}\n")
    with open(cfg, "w") as f:
        f.writelines(lines)


def _resolve_bindings(bindings: list[dict], taken: set[tuple[int, str]]) -> list[dict]:
    """Shift side-service ports off collisions; leave the Java port strict.

    The Minecraft port is what players type, so a silent move there would be
    surprising and is left to fail loudly. Voice chat and Bedrock are picked up
    from the server during the handshake, so moving those is invisible to
    players and is done automatically.
    """
    resolved: set[tuple[int, str]] = set()
    out: list[dict] = []
    for b in bindings:
        key = (b["host"], b["proto"])
        service = AUX_PORTS.get(b["container"]) if b.get("aux") else None
        if service and (key in taken or key in resolved):
            free = _next_free_port(b["container"] + 1, b["proto"], taken | resolved)
            b = {**b, "host": free, "container": free, "moved_from": b["host"],
                 "service": service}
        elif service:
            b = {**b, "service": service}
        out.append(b)
        resolved.add((b["host"], b["proto"]))
    return out


def _docker_env(name: str) -> dict[str, str]:
    code, out, _ = _run(
        ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", name])
    env: dict[str, str] = {}
    if code == 0:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _packwiz_path() -> str | None:
    """Our managed build first, then anything the admin already put on PATH."""
    if os.path.isfile(PACKWIZ_BIN) and os.access(PACKWIZ_BIN, os.X_OK):
        return PACKWIZ_BIN
    return shutil.which("packwiz")


def _packwiz_version(path: str) -> str:
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True,
                           check=False, timeout=15)
        return (r.stdout or r.stderr).strip().splitlines()[0] if r.returncode == 0 else ""
    except Exception:
        return ""


def _require_packwiz() -> str:
    path = _packwiz_path()
    if not path:
        raise HTTPException(
            503, "packwiz is not installed. Use 'Set Up Packwiz' on the Mods page first.")
    return path


async def _install_packwiz() -> str:
    """Build packwiz from source into PACKWIZ_BIN_DIR. Takes a few minutes."""
    existing = _packwiz_path()
    if existing:
        return existing
    go = shutil.which("go")
    if not go:
        raise HTTPException(
            503,
            "packwiz has no prebuilt release to download, so it is built from source "
            "and that needs the Go toolchain. Install Go 1.24+ on the host "
            "(https://go.dev/dl/) and try again.")
    os.makedirs(PACKWIZ_BIN_DIR, exist_ok=True)
    env = {**os.environ, "GOBIN": PACKWIZ_BIN_DIR}
    env.setdefault("HOME", DATA_DIR)  # go needs a writable HOME for its build cache

    def _build():
        return subprocess.run([go, "install", PACKWIZ_MODULE], env=env,
                              capture_output=True, text=True, check=False, timeout=900)
    try:
        r = await asyncio.to_thread(_build)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Building packwiz timed out after 15 minutes.")
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip()[:500] or "unknown error"
        raise HTTPException(500, f"go install failed: {detail}")
    if not os.path.isfile(PACKWIZ_BIN):
        raise HTTPException(
            500, f"go install succeeded but no binary appeared in {PACKWIZ_BIN_DIR}.")
    os.chmod(PACKWIZ_BIN, 0o755)
    return PACKWIZ_BIN


def _packwiz_init_args(name: str, env: dict[str, str]) -> list[str]:
    """Non-interactive `packwiz init` flags derived from the container's env."""
    loader = PACKWIZ_LOADERS.get(env.get("TYPE", "").upper(), "none")
    args = ["init", "-y", "--name", name, "--author", "ClusterManager",
            "--modloader", loader]
    version = env.get("VERSION", "").strip().upper()
    if version == "SNAPSHOT":
        args += ["--latest", "-s"]
    elif not version or version == "LATEST":
        args.append("--latest")
    else:
        args += ["--mc-version", env["VERSION"].strip()]
    if loader != "none":
        args.append(f"--{loader}-latest")
    return args


# `packwiz init` indexes every file under the pack root. A Minecraft server
# directory is a hostile place to do that: it holds the world (gigabytes of
# region files), logs, caches, a bundled venv and assorted symlinks. Indexing
# is slow at best, and outright fails on a symlink pointing at a directory
# ("read mc_venv/lib64: is a directory"), which is not pack content anyway.
_PACKWIZ_IGNORE = """\
# Written by ClusterManager so `packwiz refresh` only sees pack content.
# Anything the server regenerates, or that is not part of the modpack, is
# excluded — indexing it is slow and some of it breaks the index outright.
/world/
/world_*/
/logs/
/crash-reports/
/cache/
/.cache/
/.config/
/.fabric/
/.maven/
/libraries/
/versions/
/mc_venv/
/__pycache__/
/backups/
# Underscore-prefixed top-level directories are this manager's own state
# (_backups, _bin) and the convention used for kept-aside previous installs.
# None of it is pack content, and one such directory held 357 stale files.
/_*/
# Loose jars are not packwiz-tracked content — packwiz records mods as
# mods/<slug>.pw.toml metadata, and mc-update.py has --allow-loose-jars for
# anything dropped in by hand. Indexing the jars themselves just bloats it.
*.jar
*.log
session.lock
usercache.json
"""


def _seed_packwizignore(server_path: str) -> bool:
    """Write .packwizignore if absent. Returns True when one was created."""
    path = os.path.join(server_path, ".packwizignore")
    if os.path.exists(path):
        return False
    with open(path, "w") as f:
        f.write(_PACKWIZ_IGNORE)
    return True


# ---------------------------------------------------------------------------
# Resolving a packwiz pack into actual jars
#
# `packwiz modrinth add` writes a metadata file — mods/fabric-api.pw.toml —
# recording where the jar lives and what it should hash to. It never downloads
# anything. A pack is normally materialised by a second tool (packwiz-installer,
# or the itzg image when PACKWIZ_URL is set), and without that step the server
# starts with a mods folder full of .toml files it cannot load.
#
# Rather than add a Java bootstrap or require the pack be served over HTTP,
# this reads the metadata and fetches the jars directly. Every download is
# checked against the hash in the file it came from.
# ---------------------------------------------------------------------------
MOD_DOWNLOAD_MAX   = 512 * 1024 * 1024
MOD_DOWNLOAD_CHUNK = 256 * 1024

# Where a pack file is allowed to point. The hash check is what guarantees
# integrity; this is here so a hand-edited .pw.toml cannot aim the daemon at
# an arbitrary host.
MOD_DOWNLOAD_HOSTS = (
    "cdn.modrinth.com", "api.modrinth.com",
    "mediafilez.forgecdn.net", "edge.forgecdn.net", "media.forgecdn.net",
)

# Jars this manager downloaded, so a later cleanup never removes one that was
# put there by hand.
RESOLVED_INDEX = ".packwiz-resolved.json"

def _resolved_index_path(server_path: str) -> str:
    return os.path.join(server_path, RESOLVED_INDEX)

def _load_resolved(server_path: str) -> dict:
    path = _resolved_index_path(server_path)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}

def _save_resolved(server_path: str, data: dict) -> None:
    with open(_resolved_index_path(server_path), "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)

def _pw_files(server_path: str) -> list[str]:
    """Every .pw.toml in the pack, as paths relative to the server root."""
    out = []
    for root, dirs, files in os.walk(server_path):
        # The world directory can hold hundreds of thousands of region files.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("world", "world_nether", "world_the_end", "logs", "cache",
                    "libraries", "versions", "crash-reports", "backups")]
        for f in files:
            if f.endswith(".pw.toml"):
                out.append(os.path.relpath(os.path.join(root, f), server_path))
    return sorted(out)

def _parse_pw(server_path: str, rel: str) -> dict | None:
    """Read one pack file into {name, filename, side, url, hash, hash_format}."""
    import tomllib
    try:
        with open(os.path.join(server_path, rel), "rb") as f:
            doc = tomllib.load(f)
    except (OSError, ValueError):
        return None
    dl = doc.get("download") or {}
    filename = doc.get("filename") or ""
    if not filename or "/" in filename or "\\" in filename:
        return None
    return {
        "meta":        rel,
        "name":        doc.get("name") or filename,
        "filename":    filename,
        "side":        (doc.get("side") or "both").lower(),
        "url":         dl.get("url") or "",
        "hash":        (dl.get("hash") or "").lower(),
        "hash_format": (dl.get("hash-format") or "sha512").lower(),
        "dir":         os.path.dirname(rel),
    }

def _pw_state(server_path: str) -> list[dict]:
    """Every pack entry plus whether its jar is actually on disk."""
    rows = []
    for rel in _pw_files(server_path):
        entry = _parse_pw(server_path, rel)
        if not entry:
            continue
        jar = os.path.join(server_path, entry["dir"], entry["filename"])
        entry["present"] = os.path.isfile(jar)
        entry["size"] = os.path.getsize(jar) if entry["present"] else 0
        # A client-only mod on a server is not missing, it is not wanted.
        entry["wanted"] = entry["side"] in ("both", "server")
        rows.append(entry)
    return rows

def _hash_file(path: str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(MOD_DOWNLOAD_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()

def _download_mod(entry: dict, dest: str) -> str:
    """Fetch one jar to `dest`, verifying the hash. Returns '' on success."""
    url = entry["url"]
    if not url.startswith("https://"):
        return "download URL is not https"
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in MOD_DOWNLOAD_HOSTS:
        return f"refusing to download from an unexpected host ({host})"

    algo = entry["hash_format"].replace("-", "")
    if algo not in hashlib.algorithms_available:
        return f"unsupported hash format ({entry['hash_format']})"

    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "MCServerClusterManager"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            total = 0
            while True:
                chunk = resp.read(MOD_DOWNLOAD_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MOD_DOWNLOAD_MAX:
                    raise ValueError("file exceeds the size limit")
                out.write(chunk)
    except Exception as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        return f"download failed: {str(exc)[:160]}"

    if entry["hash"]:
        got = _hash_file(tmp, algo)
        if not hmac.compare_digest(got, entry["hash"]):
            os.remove(tmp)
            return f"{algo} mismatch — the file does not match what the pack expects"

    # Only now does it become the real filename, so an interrupted download
    # never leaves a half-written jar the server would try to load.
    os.replace(tmp, dest)
    return ""

def _resolve_pack(server_path: str) -> dict:
    """Download every jar the pack references but does not have."""
    resolved = _load_resolved(server_path)
    report = {"downloaded": [], "skipped": [], "failed": [], "present": 0}

    for entry in _pw_state(server_path):
        if not entry["wanted"]:
            report["skipped"].append({"name": entry["name"], "why": f"{entry['side']}-side only"})
            continue
        if entry["present"]:
            report["present"] += 1
            continue
        target_dir = os.path.join(server_path, entry["dir"])
        os.makedirs(target_dir, exist_ok=True)
        dest = os.path.join(target_dir, entry["filename"])
        err = _download_mod(entry, dest)
        if err:
            report["failed"].append({"name": entry["name"], "why": err})
            continue
        report["downloaded"].append({"name": entry["name"], "file": entry["filename"]})
        resolved[entry["meta"]] = os.path.join(entry["dir"], entry["filename"])

    _save_resolved(server_path, resolved)
    return report

def _forget_resolved(server_path: str, meta_rel: str) -> str | None:
    """Delete the jar this manager downloaded for a pack entry, if any."""
    resolved = _load_resolved(server_path)
    rel = resolved.pop(meta_rel, None)
    if not rel:
        return None
    try:
        jar = safe_path(server_path, rel)
        if os.path.isfile(jar):
            os.remove(jar)
    except (OSError, HTTPException):
        rel = None
    _save_resolved(server_path, resolved)
    return rel


def _packwiz_error(result: subprocess.CompletedProcess) -> str:
    """packwiz reports failures on stdout, so stderr alone is usually empty."""
    for stream in (result.stderr, result.stdout):
        text = (stream or "").strip()
        if text:
            # The progress bar redraws with ANSI escapes; keep the last real line.
            lines = [re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', ln).strip()
                     for ln in text.splitlines()]
            lines = [ln for ln in lines if ln and "Refreshing index" not in ln]
            if lines:
                return lines[-1][:400]
    return f"packwiz exited with status {result.returncode} and no output."


def _packwiz_init(packwiz: str, name: str, server_path: str) -> None:
    """Initialise a pack, cleaning up partial output if it fails.

    A failed init can leave an empty index.toml behind, which makes the next
    attempt look half-initialised; removing it keeps retries honest.
    """
    _seed_packwizignore(server_path)
    result = subprocess.run([packwiz] + _packwiz_init_args(name, _docker_env(name)),
                            cwd=server_path, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        for leftover in ("index.toml", "pack.toml"):
            stale = os.path.join(server_path, leftover)
            if os.path.isfile(stale) and os.path.getsize(stale) == 0:
                os.remove(stale)
        raise HTTPException(500, f"Packwiz init failed: {_packwiz_error(result)}")


def _parse_properties(server_name: str) -> dict | None:
    filepath = safe_path(DATA_DIR, server_name, "server.properties")
    if not os.path.exists(filepath):
        return None
    props: dict[str, str] = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()
    return props

def _sanitize_prop_key(k: str) -> str | None:
    if re.search(r'[=\n\r#]', k): return None
    return k.strip()[:128]

def _sanitize_prop_val(v: str) -> str:
    return re.sub(r'[\n\r]', '', str(v))[:512]

def _get_addons(server_name: str) -> dict:
    server_path = safe_path(DATA_DIR, server_name)
    addons: dict[str, list[str]] = {"mods": [], "plugins": [], "datapacks": []}
    if not os.path.exists(server_path):
        return addons
    for subdir, key in [("mods", "mods"), ("plugins", "plugins")]:
        p = os.path.join(server_path, subdir)
        if os.path.exists(p):
            addons[key] = [f for f in os.listdir(p) if f.endswith((".jar", ".disabled"))]
    props = _parse_properties(server_name)
    world_name = (props or {}).get("level-name", "world")
    dp_path = os.path.join(server_path, world_name, "datapacks")
    if os.path.exists(dp_path):
        addons["datapacks"] = os.listdir(dp_path)
    return addons

# ---------------------------------------------------------------------------
# Players — moderation over RCON
#
# Names are restricted to the Minecraft character set plus the dot Floodgate
# prefixes Bedrock players with. Nothing here reaches a shell (argv is passed
# to subprocess as a list), so the pattern exists to stop a crafted name from
# injecting extra tokens into the RCON command string.
# ---------------------------------------------------------------------------
_PLAYER_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,32}$')
_IP_RE     = re.compile(r'^[0-9a-fA-F:.]{3,45}$')

GAMEMODES  = ("survival", "creative", "adventure", "spectator")

def _validate_player(name: str) -> str:
    if not _PLAYER_RE.match(name or ""):
        raise HTTPException(400, "Invalid player name.")
    return name

def _clean_reason(text: str) -> str:
    """Free text bound for an RCON command — no newlines, bounded length."""
    return re.sub(r'[\n\r]', ' ', str(text or "")).strip()[:120]

def _player_command(action: str, player: str, arg: str) -> str:
    reason = _clean_reason(arg)
    if action == "kick":
        return f"kick {player} {reason}".strip()
    if action == "ban":
        return f"ban {player} {reason}".strip()
    if action == "pardon":
        return f"pardon {player}"
    if action == "op":
        return f"op {player}"
    if action == "deop":
        return f"deop {player}"
    if action == "whitelist_add":
        return f"whitelist add {player}"
    if action == "whitelist_remove":
        return f"whitelist remove {player}"
    if action == "kill":
        return f"kill {player}"
    if action == "gamemode":
        if reason not in GAMEMODES:
            raise HTTPException(400, f"Gamemode must be one of: {', '.join(GAMEMODES)}.")
        return f"gamemode {reason} {player}"
    raise HTTPException(400, "Unsupported player action.")

def _rcon(server: str, command: str, timeout: float = 20) -> tuple[int, str, str]:
    return _run(["docker", "exec", server, "rcon-cli", command], timeout)

def _is_running(server: str) -> bool:
    for c in _get_containers():
        if c["name"] == server:
            return _state_of(c.get("status", ""))[0] == "running"
    return False

# ---------------------------------------------------------------------------
# Access lists — whitelist / ops / bans
#
# These live as JSON in the server volume. While the server is up the edits go
# through RCON so they take effect immediately *and* the server rewrites the
# file itself; while it is down the file is the only thing there is, so it gets
# edited directly.
# ---------------------------------------------------------------------------
ACCESS_FILES = {
    "whitelist":      "whitelist.json",
    "ops":            "ops.json",
    "banned-players": "banned-players.json",
    "banned-ips":     "banned-ips.json",
}

def _read_access(server: str, kind: str) -> list[dict]:
    if kind not in ACCESS_FILES:
        raise HTTPException(400, "Unknown access list.")
    path = safe_path(DATA_DIR, server, ACCESS_FILES[kind])
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        raise HTTPException(500, f"{ACCESS_FILES[kind]} is present but unreadable.")

def _write_access(server: str, kind: str, rows: list[dict]) -> None:
    path = safe_path(DATA_DIR, server, ACCESS_FILES[kind])
    with open(path, "w") as f:
        json.dump(rows, f, indent=2)

def _offline_uuid(name: str) -> str:
    """The UUID an offline-mode server derives from a username."""
    import hashlib
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = (digest[6] & 0x0f) | 0x30      # version 3
    digest[8] = (digest[8] & 0x3f) | 0x80      # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(digest)))

def _mojang_uuid(name: str) -> str | None:
    """Resolve a premium account's UUID. Returns None if offline or unknown."""
    url = f"https://api.mojang.com/users/profiles/minecraft/{urllib.parse.quote(name)}"
    req = urllib.request.Request(url, headers={"User-Agent": "MCServerClusterManager"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return None
            raw = json.loads(resp.read().decode())
    except Exception:
        return None
    hexid = (raw or {}).get("id", "")
    if len(hexid) != 32:
        return None
    return str(uuid.UUID(hex=hexid))

def _resolve_uuid(server: str, name: str) -> str:
    """UUID for an offline edit, matching how this server assigns them."""
    props = _parse_properties(server) or {}
    if (props.get("online-mode", "true").lower() == "false"):
        return _offline_uuid(name)
    return _mojang_uuid(name) or _offline_uuid(name)

# ---------------------------------------------------------------------------
# Modrinth
#
# Proxied through the daemon rather than called from the page: it keeps the
# browser on one origin, and the API wants a descriptive User-Agent.
# ---------------------------------------------------------------------------
MODRINTH_API = "https://api.modrinth.com/v2"

def _modrinth_get(path: str, params: dict) -> dict:
    url = f"{MODRINTH_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "MCServerClusterManager (self-hosted server manager)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise HTTPException(502, f"Modrinth returned HTTP {exc.code}.")
    except urllib.error.URLError as exc:
        raise HTTPException(502, f"Could not reach Modrinth: {exc.reason}")
    except Exception:
        raise HTTPException(502, "Could not reach Modrinth.")

# ---------------------------------------------------------------------------
# Jobs / templates helpers
# ---------------------------------------------------------------------------
def _load_jobs_raw() -> list[dict]:
    if os.path.exists(JOBS_FILE):
        with open(JOBS_FILE) as f: return json.load(f)
    return []

def _save_jobs_raw(jobs: list[dict]) -> None:
    with open(JOBS_FILE, "w") as f: json.dump(jobs, f, indent=4)

def _load_templates() -> list[dict]:
    if os.path.exists(TEMPLATES_FILE):
        with open(TEMPLATES_FILE) as f: return json.load(f)
    return []

def _save_templates(templates: list[dict]) -> None:
    with open(TEMPLATES_FILE, "w") as f: json.dump(templates, f, indent=4)

async def _execute_job(action: str, target: str) -> None:
    audit("CRON", target, f"action={action}")
    if action == "backup":
        server_path = safe_path(DATA_DIR, target)
        if os.path.exists(server_path):
            ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out = os.path.join(BACKUP_DIR, f"{target}_{ts}.tar.gz")
            await asyncio.to_thread(subprocess.run, ["tar", "-czf", out, "-C", DATA_DIR, target], check=False)
            pruned = await asyncio.to_thread(_prune_backups, target)
            await notify("backup", f"💾 Scheduled snapshot of `{target}` complete"
                                   + (f" ({len(pruned)} old archive(s) pruned)." if pruned else "."))
    elif action in ("start", "stop", "restart"):
        # Tell the watchdog this one is on us, so a scheduled restart does not
        # read as a crash on the next tick.
        if action in ("stop", "restart"):
            _mark_intentional(target)
        await asyncio.to_thread(_run, ["docker", action, target])

# ---------------------------------------------------------------------------
# Backup retention
# ---------------------------------------------------------------------------
# Archives are named "<server>_<YYYYmmdd>_<HHMMSS>.tar.gz"; the server name may
# itself contain underscores, so the timestamp is matched from the right.
_BACKUP_RE = re.compile(r'^(?P<server>.+)_(?P<stamp>\d{8}_\d{6})\.tar\.gz$')

def _backup_entries() -> list[dict]:
    rows = []
    for fname in os.listdir(BACKUP_DIR):
        if not fname.endswith(".tar.gz"):
            continue
        full = os.path.join(BACKUP_DIR, fname)
        try:
            st = os.stat(full)
        except OSError:
            continue
        m = _BACKUP_RE.match(fname)
        rows.append({
            "name":   fname,
            "server": m.group("server") if m else "",
            "size":   st.st_size,
            "human":  _human(st.st_size),
            "mtime":  int(st.st_mtime),
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows

def _prune_backups(server: str | None = None, settings: dict | None = None,
                   dry_run: bool = False) -> list[str]:
    """Delete archives outside the retention policy. Returns what went (or would).

    Both rules are applied per server, not across the whole directory — one
    noisy instance backing up hourly must not evict another's only copy.
    """
    s = settings or _load_settings()
    if not s.get("backup_prune_enabled", True):
        return []
    keep_count = int(s.get("backup_keep_count", 0) or 0)
    keep_days  = int(s.get("backup_keep_days", 0) or 0)
    if keep_count <= 0 and keep_days <= 0:
        return []

    by_server: dict[str, list[dict]] = {}
    for row in _backup_entries():
        if server and row["server"] != server:
            continue
        by_server.setdefault(row["server"], []).append(row)

    cutoff = time.time() - keep_days * 86400 if keep_days > 0 else None
    removed: list[str] = []
    for rows in by_server.values():
        rows.sort(key=lambda r: r["mtime"], reverse=True)   # newest first
        for idx, row in enumerate(rows):
            too_many = keep_count > 0 and idx >= keep_count
            too_old  = cutoff is not None and row["mtime"] < cutoff
            if not (too_many or too_old):
                continue
            if dry_run:
                removed.append(row["name"])
                continue
            try:
                os.remove(os.path.join(BACKUP_DIR, row["name"]))
                removed.append(row["name"])
            except OSError:
                pass
    if removed and not dry_run:
        audit("BACKUP_PRUNE", server or "all", f"removed={len(removed)}")
    return removed

# ---------------------------------------------------------------------------
# server.properties revision history
#
# Every save writes the *previous* contents to a numbered revision first, so
# rolling back is always possible even for a change made before this existed
# (the first save after upgrading captures the original).
# ---------------------------------------------------------------------------
PROP_HISTORY_KEEP = 25

def _prop_history_dir(name: str) -> str:
    path = safe_path(PROP_HISTORY_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path

def _snapshot_properties(name: str, note: str = "") -> str | None:
    """Copy the current server.properties into the revision store."""
    current = _parse_properties(name)
    if current is None:
        return None
    hist_dir = _prop_history_dir(name)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(hist_dir, f"{stamp}.json")
    # Two saves inside the same second would collide on the filename.
    suffix = 1
    while os.path.exists(path):
        path = os.path.join(hist_dir, f"{stamp}-{suffix}.json")
        suffix += 1
    with open(path, "w") as f:
        json.dump({"saved": time.time(), "note": note, "props": current}, f, indent=2)

    # Oldest first by write time, not by name: two saves inside one second
    # differ only by a "-N" suffix, which sorts before the plain name.
    revisions = sorted(
        (f for f in os.listdir(hist_dir) if f.endswith(".json")),
        key=lambda f: os.path.getmtime(os.path.join(hist_dir, f)))
    for stale in revisions[:-PROP_HISTORY_KEEP]:
        try:
            os.remove(os.path.join(hist_dir, stale))
        except OSError:
            pass
    return os.path.basename(path)

def _load_revision(name: str, rev: str) -> dict:
    if not re.match(r'^\d{8}_\d{6}(-\d+)?\.json$', rev):
        raise HTTPException(400, "Invalid revision id.")
    path = os.path.join(_prop_history_dir(name), rev)
    if not os.path.exists(path):
        raise HTTPException(404, "Revision not found.")
    with open(path) as f:
        return json.load(f)

def _diff_props(old: dict, new: dict) -> list[dict]:
    """Key-level diff: what a rollback would actually change."""
    rows = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        rows.append({
            "key": key,
            "before": before,
            "after": after,
            "change": "added" if before is None else "removed" if after is None else "changed",
        })
    return rows

# ---------------------------------------------------------------------------
# REST — Servers
# ---------------------------------------------------------------------------
@app.get("/api/servers")
async def list_servers():
    return JSONResponse({"servers": _get_containers()})

@app.post("/api/server/create")
async def create_server(request: Request):
    data = await request.json()
    name        = validate_name(data.get("name", ""))
    server_type = data.get("type", "VANILLA").upper()
    version     = data.get("version", "LATEST")
    is_snapshot = bool(data.get("snapshot", False))
    memory      = data.get("memory", "2G")
    port        = str(data.get("port", "25565"))

    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise HTTPException(400, "Invalid port number.")
    if not re.match(r'^\d+[MmGg]$', memory):
        raise HTTPException(400, "Invalid memory value (e.g. 2G, 512M).")

    # Extra bindings for things that listen outside the Java port — Geyser's
    # Bedrock port and Simple Voice Chat are both UDP, so the protocol has to
    # travel with the number. Docker can only set these at creation time.
    extra_raw = data.get("extra_ports", [])
    if isinstance(extra_raw, str):
        extra_raw = re.split(r'[,\s]+', extra_raw.strip())
    extra_raw = [x for x in (extra_raw or []) if str(x).strip()]
    if len(extra_raw) > 20:
        raise HTTPException(400, "Too many extra ports (max 20).")

    bindings = [{"host": int(port), "container": 25565, "proto": "tcp"}]
    for x in extra_raw:
        spec = _parse_port_spec(x)
        # Only a port left at its well-known number is treated as a side
        # service; an explicit HOST:CONTAINER mapping is the admin overriding
        # placement on purpose and is honoured as written.
        spec["aux"] = (spec["host"] == spec["container"]
                       and spec["container"] in AUX_PORTS)
        bindings.append(spec)

    seen: set[tuple[int, str]] = set()
    for b in bindings:
        key = (b["host"], b["proto"])
        if key in seen:
            raise HTTPException(400, f"Port {b['host']}/{b['proto']} is listed twice.")
        seen.add(key)

    taken = _bound_host_ports()
    if (bindings[0]["host"], "tcp") in taken:
        raise HTTPException(
            409, f"Port {bindings[0]['host']}/tcp is already bound by another container.")

    bindings = _resolve_bindings(bindings, taken)
    for b in bindings:
        if not b.get("aux") and (b["host"], b["proto"]) in taken and b is not bindings[0]:
            raise HTTPException(
                409, f"Port {b['host']}/{b['proto']} is already bound by another container.")

    server_path = safe_path(DATA_DIR, name)
    os.makedirs(server_path, exist_ok=True)

    for b in bindings:
        if b.get("service"):
            _seed_aux_config(server_path, b["service"], b["host"])

    cmd = ["docker", "run", "-d", "-it", "--name", name]
    for b in bindings:
        cmd += ["-p", f"{b['host']}:{b['container']}/{b['proto']}"]
    cmd += [
        "-e", "EULA=TRUE",
        "-e", f"TYPE={server_type}",
        "-e", f"VERSION={'SNAPSHOT' if is_snapshot else version}",
        "-e", f"MEMORY={memory.upper()}",
        "-v", f"{server_path}:/data",
        DOCKER_IMAGE,
    ]
    code, _, err = _run(cmd)
    if code != 0:
        raise HTTPException(500, f"Docker error: {err}")

    bound = " ".join(f"{b['host']}:{b['container']}/{b['proto']}" for b in bindings)
    audit("DEPLOY", name,
          f"type={server_type} version={version} memory={memory} ports={bound}")

    moved = [b for b in bindings if b.get("moved_from")]
    notes = [f"{len(bindings) - 1} extra port(s) bound"] if len(bindings) > 1 else []
    notes += [f"{b['service']} moved {b['moved_from']} → {b['host']} (in use)" for b in moved]
    suffix = f" ({'; '.join(notes)})" if notes else ""
    return JSONResponse({
        "message": f"Server '{name}' deployed.{suffix}",
        "moved_ports": [
            {"service": b["service"], "from": b["moved_from"], "to": b["host"]}
            for b in moved
        ],
    })

# Regenerated on boot, tied to the old world, or pure noise — everything else
# (mods, plugins, config, jars, libraries, op/whitelist state) is carried over
# so the copy runs the same build without re-downloading anything.
_DUP_SKIP_NAMES = {
    "logs", "crash-reports", "cache", "usercache.json", "session.lock",
    ".console-history.log",
}


def _server_property(server_path: str, key: str, default: str = "") -> str:
    props = os.path.join(server_path, "server.properties")
    if os.path.isfile(props):
        try:
            with open(props) as f:
                for line in f:
                    if line.strip().startswith(f"{key}="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass
    return default


def _write_server_properties(server_path: str, updates: dict[str, str]) -> None:
    props = os.path.join(server_path, "server.properties")
    lines, seen = [], set()
    if os.path.isfile(props):
        with open(props) as f:
            for line in f:
                m = re.match(r'^\s*([\w\-.]+)\s*=', line)
                if m and m.group(1) in updates:
                    lines.append(f"{m.group(1)}={updates[m.group(1)]}\n")
                    seen.add(m.group(1))
                else:
                    lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}\n")
    with open(props, "w") as f:
        f.writelines(lines)


def _container_bindings(name: str) -> list[dict]:
    """Published bindings of an existing container, from docker inspect."""
    code, out, _ = _run([
        "docker", "inspect", "-f",
        '{{range $p, $conf := .HostConfig.PortBindings}}'
        '{{range $conf}}{{$p}}={{.HostPort}} {{end}}{{end}}', name])
    if code != 0:
        return []
    bindings = []
    for token in out.split():
        m = re.match(r'^(\d{1,5})/(tcp|udp)=(\d{1,5})$', token.strip())
        if m:
            bindings.append({"container": int(m.group(1)), "proto": m.group(2),
                             "host": int(m.group(3))})
    return bindings


@app.post("/api/server/{name}/duplicate")
async def duplicate_server(request: Request, name: str = Path(...)):
    """Clone a server's build onto a new port with a freshly generated world.

    Mods, plugins, config, the pinned jar and the world's datapacks all come
    across; the world itself does not. Datapacks are the awkward part — they
    live *inside* the world directory, so they are lifted out and replanted
    into the new world rather than being dropped with it.
    """
    validate_name(name)
    data     = await request.json()
    new_name = validate_name(data.get("new_name", ""))
    if new_name == name:
        raise HTTPException(400, "The copy needs a different name.")

    src_path = safe_path(DATA_DIR, name)
    if not os.path.isdir(src_path):
        raise HTTPException(404, f"No server directory for '{name}'.")
    dst_path = safe_path(DATA_DIR, new_name)
    if os.path.exists(dst_path):
        raise HTTPException(409, f"'{new_name}' already exists on disk.")

    code, _, _ = _run(["docker", "inspect", new_name])
    if code == 0:
        raise HTTPException(409, f"A container named '{new_name}' already exists.")

    port = str(data.get("port", "")).strip()
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise HTTPException(400, "A valid port for the copy is required.")

    src_env  = _docker_env(name)
    memory   = data.get("memory") or src_env.get("MEMORY", "2G")
    if not re.match(r'^\d+[MmGg]$', memory):
        raise HTTPException(400, "Invalid memory value (e.g. 2G, 512M).")

    level_name = str(data.get("level_name", "") or "world").strip()
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9 _\-]{0,63}$', level_name):
        raise HTTPException(400, "Invalid world name.")
    seed = str(data.get("seed", "") or "").strip()

    # Java port is strict; the side services get shifted if their number is busy.
    taken    = _bound_host_ports()
    bindings = [{"host": int(port), "container": 25565, "proto": "tcp"}]
    if (int(port), "tcp") in taken:
        raise HTTPException(409, f"Port {port}/tcp is already bound by another container.")
    for b in _container_bindings(name):
        if b["container"] == 25565 and b["proto"] == "tcp":
            continue
        bindings.append({**b, "aux": b["container"] in AUX_PORTS
                                     and b["host"] == b["container"]})
    bindings = _resolve_bindings(bindings, taken)

    src_level = _server_property(src_path, "level-name", "world")

    def _clone():
        def ignore(directory: str, entries: list[str]) -> set[str]:
            skip = set()
            for e in entries:
                full = os.path.join(directory, e)
                if e in _DUP_SKIP_NAMES:
                    skip.add(e)
                # World dirs only at the top level — a nested "world" inside a
                # mod's config is somebody else's data, not the save.
                elif (directory == src_path and os.path.isdir(full)
                      and (e == src_level or e.startswith(f"{src_level}_"))):
                    skip.add(e)
            return skip

        shutil.copytree(src_path, dst_path, ignore=ignore, symlinks=True)

        # Replant the datapacks the old world was carrying.
        src_dp = os.path.join(src_path, src_level, "datapacks")
        if os.path.isdir(src_dp):
            dst_dp = os.path.join(dst_path, level_name, "datapacks")
            os.makedirs(os.path.dirname(dst_dp), exist_ok=True)
            shutil.copytree(src_dp, dst_dp, symlinks=True)

    try:
        await asyncio.to_thread(_clone)
    except Exception as exc:
        shutil.rmtree(dst_path, ignore_errors=True)
        raise HTTPException(500, f"Copy failed: {exc}")

    updates = {"level-name": level_name, "server-port": "25565"}
    if seed:
        updates["level-seed"] = seed
    try:
        _write_server_properties(dst_path, updates)
    except Exception as exc:
        shutil.rmtree(dst_path, ignore_errors=True)
        raise HTTPException(500, f"Could not write server.properties: {exc}")

    for b in bindings:
        if b.get("service"):
            _seed_aux_config(dst_path, b["service"], b["host"])

    cmd = ["docker", "run", "-d", "-it", "--name", new_name]
    for b in bindings:
        cmd += ["-p", f"{b['host']}:{b['container']}/{b['proto']}"]
    cmd += ["-e", "EULA=TRUE"]
    for key in ("TYPE", "VERSION", "FABRIC_LOADER_VERSION", "FORGE_VERSION"):
        if src_env.get(key):
            cmd += ["-e", f"{key}={src_env[key]}"]
    cmd += [
        "-e", f"MEMORY={memory.upper()}",
        "-v", f"{dst_path}:/data",
        DOCKER_IMAGE,
    ]
    code, _, err = _run(cmd)
    if code != 0:
        shutil.rmtree(dst_path, ignore_errors=True)
        raise HTTPException(500, f"Docker error: {err}")

    bound = " ".join(f"{b['host']}:{b['container']}/{b['proto']}" for b in bindings)
    audit("DUPLICATE", new_name,
          f"source={name} type={src_env.get('TYPE', '?')} "
          f"version={src_env.get('VERSION', '?')} world={level_name} ports={bound}")

    moved = [b for b in bindings if b.get("moved_from")]
    notes = [f"{b['service']} moved {b['moved_from']} → {b['host']} (in use)" for b in moved]
    suffix = f" ({'; '.join(notes)})" if notes else ""
    return JSONResponse({
        "message": f"'{new_name}' cloned from '{name}' on port {port}.{suffix}",
        "moved_ports": [
            {"service": b["service"], "from": b["moved_from"], "to": b["host"]}
            for b in moved
        ],
    })


# ---------------------------------------------------------------------------
# REST — Updates (delegates to the existing mc-update.py / mc-chunkdiff.py)
# ---------------------------------------------------------------------------
UPDATER   = os.path.join(DATA_DIR, "mc-update.py")
CHUNKDIFF = os.path.join(DATA_DIR, "mc-chunkdiff.py")

# An update takes a borg backup, recreates the container and then watches the
# boot — minutes, not seconds. Holding the HTTP request open for that invites a
# proxy or browser timeout halfway through a container recreate, so runs happen
# in the background and the UI polls this state.
_update_runs: dict[str, dict] = {}
_update_lock = asyncio.Lock()


def _compose_service(name: str) -> str | None:
    """The compose service backing a container, if it is compose-managed."""
    code, out, _ = _run([
        "docker", "inspect", "-f",
        '{{index .Config.Labels "com.docker.compose.service"}}', name])
    svc = out.strip() if code == 0 else ""
    return svc or None


@app.get("/api/server/{name}/update/options")
async def update_options(name: str = Path(...)):
    """What this particular server can actually have updated, and why not."""
    validate_name(name)
    server_path = safe_path(DATA_DIR, name)
    if not os.path.isdir(server_path):
        raise HTTPException(404, "Container volume missing.")

    service   = _compose_service(name)
    has_pack  = os.path.isfile(os.path.join(server_path, "pack.toml"))
    env       = _docker_env(name)
    level     = _server_property(server_path, "level-name", "world")
    has_world = os.path.isdir(os.path.join(server_path, level))

    scopes = {
        "version": {
            "available": bool(service) and os.path.isfile(UPDATER),
            # mc-update.py rewrites the server's block in the compose file, so a
            # container created by `docker run` has nothing for it to edit.
            "reason": ("" if service else
                       "Not compose-managed — mc-update.py edits a compose service "
                       "block, which this container does not have.")
                      if os.path.isfile(UPDATER) else "mc-update.py not found.",
        },
        "mods": {
            "available": has_pack and bool(_packwiz_path()),
            "reason": ("" if has_pack else "No pack.toml — run Set Up Packwiz first.")
                      if _packwiz_path() else "packwiz is not installed.",
        },
        "world": {
            "available": has_world and os.path.isfile(CHUNKDIFF),
            "reason": ("" if has_world else f"No world directory ('{level}') yet.")
                      if os.path.isfile(CHUNKDIFF) else "mc-chunkdiff.py not found.",
        },
    }
    return JSONResponse({
        "server": name,
        "compose_service": service,
        "current_version": env.get("VERSION", "unknown"),
        "loader": PACKWIZ_LOADERS.get(env.get("TYPE", "").upper(), "none"),
        "level_name": level,
        "scopes": scopes,
    })


@app.post("/api/server/{name}/update/check")
async def update_check(request: Request, name: str = Path(...)):
    """Compatibility report only — never modifies the server."""
    validate_name(name)
    if not os.path.isfile(UPDATER):
        raise HTTPException(503, "mc-update.py not found next to the server data.")
    data   = await request.json() if await request.body() else {}
    target = str(data.get("target", "") or "").strip()

    cmd = [sys.executable, UPDATER, "--server", name, "--check", "--json"]
    if target:
        if not re.match(r'^[0-9][0-9A-Za-z.\-_]{0,31}$', target):
            raise HTTPException(400, "Invalid target version.")
        cmd += ["--target", target]
    else:
        cmd.append("--latest")

    def _run_check():
        return subprocess.run(cmd, cwd=DATA_DIR, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, check=False, timeout=300)
    try:
        r = await asyncio.to_thread(_run_check)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Compatibility check timed out after 5 minutes.")

    try:
        report = json.loads(r.stdout)
    except json.JSONDecodeError:
        detail = (r.stderr or r.stdout or "").strip()[-400:]
        raise HTTPException(500, f"Updater produced no report: {detail}")
    return JSONResponse(report)


def _update_argv(name: str, scope: str, opts: dict) -> list[str]:
    target = str(opts.get("target", "") or "").strip()
    if target and not re.match(r'^[0-9][0-9A-Za-z.\-_]{0,31}$', target):
        raise HTTPException(400, "Invalid target version.")

    if scope == "version":
        cmd = [sys.executable, UPDATER, "--server", name, "--update"]
        if target:
            cmd += ["--target", target]
        elif opts.get("snapshot"):
            cmd.append("--latest-snapshot")
        else:
            cmd.append("--latest")
        if opts.get("allow_loose_jars"):
            cmd.append("--allow-loose-jars")
        return cmd

    if scope == "mods":
        packwiz = _require_packwiz()
        return [packwiz, "update", "--all", "-y"]

    if scope == "world":
        # Deliberately only `plan`. mc-chunkdiff is a three-stage workflow:
        # plan reads the world and prints the recipe, diff needs a reference
        # world built separately with Chunky, and apply DELETES chunks. Only
        # the first is read-only, and the other two need a human deciding
        # between them — not a button labelled "update".
        cmd = [sys.executable, CHUNKDIFF, "plan", "--server", name]
        dim = str(opts.get("dimension", "") or "").strip()
        if dim:
            if dim not in ("overworld", "the_nether", "the_end"):
                raise HTTPException(400, "Invalid dimension.")
            cmd += ["--dim", dim]
        return cmd

    raise HTTPException(400, "Scope must be one of: version, mods, world.")


@app.post("/api/server/{name}/update/apply")
async def update_apply(request: Request, name: str = Path(...)):
    validate_name(name)
    data  = await request.json()
    scope = str(data.get("scope", "")).strip()

    server_path = safe_path(DATA_DIR, name)
    if not os.path.isdir(server_path):
        raise HTTPException(404, "Container volume missing.")

    if scope == "version" and not _compose_service(name):
        raise HTTPException(
            409, "This server is not compose-managed, so mc-update.py has no "
                 "service block to update. Mods and world scopes still work.")
    if scope == "version" and not os.path.isfile(UPDATER):
        raise HTTPException(503, "mc-update.py not found next to the server data.")
    if scope == "world" and not os.path.isfile(CHUNKDIFF):
        raise HTTPException(503, "mc-chunkdiff.py not found next to the server data.")

    argv = _update_argv(name, scope, data)
    cwd  = server_path if scope == "mods" else DATA_DIR

    async with _update_lock:
        running = _update_runs.get(name)
        if running and running.get("state") == "running":
            raise HTTPException(409, f"An update is already running for '{name}'.")
        _update_runs[name] = {
            "state": "running", "scope": scope, "output": "",
            "started": datetime.datetime.now().isoformat(timespec="seconds"),
            "returncode": None,
        }

    async def _worker():
        try:
            r = await asyncio.to_thread(
                lambda: subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                       stdin=subprocess.DEVNULL, check=False,
                                       timeout=3600))
            out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
            state = "done" if r.returncode == 0 else "failed"
            rc = r.returncode
        except subprocess.TimeoutExpired:
            out, state, rc = "Update timed out after 1 hour.", "failed", -1
        except Exception as exc:
            out, state, rc = f"Update could not start: {exc}", "failed", -1
        async with _update_lock:
            _update_runs[name].update(
                {"state": state, "output": out[-20000:], "returncode": rc,
                 "finished": datetime.datetime.now().isoformat(timespec="seconds")})
        audit("UPDATE", name, f"scope={scope} result={state} rc={rc}")

    asyncio.create_task(_worker())
    return JSONResponse({"message": f"{scope.capitalize()} update started for '{name}'.",
                         "scope": scope, "state": "running"})


@app.get("/api/server/{name}/update/status")
async def update_status(name: str = Path(...)):
    validate_name(name)
    run = _update_runs.get(name)
    if not run:
        return JSONResponse({"state": "idle"})
    return JSONResponse(run)


@app.post("/api/server/{name}/action")
async def server_action(request: Request, name: str = Path(...)):
    validate_name(name)
    data   = await request.json()
    action = data.get("action")
    if action not in ("start", "stop", "restart", "kill", "delete"):
        raise HTTPException(400, "Invalid action.")
    # Anything that takes the container down on purpose must not look like a
    # crash to the watchdog on its next tick.
    if action in ("stop", "restart", "kill", "delete"):
        _mark_intentional(name)
    cmd = ["docker", "rm", "-f", name] if action == "delete" else ["docker", action, name]
    code, _, err = _run(cmd)
    if code != 0:
        _intentional_stops.discard(name)
        raise HTTPException(500, f"Docker error: {err}")
    if action == "delete":
        _metrics.pop(name, None)
        _container_prev_state.pop(name, None)
        _restart_history.pop(name, None)
        _intentional_stops.discard(name)
    audit("ACTION", name, f"action={action}")
    return JSONResponse({"message": f"Container '{name}' → {action}."})

# ---------------------------------------------------------------------------
# REST — Container resource limits
# ---------------------------------------------------------------------------
@app.post("/api/server/{name}/resources")
async def update_resources(request: Request, name: str = Path(...)):
    validate_name(name)
    data   = await request.json()
    memory = str(data.get("memory", "")).strip()
    cpus   = str(data.get("cpus", "")).strip()

    if memory and not re.match(r'^\d+[MmGg]$', memory):
        raise HTTPException(400, "Invalid memory (e.g. 2G, 512M).")
    if cpus:
        try:
            # 0 is how docker spells "no limit", which is the only way back to
            # an uncapped container once one has been set.
            if float(cpus) < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "Invalid CPU value (e.g. 1.5, or 0 for no limit).")

    update_args: list[str] = []
    if memory:
        # --memory alone is rejected on a container whose memoryswap is still
        # unset ("Memory limit should be smaller than already set memoryswap
        # limit"), so the pair has to move together. 2x mirrors what Docker
        # itself picks at create time, which is what containers deployed
        # through this manager already have.
        size = int(memory[:-1])
        unit = memory[-1].upper()
        update_args += ["--memory", f"{size}{unit}", "--memory-swap", f"{size * 2}{unit}"]
    if cpus:
        # `--cpus 0` is accepted and silently does nothing. Writing the quota
        # directly is what actually releases the cap, and it survives a
        # restart — though HostConfig.NanoCpus stays stale afterwards, which
        # is why _effective_limits reads the cgroup instead of inspect.
        update_args += ["--cpu-quota", "-1"] if float(cpus) == 0 else ["--cpus", cpus]
    if not update_args:
        raise HTTPException(400, "Provide at least one resource limit.")

    # docker update rewrites the cgroup live — no restart, no dropped players.
    code, _, err = _run(["docker", "update"] + update_args + [name])
    if code != 0:
        raise HTTPException(500, f"docker update failed: {err}")
    audit("RESOURCES", name, f"memory={memory} cpus={cpus}")

    applied = []
    if cpus:   applied.append(f"{'no CPU limit' if float(cpus) == 0 else cpus + ' CPUs'}")
    if memory: applied.append(f"{memory.upper()} memory")
    return JSONResponse({
        "message": f"Applied to '{name}' — {', '.join(applied)}. Takes effect immediately.",
    })

def _effective_limits(name: str) -> tuple[float, int, bool]:
    """What the kernel is actually enforcing: (cpus, memory_bytes, live).

    Read from the container's own cgroup rather than `docker inspect`, because
    releasing a CPU limit leaves HostConfig.NanoCpus permanently stale — it
    keeps reporting the old cap on a container that is no longer capped, even
    across restarts. The container sees its own namespaced cgroup at
    /sys/fs/cgroup, so no host-side path guessing is needed.
    """
    code, out, _ = _run(["docker", "exec", name, "sh", "-c",
                         "cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max"], 15)
    if code != 0:
        return -1.0, -1, False          # not running; caller falls back

    lines = [l.strip() for l in out.splitlines() if l.strip()]
    cpus, mem = 0.0, 0
    if lines:
        quota, _, period = lines[0].partition(" ")
        if quota != "max":
            try:
                cpus = int(quota) / int(period or 100000)
            except (TypeError, ValueError, ZeroDivisionError):
                cpus = 0.0
    if len(lines) > 1 and lines[1] != "max":
        try:
            mem = int(lines[1])
        except ValueError:
            mem = 0
    return cpus, mem, True

@app.get("/api/server/{name}/resources")
async def get_resources(name: str = Path(...)):
    """Current container limits, alongside what the host actually has."""
    validate_name(name)
    code, out, _ = await asyncio.to_thread(_run, [
        "docker", "inspect", "-f",
        "{{.HostConfig.NanoCpus}}|{{.HostConfig.Memory}}|{{.HostConfig.CpuQuota}}"
        "|{{.HostConfig.CpuPeriod}}|{{range .Config.Env}}{{println .}}{{end}}",
        name], 20)
    if code != 0:
        raise HTTPException(404, f"'{name}' not found.")

    head = out.splitlines()[0] if out else ""
    parts = (head.split("|") + ["", "", "", ""])[:4]
    nano  = int(parts[0] or 0)
    mem   = int(parts[1] or 0)
    quota, period = int(parts[2] or 0), int(parts[3] or 0)
    # --cpus sets NanoCpus; an older --cpu-quota/--cpu-period pair means the
    # same thing and would otherwise read as unlimited.
    cpus = nano / 1e9 if nano else (quota / period if quota > 0 and period > 0 else 0)

    live_cpus, live_mem, live = await asyncio.to_thread(_effective_limits, name)
    if live:
        cpus, mem = live_cpus, live_mem

    heap = ""
    for line in out.splitlines():
        if line.startswith("MEMORY="):
            heap = line.split("=", 1)[1].strip()

    return JSONResponse({
        "cpus":          round(cpus, 2),          # 0 == no limit
        "memory_bytes":  mem,                     # 0 == no limit
        "memory_human":  _human(mem) if mem else "",
        "jvm_heap":      heap,
        "host_cpus":     psutil.cpu_count() or 0,
        "host_memory":   _human(psutil.virtual_memory().total),
        # False means the container is stopped and these are the configured
        # values rather than anything currently being enforced.
        "live":          live,
    })

@app.get("/api/server/{name}/stats")
async def container_stats(name: str = Path(...)):
    validate_name(name)
    code, out, _ = _run([
        "docker", "stats", "--no-stream", "--format",
        "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}",
        name
    ])
    if code != 0 or not out:
        raise HTTPException(503, "Container stats unavailable (is it running?).")
    parts = out.split("|")
    return JSONResponse({
        "cpu_perc":  parts[0].strip() if len(parts) > 0 else "--",
        "mem_usage": parts[1].strip() if len(parts) > 1 else "--",
        "mem_perc":  parts[2].strip() if len(parts) > 2 else "--",
        "net_io":    parts[3].strip() if len(parts) > 3 else "--",
        "block_io":  parts[4].strip() if len(parts) > 4 else "--",
    })

# ---------------------------------------------------------------------------
# REST — Properties
# ---------------------------------------------------------------------------
@app.get("/api/server/{name}/properties")
async def get_properties(name: str = Path(...)):
    validate_name(name)
    props = _parse_properties(name)
    if props is None:
        raise HTTPException(404, "server.properties not found yet.")
    return JSONResponse(props)

def _write_properties(name: str, data: dict, note: str) -> tuple[int, str | None]:
    """Snapshot the current file, then write the new one. Returns (keys, rev)."""
    filepath = safe_path(DATA_DIR, name, "server.properties")
    dir_path = os.path.dirname(filepath)
    if not os.path.exists(dir_path):
        raise HTTPException(404, "Server data directory missing.")

    rev = _snapshot_properties(name, note)

    lines = [f"# Updated via Cluster Manager\n# {datetime.datetime.now().isoformat()}\n"]
    written = 0
    for k, v in data.items():
        safe_k = _sanitize_prop_key(k)
        if safe_k is None: continue
        lines.append(f"{safe_k}={_sanitize_prop_val(v)}\n")
        written += 1

    with open(filepath, "w") as f: f.writelines(lines)
    return written, rev

@app.post("/api/server/{name}/properties/save")
async def save_properties(request: Request, name: str = Path(...)):
    validate_name(name)
    data = await request.json()
    before = _parse_properties(name) or {}
    written, rev = _write_properties(name, data, "manual save")
    changed = _diff_props(before, _parse_properties(name) or {})
    audit("PROPS_SAVE", name, f"keys={written} changed={len(changed)}")
    return JSONResponse({
        "message": "Properties saved." if changed
                   else "Properties saved — nothing actually changed.",
        "changed": changed,
        "revision": rev,
    })

@app.get("/api/server/{name}/properties/history")
async def properties_history(name: str = Path(...)):
    validate_name(name)
    hist_dir = _prop_history_dir(name)
    current  = _parse_properties(name) or {}
    rows = []
    for fname in (f for f in os.listdir(hist_dir) if f.endswith(".json")):
        try:
            with open(os.path.join(hist_dir, fname)) as f:
                rec = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        props = rec.get("props", {})
        rows.append({
            "id":      fname,
            "saved":   rec.get("saved", 0),
            "note":    rec.get("note", ""),
            "keys":    len(props),
            # How far this revision sits from what is on disk right now.
            "changes": len(_diff_props(props, current)),
        })
    # Newest first by recorded time — filenames tie on same-second saves.
    rows.sort(key=lambda r: r["saved"], reverse=True)
    return JSONResponse({"revisions": rows, "current_keys": len(current)})

@app.get("/api/server/{name}/properties/history/{rev}")
async def properties_revision(name: str = Path(...), rev: str = Path(...)):
    validate_name(name)
    rec = _load_revision(name, rev)
    current = _parse_properties(name) or {}
    # Direction reads as "what applying this revision would do to the file".
    return JSONResponse({
        "id": rev,
        "saved": rec.get("saved", 0),
        "note": rec.get("note", ""),
        "props": rec.get("props", {}),
        "diff": _diff_props(current, rec.get("props", {})),
    })

@app.post("/api/server/{name}/properties/rollback")
async def properties_rollback(request: Request, name: str = Path(...)):
    validate_name(name)
    data = await request.json()
    rev  = data.get("revision", "")
    rec  = _load_revision(name, rev)
    props = rec.get("props", {})
    if not props:
        raise HTTPException(400, "That revision holds no properties.")
    before = _parse_properties(name) or {}
    diff = _diff_props(before, props)
    # The rollback itself is snapshotted, so it can be undone in turn.
    written, new_rev = _write_properties(name, props, f"rollback to {rev}")
    audit("PROPS_ROLLBACK", name, f"rev={rev} keys={written} changed={len(diff)}")
    return JSONResponse({
        "message": f"Rolled back to {rev} — {len(diff)} key(s) changed. "
                   "Restart the server for it to take effect.",
        "changed": diff,
        "revision": new_rev,
    })

# ---------------------------------------------------------------------------
# REST — Player moderation
# ---------------------------------------------------------------------------
@app.post("/api/server/{name}/player/action")
async def player_action(request: Request, name: str = Path(...)):
    validate_name(name)
    data   = await request.json()
    player = _validate_player(data.get("player", ""))
    action = data.get("action", "")
    arg    = data.get("arg", "")

    command = _player_command(action, player, arg)
    if not await asyncio.to_thread(_is_running, name):
        raise HTTPException(409, f"'{name}' is not running — RCON commands need a live server.")

    code, out, err = await asyncio.to_thread(_rcon, name, command)
    if code != 0:
        raise HTTPException(502, f"RCON failed: {(err or out or 'no response')[:200]}")
    audit("PLAYER_ACTION", name, f"action={action} player={player}")
    return JSONResponse({
        "message": (out or f"{action} sent for {player}.").strip(),
        "command": command,
    })

# ---------------------------------------------------------------------------
# REST — Access lists (whitelist / ops / bans)
# ---------------------------------------------------------------------------
@app.get("/api/server/{name}/access/{kind}")
async def access_list(name: str = Path(...), kind: str = Path(...)):
    validate_name(name)
    rows = await asyncio.to_thread(_read_access, name, kind)
    return JSONResponse({
        "kind": kind,
        "entries": rows,
        "running": await asyncio.to_thread(_is_running, name),
        "file": ACCESS_FILES.get(kind, ""),
    })

@app.post("/api/server/{name}/access/{kind}")
async def access_edit(request: Request, name: str = Path(...), kind: str = Path(...)):
    """Add or remove one entry, live over RCON when the server is up."""
    validate_name(name)
    if kind not in ACCESS_FILES:
        raise HTTPException(400, "Unknown access list.")
    data   = await request.json()
    op     = data.get("op", "")
    target = (data.get("value", "") or "").strip()
    reason = _clean_reason(data.get("reason", ""))
    if op not in ("add", "remove"):
        raise HTTPException(400, "op must be 'add' or 'remove'.")

    if kind == "banned-ips":
        if not _IP_RE.match(target):
            raise HTTPException(400, "Invalid IP address.")
    else:
        _validate_player(target)

    running = await asyncio.to_thread(_is_running, name)

    if running:
        command = {
            ("whitelist", "add"):         f"whitelist add {target}",
            ("whitelist", "remove"):      f"whitelist remove {target}",
            ("ops", "add"):               f"op {target}",
            ("ops", "remove"):            f"deop {target}",
            ("banned-players", "add"):    f"ban {target} {reason}".strip(),
            ("banned-players", "remove"): f"pardon {target}",
            ("banned-ips", "add"):        f"ban-ip {target} {reason}".strip(),
            ("banned-ips", "remove"):     f"pardon-ip {target}",
        }[(kind, op)]
        code, out, err = await asyncio.to_thread(_rcon, name, command)
        if code != 0:
            raise HTTPException(502, f"RCON failed: {(err or out or 'no response')[:200]}")
        audit("ACCESS_EDIT", name, f"{kind} {op} {target} (rcon)")
        return JSONResponse({"message": (out or f"{op} applied.").strip(), "via": "rcon"})

    # Offline edit. Removal only needs the name; adding needs a UUID, which the
    # running server would normally resolve for us.
    rows = await asyncio.to_thread(_read_access, name, kind)
    key  = "ip" if kind == "banned-ips" else "name"
    if op == "remove":
        kept = [r for r in rows if str(r.get(key, "")).lower() != target.lower()]
        if len(kept) == len(rows):
            raise HTTPException(404, f"'{target}' is not on that list.")
        await asyncio.to_thread(_write_access, name, kind, kept)
        audit("ACCESS_EDIT", name, f"{kind} remove {target} (file)")
        return JSONResponse({"message": f"Removed '{target}' from {kind}.", "via": "file"})

    if any(str(r.get(key, "")).lower() == target.lower() for r in rows):
        raise HTTPException(409, f"'{target}' is already on that list.")

    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    if kind == "banned-ips":
        entry = {"ip": target, "created": now, "source": "Cluster Manager",
                 "expires": "forever", "reason": reason or "Banned by an operator."}
    else:
        resolved = await asyncio.to_thread(_resolve_uuid, name, target)
        entry = {"uuid": resolved, "name": target}
        if kind == "ops":
            props = _parse_properties(name) or {}
            try:
                level = int(props.get("op-permission-level", "4"))
            except ValueError:
                level = 4
            entry.update({"level": level, "bypassesPlayerLimit": False})
        elif kind == "banned-players":
            entry.update({"created": now, "source": "Cluster Manager",
                          "expires": "forever", "reason": reason or "Banned by an operator."})
    rows.append(entry)
    await asyncio.to_thread(_write_access, name, kind, rows)
    audit("ACCESS_EDIT", name, f"{kind} add {target} (file)")
    return JSONResponse({
        "message": f"Added '{target}' to {kind}. It applies next time the server starts.",
        "via": "file",
    })

# ---------------------------------------------------------------------------
# REST — Metrics history
# ---------------------------------------------------------------------------
def _series(name: str, since: int | None) -> list[dict]:
    rows = list(_metrics.get(name, ()))
    if since:
        rows = [r for r in rows if r["t"] >= since]
    return rows

@app.get("/api/metrics")
async def metrics_cluster(minutes: int = 60):
    """Every sampled server, for the cluster graphs."""
    minutes = max(1, min(minutes, 720))
    since   = int(time.time()) - minutes * 60
    known   = {c["name"]: c["status"] for c in _get_containers()}
    out = []
    for name in sorted(set(_metrics) | set(known)):
        rows = _series(name, since)
        live = [r for r in rows if r["cpu"] is not None]
        out.append({
            "server":  name,
            "status":  known.get(name, "unknown"),
            "samples": rows,
            "cpu_avg": round(sum(r["cpu"] for r in live) / len(live), 2) if live else None,
            "cpu_max": max((r["cpu"] for r in live), default=None),
            "mem_max": max((r["mem_mb"] for r in live if r["mem_mb"] is not None), default=None),
        })
    return JSONResponse({
        "interval": METRICS_INTERVAL, "minutes": minutes, "servers": out,
    })

@app.get("/api/server/{name}/metrics")
async def metrics_one(name: str = Path(...), minutes: int = 60):
    validate_name(name)
    minutes = max(1, min(minutes, 720))
    return JSONResponse({
        "server": name, "interval": METRICS_INTERVAL, "minutes": minutes,
        "samples": _series(name, int(time.time()) - minutes * 60),
    })

# ---------------------------------------------------------------------------
# REST — Settings (retention, watchdog, notifications)
# ---------------------------------------------------------------------------
@app.get("/api/settings")
async def get_settings():
    return JSONResponse(_load_settings())

@app.post("/api/settings")
async def post_settings(request: Request):
    raw = await request.json()
    if not isinstance(raw, dict):
        raise HTTPException(400, "Expected a settings object.")
    merged = _coerce_settings(raw)
    async with _settings_lock:
        await asyncio.to_thread(_save_settings, merged)
    audit("SETTINGS_SAVE", "system", f"keys={len(raw)}")
    return JSONResponse({"message": "Settings saved.", "settings": merged})

@app.post("/api/settings/test-webhook")
async def test_webhook():
    s = _load_settings()
    if not (s.get("webhook_url") or "").strip():
        raise HTTPException(400, "No webhook URL is configured.")
    ok = await asyncio.to_thread(
        _notify_blocking, "🔔 Test notification from Cluster Manager.", s)
    if not ok:
        raise HTTPException(502, "The webhook did not accept the message.")
    return JSONResponse({"message": "Test notification delivered."})

# ---------------------------------------------------------------------------
# REST — Modrinth search
# ---------------------------------------------------------------------------
# Modrinth tags a project by loader, and for the server flavours that share the
# Bukkit plugin API the tag is the flavour's own name.
_MODRINTH_LOADERS = {
    "FABRIC": "fabric", "FORGE": "forge", "NEOFORGE": "neoforge", "QUILT": "quilt",
    "PAPER": "paper", "SPIGOT": "spigot", "BUKKIT": "bukkit", "PURPUR": "purpur",
    "FOLIA": "folia", "VELOCITY": "velocity", "BUNGEECORD": "bungeecord",
}

@app.get("/api/server/{name}/runtime")
async def server_runtime(name: str = Path(...)):
    """What this instance actually runs — used to scope a Modrinth search."""
    validate_name(name)
    env = await asyncio.to_thread(_docker_env, name)
    stype = (env.get("TYPE", "") or "").upper()
    version = env.get("VERSION", "") or ""
    return JSONResponse({
        "type": stype,
        "version": version,
        "loader": _MODRINTH_LOADERS.get(stype, ""),
        # A plugin server wants plugins; a mod loader wants mods.
        "project_type": "plugin" if stype in ("PAPER", "SPIGOT", "BUKKIT", "PURPUR", "FOLIA")
                        else "mod",
    })
@app.get("/api/modrinth/search")
async def modrinth_search(q: str = "", loader: str = "", version: str = "",
                          project_type: str = "mod", limit: int = 20):
    facets: list[list[str]] = []
    if project_type in ("mod", "plugin", "datapack", "modpack", "resourcepack", "shader"):
        facets.append([f"project_type:{project_type}"])
    if loader:
        loader = loader.lower()
        if not re.match(r'^[a-z]{2,16}$', loader):
            raise HTTPException(400, "Invalid loader.")
        facets.append([f"categories:{loader}"])
    if version:
        if not re.match(r'^[\w.\-]{1,24}$', version):
            raise HTTPException(400, "Invalid game version.")
        facets.append([f"versions:{version}"])

    params = {"query": q[:120], "limit": max(1, min(limit, 50)), "index": "relevance"}
    if facets:
        params["facets"] = json.dumps(facets)

    raw = await asyncio.to_thread(_modrinth_get, "/search", params)
    hits = []
    for h in (raw.get("hits") or []):
        hits.append({
            "slug":        h.get("slug", ""),
            "title":       h.get("title", ""),
            "description": (h.get("description") or "")[:240],
            "author":      h.get("author", ""),
            "downloads":   h.get("downloads", 0),
            "icon_url":    h.get("icon_url") or "",
            "categories":  (h.get("categories") or [])[:6],
            "versions":    (h.get("versions") or [])[-6:],
            "server_side": h.get("server_side", "unknown"),
            "client_side": h.get("client_side", "unknown"),
        })
    return JSONResponse({"hits": hits, "total": raw.get("total_hits", len(hits))})

# ---------------------------------------------------------------------------
# REST — Addons
# ---------------------------------------------------------------------------
@app.get("/api/server/{name}/addons")
async def get_addons(name: str = Path(...)):
    validate_name(name)
    return JSONResponse(_get_addons(name))

@app.post("/api/server/{name}/packwiz")
async def packwiz_exec(request: Request, name: str = Path(...)):
    validate_name(name)
    data     = await request.json()
    mod_slug = data.get("mod", "").strip()
    action   = data.get("action", "add")
    if not mod_slug or action not in ("add", "remove"):
        raise HTTPException(400, "Invalid mod slug or action.")
    server_path = safe_path(DATA_DIR, name)
    if not os.path.exists(server_path):
        raise HTTPException(404, "Container volume missing.")
    packwiz = _require_packwiz()
    pack_toml = os.path.join(server_path, "pack.toml")
    if not os.path.exists(pack_toml):
        # This used to run without cwd, so it wrote pack.toml into the manager's
        # own directory and re-ran on every add. It has to run in the volume.
        _packwiz_init(packwiz, name, server_path)
    else:
        _seed_packwizignore(server_path)
    # A removal takes the .pw.toml with it, so note what the pack knew before
    # running the command — afterwards there is nothing left to read.
    before = {e["meta"]: e for e in _pw_state(server_path)} if action == "remove" else {}

    # Removal is a top-level command: `packwiz modrinth` only knows add and
    # export. Asking it to remove printed the help text and exited 0, so this
    # used to report success having done nothing at all.
    argv = [packwiz, "modrinth", "add", mod_slug, "-y"] if action == "add" \
        else [packwiz, "remove", mod_slug, "-y"]
    res = subprocess.run(argv, cwd=server_path, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL, check=False)
    if res.returncode != 0:
        raise HTTPException(500, f"Packwiz error: {_packwiz_error(res)}")
    subprocess.run([packwiz, "refresh"], cwd=server_path, capture_output=True, check=False)
    audit("PACKWIZ", name, f"action={action} mod={mod_slug}")

    if action == "remove":
        gone = [m for m in before if not os.path.exists(os.path.join(server_path, m))]
        if not gone:
            # packwiz matches on the metadata name, which is not always the
            # Modrinth slug — say so instead of claiming a success.
            raise HTTPException(404, f"Nothing in the pack matched '{mod_slug}'. "
                                     "Check the name in the pack contents list.")
        removed = [r for r in (_forget_resolved(server_path, m) for m in gone) if r]
        detail = f" Removed {len(removed)} jar(s)." if removed else \
                 " Its jar was not one this manager downloaded, so it was left alone."
        return JSONResponse({"message": f"Removed '{mod_slug}' from the pack.{detail}",
                             "removed": removed})

    # An add only writes metadata. Without this step the mods folder fills up
    # with .toml files and the server has nothing to load.
    report = await asyncio.to_thread(_resolve_pack, server_path)
    if report["failed"]:
        first = report["failed"][0]
        raise HTTPException(502, f"'{mod_slug}' was added to the pack, but downloading "
                                 f"{first['name']} failed: {first['why']}")
    got = ", ".join(d["file"] for d in report["downloaded"]) or "nothing new"
    return JSONResponse({
        "message": f"Added '{mod_slug}' and downloaded {got}.",
        "report": report,
    })

@app.post("/api/server/{name}/packwiz/sync")
async def packwiz_sync(name: str = Path(...)):
    """Download every jar the pack references but does not have.

    Exists because a pack built before the add step resolved downloads is all
    metadata and no jars — this brings it up to date without re-adding.
    """
    validate_name(name)
    server_path = safe_path(DATA_DIR, name)
    if not os.path.exists(server_path):
        raise HTTPException(404, "Container volume missing.")
    report = await asyncio.to_thread(_resolve_pack, server_path)
    audit("PACKWIZ_SYNC", name,
          f"downloaded={len(report['downloaded'])} failed={len(report['failed'])}")

    bits = []
    if report["downloaded"]: bits.append(f"downloaded {len(report['downloaded'])}")
    if report["present"]:    bits.append(f"{report['present']} already present")
    if report["skipped"]:    bits.append(f"{len(report['skipped'])} client-side skipped")
    if report["failed"]:     bits.append(f"{len(report['failed'])} failed")
    return JSONResponse({
        "message": "Pack sync: " + (", ".join(bits) if bits else "nothing to do") + ".",
        "report": report,
    })

@app.get("/api/server/{name}/packwiz/state")
async def packwiz_state(name: str = Path(...)):
    """What the pack lists, and which of it is actually on disk."""
    validate_name(name)
    server_path = safe_path(DATA_DIR, name)
    if not os.path.exists(server_path):
        raise HTTPException(404, "Container volume missing.")
    rows = await asyncio.to_thread(_pw_state, server_path)
    missing = [r for r in rows if r["wanted"] and not r["present"]]
    return JSONResponse({
        "entries": rows,
        "total": len(rows),
        "missing": len(missing),
        "has_pack": os.path.isfile(os.path.join(server_path, "pack.toml")),
    })

@app.get("/api/packwiz/status")
async def packwiz_status():
    path = _packwiz_path()
    return JSONResponse({
        "installed": bool(path),
        "path": path or "",
        "version": _packwiz_version(path) if path else "",
        "go_available": bool(shutil.which("go")),
        "install_dir": PACKWIZ_BIN_DIR,
    })


@app.post("/api/server/{name}/packwiz/setup")
async def packwiz_setup(name: str = Path(...)):
    """Install packwiz if the host lacks it, then init the pack for this server."""
    validate_name(name)
    server_path = safe_path(DATA_DIR, name)
    if not os.path.exists(server_path):
        raise HTTPException(404, "Container volume missing.")

    was_installed = bool(_packwiz_path())
    packwiz = await _install_packwiz()

    pack_toml = os.path.join(server_path, "pack.toml")
    already_init = os.path.exists(pack_toml)
    if not already_init:
        _packwiz_init(packwiz, name, server_path)
    elif _seed_packwizignore(server_path):
        # Pack predates the ignore file — add it so `refresh` stops walking the
        # world directory on every subsequent mod operation.
        subprocess.run([packwiz, "refresh"], cwd=server_path,
                       capture_output=True, check=False)

    steps = []
    steps.append("packwiz already installed" if was_installed else "packwiz built and installed")
    steps.append("pack already initialised" if already_init else "pack initialised")
    audit("PACKWIZ_SETUP", name, "; ".join(steps))
    return JSONResponse({
        "message": f"Packwiz ready for '{name}' — " + "; ".join(steps) + ".",
        "installed": True,
        "version": _packwiz_version(packwiz),
        "initialised": True,
    })


@app.post("/api/server/{name}/datapacks/upload")
async def upload_datapack(name: str = Path(...), file: UploadFile = File(...)):
    validate_name(name)
    filename = file.filename or ""
    if not filename.endswith(".zip"):
        raise HTTPException(400, "Only .zip files are accepted.")
    MAX_SIZE = 50 * 1024 * 1024
    content  = await file.read(MAX_SIZE + 1)
    if len(content) > MAX_SIZE:
        raise HTTPException(413, "File exceeds 50 MB limit.")
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        raise HTTPException(400, "Invalid filename.")
    props      = _parse_properties(name)
    world_name = (props or {}).get("level-name", "world")
    target_dir = safe_path(DATA_DIR, name, world_name, "datapacks")
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(target_dir, safe_filename), "wb") as f: f.write(content)
    audit("DATAPACK_UPLOAD", name, f"file={safe_filename}")
    return JSONResponse({"message": f"Datapack '{safe_filename}' uploaded."})

# ---------------------------------------------------------------------------
# REST — Backups (capture + list + restore)
# ---------------------------------------------------------------------------
@app.post("/api/server/{name}/backup")
async def trigger_backup(name: str = Path(...)):
    validate_name(name)
    server_path = safe_path(DATA_DIR, name)
    if not os.path.exists(server_path):
        raise HTTPException(404, "Server volume not found.")
    ts          = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_DIR, f"{name}_{ts}.tar.gz")
    code, _, err = _run(["tar", "-czf", backup_file, "-C", DATA_DIR, name])
    if code != 0:
        raise HTTPException(500, f"tar failed: {err}")
    pruned = await asyncio.to_thread(_prune_backups, name)
    audit("BACKUP", name, f"file={os.path.basename(backup_file)}")
    await notify("backup", f"💾 Snapshot of `{name}` captured.")
    msg = f"Snapshot created: {os.path.basename(backup_file)}"
    if pruned:
        msg += f" — {len(pruned)} old archive(s) pruned."
    return JSONResponse({"message": msg, "pruned": pruned})

@app.get("/api/backups")
async def list_backups():
    rows  = _backup_entries()
    total = sum(r["size"] for r in rows)
    return JSONResponse({
        # Plain filenames kept for anything still reading the old shape.
        "backups": [r["name"] for r in rows],
        "entries": rows,
        "total": total,
        "total_human": _human(total),
        "settings": {k: _load_settings()[k] for k in
                     ("backup_keep_count", "backup_keep_days", "backup_prune_enabled")},
    })

@app.delete("/api/backups/{filename}")
async def delete_backup(filename: str = Path(...)):
    if not filename.endswith(".tar.gz") or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid backup filename.")
    path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Backup not found.")
    try:
        os.remove(path)
    except OSError as exc:
        raise HTTPException(500, f"Could not delete: {exc}")
    audit("BACKUP_DELETE", "system", f"file={filename}")
    return JSONResponse({"message": f"Deleted {filename}."})

@app.post("/api/backups/prune")
async def prune_backups(request: Request):
    data    = await request.json() if await request.body() else {}
    dry_run = bool(data.get("dry_run", False))
    removed = await asyncio.to_thread(_prune_backups, data.get("server") or None, None, dry_run)
    if not removed:
        return JSONResponse({"message": "Nothing to prune — everything is inside the policy.",
                             "removed": []})
    verb = "would be removed" if dry_run else "removed"
    return JSONResponse({"message": f"{len(removed)} archive(s) {verb}.", "removed": removed})

@app.post("/api/server/{name}/restore")
async def restore_backup(request: Request, name: str = Path(...)):
    validate_name(name)
    data     = await request.json()
    filename = data.get("filename", "")
    # Validate filename: must end in .tar.gz and live inside BACKUP_DIR
    if not filename.endswith(".tar.gz") or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid backup filename.")
    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        raise HTTPException(404, f"Backup '{filename}' not found.")

    server_path = safe_path(DATA_DIR, name)

    # Stop the container first
    _run(["docker", "stop", name])

    # Remove existing data dir and re-extract
    shutil.rmtree(server_path, ignore_errors=True)
    code, _, err = _run(["tar", "-xzf", backup_path, "-C", DATA_DIR])
    if code != 0:
        raise HTTPException(500, f"Restore failed: {err}")

    # Restart the container
    _run(["docker", "start", name])
    audit("RESTORE", name, f"file={filename}")
    return JSONResponse({"message": f"Restored '{filename}' to '{name}' and restarted."})

# ---------------------------------------------------------------------------
# REST — Audit log
# ---------------------------------------------------------------------------
@app.get("/api/audit")
async def get_audit(limit: int = 100):
    if not os.path.exists(AUDIT_LOG):
        return JSONResponse({"entries": []})
    with open(AUDIT_LOG) as f:
        lines = f.readlines()
    entries = [l.rstrip() for l in lines[-limit:] if l.strip()]
    entries.reverse()   # newest first
    return JSONResponse({"entries": entries})

# ---------------------------------------------------------------------------
# REST — Templates
# ---------------------------------------------------------------------------
@app.get("/api/templates")
async def list_templates():
    return JSONResponse({"templates": _load_templates()})

@app.post("/api/templates/save")
async def save_template(request: Request):
    data = await request.json()
    label = data.get("label", "").strip()
    if not label:
        raise HTTPException(400, "Template label is required.")
    template = {
        "id":      str(uuid.uuid4())[:8],
        "label":   label[:64],
        "type":    data.get("type",    "VANILLA"),
        "version": data.get("version", "LATEST"),
        "memory":  data.get("memory",  "2G"),
    }
    templates = _load_templates()
    templates.append(template)
    _save_templates(templates)
    audit("TEMPLATE_SAVE", "system", f"label={label}")
    return JSONResponse({"message": f"Template '{label}' saved.", "template": template})

@app.delete("/api/templates/{tid}")
async def delete_template(tid: str = Path(...)):
    templates = [t for t in _load_templates() if t["id"] != tid]
    _save_templates(templates)
    return JSONResponse({"message": "Template deleted."})

# ---------------------------------------------------------------------------
# REST — Jobs
# ---------------------------------------------------------------------------
@app.get("/api/jobs")
async def list_jobs():
    async with _jobs_lock:
        return JSONResponse({"jobs": _load_jobs_raw()})

@app.post("/api/jobs/create")
async def create_job(request: Request):
    data   = await request.json()
    cron   = data.get("cron", "").strip()
    action = data.get("action", "")
    target = data.get("target", "")
    validate_name(target)
    if action not in ("backup", "start", "stop", "restart"):
        raise HTTPException(400, "Invalid action.")
    job_id = str(uuid.uuid4())[:8]
    try:
        scheduler.add_job(_execute_job, CronTrigger.from_crontab(cron),
                          args=[action, target], id=job_id)
    except Exception as exc:
        raise HTTPException(400, f"Invalid cron syntax: {exc}")
    async with _jobs_lock:
        jobs = _load_jobs_raw()
        jobs.append({"id": job_id, "target": target, "action": action, "cron": cron})
        _save_jobs_raw(jobs)
    audit("JOB_CREATE", target, f"id={job_id} action={action} cron={cron}")
    return JSONResponse({"message": "Task scheduled.", "id": job_id})

@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str = Path(...)):
    if job_id in SYSTEM_JOB_IDS:
        raise HTTPException(400, "That is an internal job and cannot be removed.")
    async with _jobs_lock:
        jobs = [j for j in _load_jobs_raw() if j["id"] != job_id]
        _save_jobs_raw(jobs)
    try: scheduler.remove_job(job_id)
    except Exception: pass
    return JSONResponse({"message": "Task removed."})

# ---------------------------------------------------------------------------
# File manager helpers
#
# Every path below is resolved with realpath and then re-checked against the
# server's own root, so neither "../" nor a symlink planted inside the volume
# can reach anything else on the host. safe_path() is deliberately not reused:
# its prefix test also accepts a sibling directory whose name merely starts
# with the server's own, which is harmless for the fixed filenames it guards
# but not for arbitrary user input.
# ---------------------------------------------------------------------------
EDIT_MAX_BYTES   = 2  * 1024 * 1024     # biggest file the inline editor will open
UPLOAD_MAX_BYTES = 512 * 1024 * 1024    # per uploaded file
UPLOAD_CHUNK     = 1024 * 1024

# Suffixes the browser marks as editable. The read endpoint still sniffs for
# NUL bytes, so this only decides which rows get an EDIT button.
_TEXT_EXTS = {
    ".properties", ".txt", ".yml", ".yaml", ".json", ".json5", ".toml", ".cfg",
    ".conf", ".config", ".ini", ".log", ".md", ".mcmeta", ".mcfunction", ".sh",
    ".env", ".xml", ".csv", ".tsv", ".lang", ".snbt", ".list", ".lock", ".hjson",
}
_TEXT_FILES = {
    "eula.txt", "dockerfile", "makefile", ".packwizignore", ".gitignore",
    "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json",
}

def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def _server_root(name: str) -> str:
    validate_name(name)
    root = os.path.realpath(os.path.join(DATA_DIR, name))
    if not os.path.isdir(root):
        raise HTTPException(404, "Container volume missing.")
    return root

def _resolve_in(root: str, rel: str) -> str:
    """Resolve a caller-supplied relative path inside `root`."""
    if not isinstance(rel, str):
        raise HTTPException(400, "Path must be a string.")
    if "\0" in rel:
        raise HTTPException(400, "Invalid path.")
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if rel in ("", "."):
        return root
    target = os.path.realpath(os.path.join(root, rel))
    if target != root and not target.startswith(root + os.sep):
        raise HTTPException(400, "Path escapes the server volume.")
    return target

def _rel_to(root: str, target: str) -> str:
    return "" if target == root else os.path.relpath(target, root)

def _is_text_name(fname: str) -> bool:
    low = fname.lower()
    return low in _TEXT_FILES or os.path.splitext(low)[1] in _TEXT_EXTS

def _fs_entry(root: str, full: str) -> dict:
    st      = os.lstat(full)
    is_link = stat.S_ISLNK(st.st_mode)
    broken  = False
    if is_link:
        try:
            st = os.stat(full)
        except OSError:
            broken = True
    is_dir = stat.S_ISDIR(st.st_mode)
    fname  = os.path.basename(full)
    return {
        "name":  fname,
        "path":  _rel_to(root, full),
        "dir":   is_dir,
        "link":  is_link,
        "broken": broken,
        "size":  0 if is_dir else st.st_size,
        "human": "" if is_dir else _human(st.st_size),
        "mtime": int(st.st_mtime),
        "mode":  stat.filemode(st.st_mode),
        "text":  (not is_dir) and (not broken) and _is_text_name(fname),
    }

async def _save_upload(upload: UploadFile, dest: str) -> int:
    """Stream an upload to disk, aborting if it runs past the size cap."""
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        413, f"'{os.path.basename(dest)}' exceeds the "
                             f"{_human(UPLOAD_MAX_BYTES)} upload limit.")
                out.write(chunk)
    except BaseException:
        # A partial file is worse than none — the user would not know it is short.
        try: os.unlink(dest)
        except OSError: pass
        raise
    return written

# ---------------------------------------------------------------------------
# REST — File manager
# ---------------------------------------------------------------------------
@app.get("/api/server/{name}/files/list")
async def files_list(name: str = Path(...), path: str = ""):
    root   = _server_root(name)
    target = _resolve_in(root, path)
    if not os.path.isdir(target):
        raise HTTPException(404, "Directory not found.")
    entries = []
    try:
        with os.scandir(target) as it:
            for de in it:
                try:
                    entries.append(_fs_entry(root, de.path))
                except OSError:
                    continue
    except PermissionError:
        raise HTTPException(403, "Permission denied reading that directory.")
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return JSONResponse({
        "server":  name,
        "root":    root,
        "path":    _rel_to(root, target),
        "parent":  None if target == root else _rel_to(root, os.path.dirname(target)),
        "entries": entries,
    })

@app.get("/api/server/{name}/files/read")
async def files_read(name: str = Path(...), path: str = ""):
    root   = _server_root(name)
    target = _resolve_in(root, path)
    if not os.path.isfile(target):
        raise HTTPException(404, "File not found.")
    size = os.path.getsize(target)
    if size > EDIT_MAX_BYTES:
        raise HTTPException(413, f"{_human(size)} is past the "
                                 f"{_human(EDIT_MAX_BYTES)} editor limit — download it instead.")
    with open(target, "rb") as f:
        raw = f.read()
    if b"\0" in raw[:8192]:
        raise HTTPException(415, "Binary file — download it instead.")
    return JSONResponse({
        "path":    _rel_to(root, target),
        "content": raw.decode("utf-8", errors="replace"),
        "size":    size,
        "mtime":   int(os.path.getmtime(target)),
    })

@app.post("/api/server/{name}/files/write")
async def files_write(request: Request, name: str = Path(...)):
    root    = _server_root(name)
    data    = await request.json()
    target  = _resolve_in(root, data.get("path", ""))
    content = data.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(400, "Content must be text.")
    if target == root or os.path.isdir(target):
        raise HTTPException(400, "Target is a directory.")
    if len(content.encode("utf-8")) > EDIT_MAX_BYTES:
        raise HTTPException(413, f"Content exceeds {_human(EDIT_MAX_BYTES)}.")
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise HTTPException(404, "Parent directory does not exist.")
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    audit("FILE_WRITE", name, f"path={_rel_to(root, target)} bytes={len(content)}")
    return JSONResponse({"message": f"Saved {os.path.basename(target)}.",
                         "mtime": int(os.path.getmtime(target))})

@app.post("/api/server/{name}/files/mkdir")
async def files_mkdir(request: Request, name: str = Path(...)):
    root   = _server_root(name)
    data   = await request.json()
    target = _resolve_in(root, data.get("path", ""))
    if target == root:
        raise HTTPException(400, "A folder name is required.")
    if os.path.exists(target):
        raise HTTPException(409, "That name is already taken.")
    os.makedirs(target)
    audit("FILE_MKDIR", name, f"path={_rel_to(root, target)}")
    return JSONResponse({"message": f"Created {_rel_to(root, target)}/."})

@app.post("/api/server/{name}/files/rename")
async def files_rename(request: Request, name: str = Path(...)):
    """Rename or move. `to` is a path relative to the volume root, so the same
    call covers an in-place rename and a drop into another directory."""
    root = _server_root(name)
    data = await request.json()
    src  = _resolve_in(root, data.get("path", ""))
    dst  = _resolve_in(root, data.get("to", ""))
    if src == root or dst == root:
        raise HTTPException(400, "The volume root cannot be renamed.")
    if not os.path.lexists(src):
        raise HTTPException(404, "Source no longer exists.")
    if os.path.lexists(dst):
        raise HTTPException(409, "Destination already exists.")
    if not os.path.isdir(os.path.dirname(dst)):
        raise HTTPException(404, "Destination directory does not exist.")
    os.rename(src, dst)
    audit("FILE_RENAME", name, f"{_rel_to(root, src)} -> {_rel_to(root, dst)}")
    return JSONResponse({"message": f"Moved to {_rel_to(root, dst)}."})

@app.post("/api/server/{name}/files/delete")
async def files_delete(request: Request, name: str = Path(...)):
    root  = _server_root(name)
    data  = await request.json()
    paths = data.get("paths") or ([data["path"]] if data.get("path") else [])
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list):
        raise HTTPException(400, "Expected a list of paths.")
    if not paths:
        raise HTTPException(400, "Nothing selected.")
    removed, failed = [], []
    for rel in paths:
        target = _resolve_in(root, rel)
        if target == root:
            raise HTTPException(400, "The volume root cannot be deleted.")
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.unlink(target)
            removed.append(_rel_to(root, target))
        except OSError as exc:
            failed.append(f"{_rel_to(root, target)}: {exc.strerror or exc}")
    audit("FILE_DELETE", name, f"removed={len(removed)} failed={len(failed)}")
    if failed and not removed:
        raise HTTPException(500, "; ".join(failed))
    msg = f"Deleted {len(removed)} item(s)."
    if failed:
        msg += f" {len(failed)} failed: " + "; ".join(failed)
    return JSONResponse({"message": msg, "removed": removed, "failed": failed})

@app.post("/api/server/{name}/files/upload")
async def files_upload(name: str = Path(...), path: str = Form(""),
                       files: list[UploadFile] = File(...)):
    root   = _server_root(name)
    target = _resolve_in(root, path)
    if not os.path.isdir(target):
        raise HTTPException(404, "Destination directory not found.")
    saved = []
    for up in files:
        base = os.path.basename(up.filename or "")
        if not base or base in (".", ".."):
            raise HTTPException(400, "Invalid filename in upload.")
        dest = _resolve_in(root, os.path.join(_rel_to(root, target), base))
        saved.append({"name": base, "size": await _save_upload(up, dest)})
    audit("FILE_UPLOAD", name, f"dir={_rel_to(root, target) or '/'} files={len(saved)}")
    return JSONResponse({"message": f"Uploaded {len(saved)} file(s).", "files": saved})

@app.get("/api/server/{name}/files/download")
async def files_download(name: str = Path(...), path: str = ""):
    root   = _server_root(name)
    target = _resolve_in(root, path)
    if not os.path.isfile(target):
        raise HTTPException(404, "File not found.")
    audit("FILE_DOWNLOAD", name, f"path={_rel_to(root, target)}")
    return FileResponse(target, filename=os.path.basename(target),
                        media_type="application/octet-stream")

# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------
# Anything whose name looks like a credential is reported as set/unset only.
_SECRET_ENV_RE = re.compile(r'(PASS|SECRET|TOKEN|CRED|_KEY$|APIKEY)', re.I)

# Lines worth surfacing out of a few hundred of boot spam.
_LOG_PROBLEM_RE = re.compile(
    r'(\bERROR\b|\bFATAL\b|\bSEVERE\b|Exception|Caused by:|'
    r'Cannot |Failed to |Unable to |\bWARN\b)')

_DEBUG_PROP_KEYS = [
    "server-port", "server-ip", "level-name", "level-type", "online-mode",
    "difficulty", "gamemode", "max-players", "view-distance", "simulation-distance",
    "enable-rcon", "rcon.port", "rcon.password", "enable-query", "query.port",
    "white-list", "enforce-whitelist", "motd", "max-tick-time", "spawn-protection",
]

def _docker_inspect(name: str) -> dict | None:
    code, out, _ = _run(["docker", "inspect", name], timeout=15)
    if code != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return data[0] if data else None

def _recent_files(path: str, limit: int = 8) -> list[dict]:
    if not os.path.isdir(path):
        return []
    items = []
    try:
        for f in os.listdir(path):
            fp = os.path.join(path, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            items.append({"name": f, "size": st.st_size, "human": _human(st.st_size),
                          "mtime": int(st.st_mtime)})
    except OSError:
        return []
    items.sort(key=lambda e: e["mtime"], reverse=True)
    return items[:limit]

def _port_report(info: dict) -> list[dict]:
    """Published ports, cross-checked against what is actually listening."""
    mapped    = (info.get("NetworkSettings") or {}).get("Ports") or {}
    listening = _host_listeners()
    rows: list[dict] = []
    for cport, binds in sorted(mapped.items()):
        cnum, _, proto = cport.partition("/")
        proto = proto or "tcp"
        if not binds:
            rows.append({"container": cnum, "proto": proto, "host": "", "ip": "",
                         "published": False, "listening": False})
            continue
        for b in binds:
            hp = str(b.get("HostPort") or "")
            rows.append({
                "container": cnum, "proto": proto, "host": hp,
                "ip": b.get("HostIp") or "0.0.0.0", "published": bool(hp),
                "listening": bool(hp) and (int(hp), proto) in listening,
            })
    return rows

def _debug_checks(name: str, root: str, container: dict, props: dict,
                  ports: list[dict], crash: list[dict], disk_free: int) -> list[dict]:
    """The part that actually saves time: turn the raw facts into findings.

    Every entry is something a human would otherwise have to notice by reading
    three panels at once.
    """
    out: list[dict] = []
    def add(level: str, title: str, detail: str):
        out.append({"level": level, "title": title, "detail": detail})

    if not container.get("exists"):
        add("error", "No container",
            f"docker has no container named '{name}'. The volume is still on disk, so "
            f"redeploying with the same ID picks the world back up.")

    state = container.get("status", "")
    if state == "exited" and container.get("exit_code"):
        add("error", f"Exited with code {container['exit_code']}",
            container.get("error") or "Check the log tail below for the last lines before shutdown.")
    if container.get("oom_killed"):
        add("error", "Killed by the OOM reaper",
            "The container hit its memory limit. Raise MEMORY/-Xmx or the container limit.")
    if container.get("restart_count", 0) >= 3:
        add("warn", f"Restarted {container['restart_count']} times",
            "A crash loop — the same failure is most likely at the end of every log cycle.")
    health = (container.get("health") or {}).get("status")
    if health and health not in ("healthy", "starting"):
        add("warn", f"Health check: {health}", "The image's own readiness probe is failing.")

    eula = os.path.join(root, "eula.txt")
    if os.path.isfile(eula):
        try:
            with open(eula) as f:
                if "eula=true" not in f.read().lower():
                    add("error", "EULA not accepted",
                        "eula.txt does not say eula=true, so the server exits immediately on boot.")
        except OSError:
            pass
    elif container.get("running"):
        add("info", "No eula.txt yet", "Normal on a container that has never finished a boot.")

    if props:
        if props.get("enable-rcon", "false").lower() != "true":
            add("warn", "RCON disabled",
                "enable-rcon=false — the Console and Players panels cannot talk to this server.")
        elif not props.get("rcon.password"):
            add("warn", "RCON password is empty",
                "Most builds refuse RCON logins without a password, so console commands will silently fail.")
        prop_port = props.get("server-port", "")
        if container.get("exists") and prop_port and not any(p["container"] == prop_port for p in ports):
            add("warn", f"server-port={prop_port} is not published",
                "No container port matches server.properties, so nothing on the host can reach the server.")
        level = props.get("level-name", "world")
        if not os.path.isdir(os.path.join(root, level)):
            add("info", f"World '{level}' not generated yet",
                "The directory appears on the first successful boot.")
    else:
        add("info", "No server.properties",
            "It is written on the first boot; until then Properties and World stay empty.")

    if container.get("running"):
        dead = [f"{p['host']}/{p['proto']}" for p in ports if p["published"] and not p["listening"]]
        if dead:
            add("warn", "Published but not listening: " + ", ".join(dead),
                "docker holds the host port but nothing inside the container answers on it yet — "
                "usually means the server is still starting, or crashed after the port was claimed.")

    if crash:
        newest = crash[0]
        started = container.get("started_at_ts") or 0
        when    = datetime.datetime.fromtimestamp(newest["mtime"]).isoformat(timespec="seconds")
        if started and newest["mtime"] >= started:
            add("error", "Crash report from the current run", f"{newest['name']} ({when})")
        else:
            add("info", "Older crash report on disk", f"{newest['name']} ({when})")

    if disk_free and disk_free < 2 * 1024 ** 3:
        add("warn", f"Only {_human(disk_free)} free on the data filesystem",
            "Chunk saves and backups fail quietly when the disk fills up.")

    if not out:
        add("ok", "No problems detected", "Container state, ports, EULA, RCON and disk all look sane.")
    return out

# ---------------------------------------------------------------------------
# REST — Debug
# ---------------------------------------------------------------------------
@app.get("/api/server/{name}/debug")
async def server_debug(name: str = Path(...)):
    """One snapshot with everything needed to work out why a server misbehaves."""
    validate_name(name)
    root = os.path.realpath(os.path.join(DATA_DIR, name))

    info  = await asyncio.to_thread(_docker_inspect, name)
    state = (info or {}).get("State") or {}
    cfg   = (info or {}).get("Config") or {}
    hcfg  = (info or {}).get("HostConfig") or {}

    def _ts(iso: str) -> int:
        try:
            return int(datetime.datetime.fromisoformat(
                (iso or "").replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0

    health = state.get("Health") or {}
    container = {
        "exists":        info is not None,
        "id":            (info or {}).get("Id", "")[:12],
        "image":         cfg.get("Image", ""),
        "status":        state.get("Status", "absent"),
        "running":       bool(state.get("Running")),
        "exit_code":     state.get("ExitCode", 0),
        "error":         state.get("Error", ""),
        "oom_killed":    bool(state.get("OOMKilled")),
        "pid":           state.get("Pid", 0),
        "restart_count": (info or {}).get("RestartCount", 0),
        "created":       (info or {}).get("Created", ""),
        "started_at":    state.get("StartedAt", ""),
        "started_at_ts": _ts(state.get("StartedAt", "")),
        "finished_at":   state.get("FinishedAt", ""),
        "restart_policy": (hcfg.get("RestartPolicy") or {}).get("Name", ""),
        "memory_limit":  _human(hcfg["Memory"]) if hcfg.get("Memory") else "unlimited",
        "cpu_limit":     (f"{hcfg['NanoCpus'] / 1e9:g}" if hcfg.get("NanoCpus") else "unlimited"),
        "compose_service": (cfg.get("Labels") or {}).get("com.docker.compose.service", ""),
        "health": {
            "status":         health.get("Status", ""),
            "failing_streak": health.get("FailingStreak", 0),
            "log": [{"exit": e.get("ExitCode"), "end": e.get("End", ""),
                     "output": (e.get("Output") or "").strip()[:400]}
                    for e in (health.get("Log") or [])[-3:]],
        },
    }

    env_raw = {}
    for line in cfg.get("Env") or []:
        if "=" in line:
            k, v = line.split("=", 1)
            env_raw[k] = v
    env = {k: ("(set — hidden)" if v else "(empty)") if _SECRET_ENV_RE.search(k) else v
           for k, v in sorted(env_raw.items())}

    ports = _port_report(info) if info else []

    stats = {}
    if container["running"]:
        code, out, _ = await asyncio.to_thread(
            _run, ["docker", "stats", "--no-stream", "--format",
                   "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}|{{.PIDs}}",
                   name], 20)
        if code == 0 and out:
            p = (out.split("|") + [""] * 6)[:6]
            stats = {"cpu": p[0].strip(), "mem": p[1].strip(), "mem_perc": p[2].strip(),
                     "net_io": p[3].strip(), "block_io": p[4].strip(), "pids": p[5].strip()}

    top = ""
    if container["running"]:
        code, out, _ = await asyncio.to_thread(
            _run, ["docker", "top", name, "-o", "pid,pcpu,pmem,etime,args"], 15)
        top = out if code == 0 else ""

    props = _parse_properties(name) if os.path.isdir(root) else None
    props_view = {}
    for k in _DEBUG_PROP_KEYS:
        if props and k in props:
            props_view[k] = "(set — hidden)" if k == "rcon.password" and props[k] else props[k]

    level  = (props or {}).get("level-name", "world")
    crash  = _recent_files(os.path.join(root, "crash-reports"))
    logs   = _recent_files(os.path.join(root, "logs"))
    key_files = []
    for rel in ("server.properties", "eula.txt", "ops.json", "whitelist.json",
                "pack.toml", "logs/latest.log", f"{level}/level.dat"):
        fp = os.path.join(root, rel)
        exists = os.path.exists(fp)
        entry = {"path": rel, "exists": exists}
        if exists:
            try:
                st = os.stat(fp)
                entry.update(size=st.st_size, human=_human(st.st_size), mtime=int(st.st_mtime))
            except OSError:
                pass
        key_files.append(entry)
    key_files.append(_launcher_entry(root))

    try:
        du = shutil.disk_usage(DATA_DIR if os.path.isdir(DATA_DIR) else "/")
        disk = {"total": _human(du.total), "used": _human(du.used),
                "free": _human(du.free), "free_bytes": du.free,
                "percent": round(du.used / du.total * 100, 1) if du.total else 0}
    except OSError:
        disk = {"free_bytes": 0}

    _, docker_ver, _ = await asyncio.to_thread(
        _run, ["docker", "version", "--format", "{{.Server.Version}}"], 10)
    img_code, _, _ = await asyncio.to_thread(
        _run, ["docker", "image", "inspect", cfg.get("Image") or DOCKER_IMAGE], 10)

    audit_lines: list[str] = []
    if os.path.exists(AUDIT_LOG):
        try:
            with open(AUDIT_LOG) as f:
                audit_lines = [l.rstrip() for l in f if f"target={name} " in l or
                               l.rstrip().endswith(f"target={name}")][-15:]
            audit_lines.reverse()
        except OSError:
            pass

    host = {
        "docker_version": docker_ver or "unavailable",
        "image":          cfg.get("Image") or DOCKER_IMAGE,
        "image_present":  img_code == 0,
        "python":         sys.version.split()[0],
        "data_dir":       DATA_DIR,
        "volume":         root,
        "volume_exists":  os.path.isdir(root),
        "cpu":            psutil.cpu_percent(interval=None),
        "ram":            psutil.virtual_memory().percent,
        "load":           [round(x, 2) for x in os.getloadavg()],
        "disk":           disk,
        "packwiz":        _packwiz_version(_packwiz_path()) if _packwiz_path() else "",
    }

    return JSONResponse({
        "server":     name,
        "generated":  datetime.datetime.now().isoformat(timespec="seconds"),
        "container":  container,
        "env":        env,
        "ports":      ports,
        "stats":      stats,
        "top":        top,
        "properties": props_view,
        "files":      key_files,
        "crash_reports": crash,
        "log_files":  logs,
        "audit":      audit_lines,
        "host":       host,
        "checks":     _debug_checks(name, root, container, props or {}, ports, crash,
                                    disk.get("free_bytes", 0)),
    })

# Only vanilla actually boots a file called server.jar. Fabric, Paper, Forge
# and friends each ship their own launcher name, so looking for one fixed
# filename reported "missing" on a server that was running perfectly well.
_LAUNCHER_RE = re.compile(
    r'^(?:server|fabric-server|forge|neoforge|quilt-server|paper|purpur|folia|'
    r'spigot|craftbukkit|velocity|waterfall|bungeecord|minecraft_server)'
    r'[\w.+-]*\.jar$', re.I)

def _launcher_entry(root: str) -> dict:
    """The server jar this instance would actually start, whatever it is named."""
    candidates = []
    try:
        for f in os.listdir(root):
            if not f.lower().endswith(".jar") or not _LAUNCHER_RE.match(f):
                continue
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            candidates.append((st.st_size, f, st))
    except OSError:
        pass
    if not candidates:
        return {"path": "server jar", "exists": False,
                "note": "no launcher jar in the volume root"}
    # Largest wins: the boot jar dwarfs anything incidental sitting beside it.
    size, fname, st = max(candidates, key=lambda c: c[0])
    return {"path": fname, "exists": True, "size": size,
            "human": _human(size), "mtime": int(st.st_mtime),
            "note": "launcher jar"}

# Collapsing repeats needs a notion of "the same line again" that ignores the
# parts guaranteed to differ: the timestamp, the thread, and any number or
# path embedded in the message. One mod warning about 300 files produces 300
# lines that are identical apart from the filename.
_LOG_STAMP_RE = re.compile(r'^\[[^\]]*\]\s*(\[[^\]]*\]:?)?\s*')
_LOG_VARY_RE  = re.compile(
    r'\b[\w./-]+\.(?:png|json|jar|toml|mcmeta|ogg|nbt)\b'   # asset paths
    r'|\b[a-z0-9_]+:[a-z0-9_/.]+\b'                          # minecraft:stone, create:track
    r'|\b\d+\b')                                             # counts, sizes, coordinates

def _log_signature(line: str) -> str:
    """What makes two log lines 'the same message'."""
    return _LOG_VARY_RE.sub("·", _LOG_STAMP_RE.sub("", line)).strip().lower()[:200]

def _collapse_repeats(lines: list[str]) -> list[dict]:
    """Group consecutive same-shaped lines into one row with a count.

    Only consecutive runs are merged: a warning that recurs after other output
    is worth seeing again, whereas 300 in a row is one event.
    """
    out: list[dict] = []
    for line in lines:
        sig = _log_signature(line)
        if out and out[-1]["sig"] == sig:
            out[-1]["count"] += 1
            continue
        out.append({"text": line, "sig": sig, "count": 1})
    for row in out:
        row.pop("sig", None)
    return out

@app.get("/api/server/{name}/debug/logs")
async def debug_logs(name: str = Path(...), lines: int = 400,
                     problems: bool = False, collapse: bool = True):
    """Log tail for the debug screen — optionally only the lines that matter.

    Repeats are collapsed by default: a single mod can emit hundreds of
    near-identical warnings, which otherwise push everything else out of view.
    """
    validate_name(name)
    lines = max(1, min(int(lines), 5000))
    code, out, err = await asyncio.to_thread(
        _run, ["docker", "logs", "--tail", str(lines), name], 30)
    if code != 0:
        raise HTTPException(503, err or "docker logs failed.")
    text = "\n".join(p for p in (out, err) if p)
    all_lines = text.splitlines()
    hits = [l for l in all_lines if _LOG_PROBLEM_RE.search(l)]
    selected = hits[-lines:] if problems else all_lines

    rows = _collapse_repeats(selected) if collapse else [
        {"text": l, "count": 1} for l in selected]
    return JSONResponse({
        # Plain strings kept for anything still reading the old shape.
        "lines":    [r["text"] for r in rows],
        "rows":     rows,
        "total":    len(all_lines),
        "shown":    len(selected),
        "problems": len(hits),
        "filtered": problems,
        "collapsed": len(selected) - len(rows),
    })

@app.get("/api/server/{name}/debug/disk")
async def debug_disk(name: str = Path(...)):
    """Per-directory usage of the volume. Separate from /debug because du has
    to walk the world, which on a large save is not something to do on every
    refresh of the panel."""
    root     = _server_root(name)
    children = [os.path.join(root, d) for d in sorted(os.listdir(root))]
    if not children:
        return JSONResponse({"total": 0, "total_human": "0 B", "entries": []})
    # du is given the children only: handed the root as well it credits every
    # byte to that one line and reports nothing for the entries underneath.
    code, out, err = await asyncio.to_thread(_run, ["du", "-sb", "--"] + children, 120)
    if code not in (0, 1) or not out:
        raise HTTPException(503, err or "du failed.")
    rows, total = [], 0
    for line in out.splitlines():
        size, _, path = line.partition("\t")
        try:
            size_i = int(size)
        except ValueError:
            continue
        total += size_i
        rows.append({"name": os.path.basename(path), "bytes": size_i,
                     "human": _human(size_i),
                     "dir": os.path.isdir(path)})
    rows.sort(key=lambda r: r["bytes"], reverse=True)
    return JSONResponse({"total": total, "total_human": _human(total),
                         "entries": rows[:40], "partial": bool(err)})

# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # HTTP middleware does not run for websockets, so the session cookie has to
    # be checked here. Without this the socket would hand out live logs and
    # RCON execution to anyone who could open it.
    if not _session_valid(websocket.cookies.get(SESSION_COOKIE)):
        await websocket.close(code=1008)    # policy violation
        return

    await websocket.accept()
    active_log_task: asyncio.Task | None = None
    connected = True          # shared flag — set False the moment the socket closes

    psutil.cpu_percent(interval=None)
    await asyncio.sleep(0.1)

    async def safe_send(payload: dict) -> bool:
        """Send JSON if the socket is still open. Returns False if it isn't."""
        if not connected:
            return False
        try:
            await websocket.send_json(payload)
            return True
        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError covers "Cannot call send once a close message has been sent"
            return False
        except Exception:
            return False

    async def stats_engine():
        while connected:
            try:
                cpu  = psutil.cpu_percent(interval=1)
                ram  = psutil.virtual_memory().percent
                disk = psutil.disk_usage('/').percent
                ok = await safe_send({
                    "type": "sys_stats",
                    "cpu": round(cpu, 1), "ram": round(ram, 1), "disk": round(disk, 1),
                })
                if not ok:
                    break
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(2)

    stats_task = asyncio.create_task(stats_engine())

    async def stream_logs(container_name: str):
        proc = await asyncio.create_subprocess_exec(
            "docker", "logs", "-f", "--tail", "100", container_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            if proc.stdout:
                async for line in proc.stdout:
                    if not connected:
                        break
                    ok = await safe_send({
                        "type": "docker_log",
                        "data": line.decode("utf-8", errors="replace").rstrip(),
                    })
                    if not ok:
                        break
        except asyncio.CancelledError:
            pass
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            method = msg.get("method", "")

            if method == "docker:logs/subscribe":
                target = msg.get("target", "")
                if target and _NAME_RE.match(target):
                    if active_log_task:
                        active_log_task.cancel()
                    active_log_task = asyncio.create_task(stream_logs(target))

            elif method == "docker:exec/command":
                target  = msg.get("target", "")
                command = msg.get("command", "")
                if target and command and _NAME_RE.match(target):
                    # Fire-and-forget in a thread; response arrives via the log stream
                    await asyncio.to_thread(_run, ["docker", "exec", target, "rcon-cli", command])

            elif method == "player:list":
                target = msg.get("target", "")
                if target and _NAME_RE.match(target):
                    # Run the blocking RCON call in a thread, then check connected before sending
                    code, out, _ = await asyncio.to_thread(
                        _run, ["docker", "exec", target, "rcon-cli", "list"]
                    )
                    players: list[str] = []
                    if code == 0 and out:
                        m = re.search(r'players online:\s*(.*)', out, re.IGNORECASE)
                        if m and m.group(1).strip():
                            players = [p.strip() for p in m.group(1).split(",") if p.strip()]
                    if code == 0:
                        # Free data point — the sampler stays RCON-free on purpose,
                        # so this is the only place a player count comes from.
                        _record_players(target, len(players))
                    await safe_send({"type": "player_list", "players": players})

            elif method == "player:data":
                target   = msg.get("target", "")
                player   = msg.get("player", "")
                category = msg.get("category", "all")
                if target and player and _NAME_RE.match(target):
                    cmd_map = {
                        "all":       f"data get entity {player}",
                        "identity":  f"data get entity {player} UUID",
                        "location":  f"data get entity {player} Pos",
                        "inventory": f"data get entity {player} Inventory",
                        "vitals":    f"data get entity {player} Health",
                        "mechanics": f"data get entity {player} abilities",
                        "metadata":  f"data get entity {player} playerGameType",
                    }
                    _, out, _ = await asyncio.to_thread(
                        _run, ["docker", "exec", target, "rcon-cli",
                               cmd_map.get(category, f"data get entity {player}")]
                    )
                    await safe_send({"type": "player_data", "data": out or "(no data)"})

            elif method == "tps:poll":
                target = msg.get("target", "")
                if target and _NAME_RE.match(target):
                    code, out, _ = await asyncio.to_thread(
                        _run, ["docker", "exec", target, "rcon-cli", "tps"]
                    )
                    await safe_send({"type": "tps_data", "raw": out if code == 0 else "", "ok": code == 0})

    except WebSocketDisconnect:
        pass
    except json.JSONDecodeError:
        pass
    except RuntimeError:
        # Client closed the connection mid-receive
        pass
    finally:
        connected = False          # signal all coroutines to stop trying to send
        stats_task.cancel()
        if active_log_task:
            active_log_task.cancel()

# ---------------------------------------------------------------------------
# Static
#
# StaticFiles sends ETag and Last-Modified but no Cache-Control, which leaves
# the browser to guess how long a file stays fresh. It guesses in hours, so a
# deploy that changes script.js and index.html together can leave a browser
# running the new markup against the old script — every handler added in that
# release is suddenly "not defined".
#
# `no-cache` does not mean "do not store": it means "revalidate before use".
# The ETag above is what makes that cheap, so the browser gets a 304 with an
# empty body unless the file really did change.
# ---------------------------------------------------------------------------
class RevalidatingStatics(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

app.mount("/", RevalidatingStatics(directory=os.path.dirname(os.path.abspath(__file__)), html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT)