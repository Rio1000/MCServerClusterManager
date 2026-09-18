import asyncio
import json
import os
import re
import psutil
import datetime
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, Request, UploadFile, File, Path, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import uvicorn

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR      = "/home/spark/piracy-vpn/Minecraft_Servers"
DOCKER_IMAGE  = "itzg/minecraft-server"
BACKUP_DIR    = os.path.join(DATA_DIR, "_backups")
JOBS_FILE     = os.path.join(DATA_DIR, "_jobs.json")
TEMPLATES_FILE = os.path.join(DATA_DIR, "_templates.json")
AUDIT_LOG     = os.path.join(DATA_DIR, "_audit.log")

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
# Lifespan
# ---------------------------------------------------------------------------
scheduler  = AsyncIOScheduler()
_jobs_lock = asyncio.Lock()

psutil.cpu_percent(interval=None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA_DIR,   exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    for j in _load_jobs_raw():
        try:
            scheduler.add_job(
                _execute_job, CronTrigger.from_crontab(j["cron"]),
                args=[j["action"], j["target"]], id=j["id"], replace_existing=True,
            )
        except Exception as exc:
            print(f"[startup] job {j['id']} failed: {exc}")
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)

app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str]) -> tuple[int, str, str]:
    if not shutil.which(cmd[0]):
        return 1, "", f"'{cmd[0]}' not found on host."
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
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


def _bound_host_ports() -> set[tuple[int, str]]:
    """Every (host port, protocol) already bound by a managed container."""
    bound: set[tuple[int, str]] = set()
    for srv in _get_containers():
        for host, proto in _BOUND_PORT_RE.findall(srv.get("ports", "")):
            bound.add((int(host), proto))
    return bound


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
    elif action in ("start", "stop", "restart"):
        await asyncio.to_thread(_run, ["docker", action, target])

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
    bindings += [_parse_port_spec(x) for x in extra_raw]

    seen: set[tuple[int, str]] = set()
    for b in bindings:
        key = (b["host"], b["proto"])
        if key in seen:
            raise HTTPException(400, f"Port {b['host']}/{b['proto']} is listed twice.")
        seen.add(key)

    taken = _bound_host_ports()
    for b in bindings:
        if (b["host"], b["proto"]) in taken:
            raise HTTPException(
                409, f"Port {b['host']}/{b['proto']} is already bound by another container.")

    server_path = safe_path(DATA_DIR, name)
    os.makedirs(server_path, exist_ok=True)

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
    extra_note = f" ({len(bindings) - 1} extra port(s) bound)" if len(bindings) > 1 else ""
    return JSONResponse({"message": f"Server '{name}' deployed.{extra_note}"})

@app.post("/api/server/{name}/action")
async def server_action(request: Request, name: str = Path(...)):
    validate_name(name)
    data   = await request.json()
    action = data.get("action")
    if action not in ("start", "stop", "restart", "kill", "delete"):
        raise HTTPException(400, "Invalid action.")
    cmd = ["docker", "rm", "-f", name] if action == "delete" else ["docker", action, name]
    code, _, err = _run(cmd)
    if code != 0:
        raise HTTPException(500, f"Docker error: {err}")
    audit("ACTION", name, f"action={action}")
    return JSONResponse({"message": f"Container '{name}' → {action}."})

# ---------------------------------------------------------------------------
# REST — Container resource limits
# ---------------------------------------------------------------------------
@app.post("/api/server/{name}/resources")
async def update_resources(request: Request, name: str = Path(...)):
    validate_name(name)
    data   = await request.json()
    memory = data.get("memory", "").strip()
    cpus   = data.get("cpus", "").strip()

    if memory and not re.match(r'^\d+[MmGg]$', memory):
        raise HTTPException(400, "Invalid memory (e.g. 2G, 512M).")
    if cpus:
        try:
            v = float(cpus)
            if v <= 0: raise ValueError()
        except ValueError:
            raise HTTPException(400, "Invalid CPU value (e.g. 1.5).")

    update_args: list[str] = []
    if memory: update_args += ["--memory", memory.upper()]
    if cpus:   update_args += ["--cpus",   cpus]
    if not update_args:
        raise HTTPException(400, "Provide at least one resource limit.")

    code, _, err = _run(["docker", "update"] + update_args + [name])
    if code != 0:
        raise HTTPException(500, f"docker update failed: {err}")
    audit("RESOURCES", name, f"memory={memory} cpus={cpus}")
    return JSONResponse({"message": f"Resource limits updated for '{name}'."})

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

@app.post("/api/server/{name}/properties/save")
async def save_properties(request: Request, name: str = Path(...)):
    validate_name(name)
    data     = await request.json()
    filepath = safe_path(DATA_DIR, name, "server.properties")
    dir_path = os.path.dirname(filepath)
    if not os.path.exists(dir_path):
        raise HTTPException(404, "Server data directory missing.")

    lines = [f"# Updated via Cluster Manager\n# {datetime.datetime.now().isoformat()}\n"]
    for k, v in data.items():
        safe_k = _sanitize_prop_key(k)
        if safe_k is None: continue
        lines.append(f"{safe_k}={_sanitize_prop_val(v)}\n")

    with open(filepath, "w") as f: f.writelines(lines)
    audit("PROPS_SAVE", name, f"keys={len(lines)-2}")
    return JSONResponse({"message": "Properties saved."})

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
        init = subprocess.run([packwiz] + _packwiz_init_args(name, _docker_env(name)),
                              cwd=server_path, capture_output=True, text=True, check=False)
        if init.returncode != 0:
            raise HTTPException(500, f"Packwiz init failed: {init.stderr.strip()[:400]}")
    res = subprocess.run([packwiz, "modrinth", action, mod_slug, "-y"],
                         cwd=server_path, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise HTTPException(500, f"Packwiz error: {res.stderr.strip()}")
    subprocess.run([packwiz, "refresh"], cwd=server_path, capture_output=True, check=False)
    audit("PACKWIZ", name, f"action={action} mod={mod_slug}")
    return JSONResponse({"message": f"Packwiz {action} completed for '{mod_slug}'."})

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
        env = _docker_env(name)
        init = subprocess.run([packwiz] + _packwiz_init_args(name, env),
                              cwd=server_path, capture_output=True, text=True, check=False)
        if init.returncode != 0:
            raise HTTPException(500, f"Packwiz init failed: {init.stderr.strip()[:400]}")

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
    audit("BACKUP", name, f"file={os.path.basename(backup_file)}")
    return JSONResponse({"message": f"Snapshot created: {os.path.basename(backup_file)}"})

@app.get("/api/backups")
async def list_backups():
    files = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.endswith(".tar.gz")),
        reverse=True,
    )
    return JSONResponse({"backups": files})

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
    async with _jobs_lock:
        jobs = [j for j in _load_jobs_raw() if j["id"] != job_id]
        _save_jobs_raw(jobs)
    try: scheduler.remove_job(job_id)
    except Exception: pass
    return JSONResponse({"message": "Task removed."})

# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
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
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory=os.path.dirname(os.path.abspath(__file__)), html=True), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)