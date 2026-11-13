#!/usr/bin/env python3
"""
Minecraft server version / mod update with a blocking compatibility gate.

    ./mc-update.py --check  --latest
    ./mc-update.py --check  --latest --json
    ./mc-update.py --update --server minecraft-crossplay --target 26.2
    ./mc-update.py --scan   --server CSUSERVER --json        # what packwiz does not track
    ./mc-update.py --adopt  --server CSUSERVER               # track it
    ./mc-update.py --update-mods --server CSUSERVER          # newest builds, same MC version
    ./mc-update.py --regen  -- --dim the_nether --dry-run    # -> mc-chunkdiff.py

Flow for --update:
    1. Resolve the target version (--target, or --latest / --latest-snapshot
       from Mojang's manifest) and report it against the installed version.
    2. Resolve every mod in THIS SERVER's packwiz pack against the target MC
       version via the Modrinth API. ANY unresolved mod or unsatisfied required
       dependency ABORTS. Unknown is treated as incompatible, because Modrinth
       metadata is self-reported and frequently lags reality.
    3. Jars in mods/ that packwiz does not track are identified by hash on
       Modrinth. A match is adopted into the pack (a .pw.toml is written for
       it) so packwiz updates it like any other mod; a jar Modrinth has never
       seen is judged on the minecraft dependency it declares itself.
    4. Take a verified borg backup and confirm the archive exists.
    5. Snapshot the pack metadata and the container definition, bump VERSION
       (compose block, or the docker-run container itself), run packwiz,
       recreate the container, and watch the boot. Any failure — including an
       exception halfway — restores every one of those and says what to do.

Chunk regeneration (--regen) is deliberately a SEPARATE command, now handled
by mc-chunkdiff.py. Everything after --regen is passed straight through.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Where the server directories live. The manager passes MCSCM_DATA_DIR; run by
# hand, this follows the manager's own rule — the parent of its checkout —
# and also works through the mc-update.py symlink kept beside the server data
# (abspath, deliberately not realpath, so the symlink's directory is used).
# This used to be a hard-coded absolute path.
_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("MCSCM_DATA_DIR") or (
    os.path.dirname(_HERE) if os.path.isfile(os.path.join(_HERE, "web_manager.py")) else _HERE)
BACKUP_SCRIPT = os.path.join(DATA_DIR, "mc-backup.sh")
COMPOSE = os.environ.get(
    "MCSCM_COMPOSE", os.path.join(os.path.dirname(DATA_DIR), "docker-compose.minecraft.yml"))
BORG_REPO = os.environ.get("BORG_REPO", "/mnt/media/backups/minecraft-borg")
SNAPSHOT_DIR = os.path.join(DATA_DIR, "_update_snapshots")
SNAPSHOT_KEEP = 10
# Jars an update replaces are moved here, never deleted. The leading underscore
# keeps it out of the packwiz index (.packwizignore has /_*/).
REPLACED_DIR = "_mods_replaced"
RESOLVED_INDEX = ".packwiz-resolved.json"
LEGACY_PACK = os.path.join(DATA_DIR, "CrossPlayServer26.1.2Mods")
MODRINTH = "https://api.modrinth.com/v2"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
UA = "spark-servermanager/1.1 (self-hosted)"

# Where a pack file may point. The hash check guarantees integrity; this stops
# a hand-edited .pw.toml aiming the updater at an arbitrary host.
DOWNLOAD_HOSTS = (
    "cdn.modrinth.com", "api.modrinth.com",
    "mediafilez.forgecdn.net", "edge.forgecdn.net", "media.forgecdn.net",
)
DOWNLOAD_MAX = 512 * 1024 * 1024

LOADERS = {"FABRIC": "fabric", "FORGE": "forge", "NEOFORGE": "neoforge", "QUILT": "quilt"}

C_OK, C_BAD, C_WARN, C_DIM, C_END = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# --json callers get a single JSON document on stdout and nothing else, so the
# human-readable stream is routed to stderr rather than suppressed.
JSON_MODE = False


def _out(s):
    print(s, file=sys.stderr if JSON_MODE else sys.stdout, flush=True)


def ok(m):   _out(f"  {C_OK}✓{C_END} {m}")
def bad(m):  _out(f"  {C_BAD}✗{C_END} {m}")
def warn(m): _out(f"  {C_WARN}!{C_END} {m}")
def step(m): _out(f"\n\033[1m==> {m}\033[0m")


def emit(obj, code):
    """Final JSON document for --json, then exit."""
    print(json.dumps(obj, indent=2))
    sys.exit(code)


def die(m, obj=None):
    bad(m)
    if JSON_MODE:
        emit({**(obj or {}), "ok": False, "error": m}, 1)
    sys.exit(1)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False,
                          stdin=subprocess.DEVNULL, **kw)


def stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


# ===========================================================================
# Modrinth
# ===========================================================================
_cache: dict = {}


class ApiError(Exception):
    """Modrinth could not be reached. NOT the same thing as "no such build"."""


def api(path: str, params: dict | None = None, body: object | None = None) -> object | None:
    """GET (or POST when `body` is given). Returns None only for a real 404.

    This used to return None for every failure, so a network blip or a rate
    limit read as "no compatible build" and blocked an update for a reason
    that had nothing to do with the mod.
    """
    url = f"{MODRINTH}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    key = url + ("|" + json.dumps(body, sort_keys=True) if body is not None else "")
    if key in _cache:
        return _cache[key]
    headers = {"User-Agent": UA, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    last = "no attempt made"
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                out = json.load(r)
            _cache[key] = out
            time.sleep(0.12)          # stay well inside Modrinth's rate limit
            return out
        except urllib.error.HTTPError as e:
            if e.code == 404:
                _cache[key] = None
                return None
            last = f"HTTP {e.code}"
            time.sleep(5 * (attempt + 1) if e.code == 429 else 2)
        except Exception as e:
            last = str(e)[:120]
            time.sleep(2)
    raise ApiError(f"Modrinth API unreachable for {path} ({last})")


# ===========================================================================
# Docker / server discovery
# ===========================================================================
_inspect_cache: dict = {}


def inspect(name: str, fresh: bool = False) -> dict | None:
    if not fresh and name in _inspect_cache:
        return _inspect_cache[name]
    try:
        r = run(["docker", "inspect", name], timeout=30)
        info = json.loads(r.stdout)[0] if r.returncode == 0 else None
    except Exception:
        info = None
    _inspect_cache[name] = info
    return info


def container_env(name: str) -> dict:
    info = inspect(name) or {}
    env = {}
    for line in ((info.get("Config") or {}).get("Env") or []):
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def compose_service_of(name: str) -> str | None:
    info = inspect(name) or {}
    labels = (info.get("Config") or {}).get("Labels") or {}
    return labels.get("com.docker.compose.service") or None


def resolve_pack(server: str, explicit: str | None) -> tuple[str | None, str]:
    """Which packwiz pack belongs to this server. Returns (dir, how-it-was-found).

    The old default was one fixed directory (CrossPlayServer26.1.2Mods) for
    EVERY server, so checking any other server compared its jars against the
    crossplay pack and reported its own managed mods as "not managed by
    packwiz".
    """
    if explicit:
        return explicit, "--pack"
    sdir = os.path.join(DATA_DIR, server)
    info = inspect(server)
    url = container_env(server).get("PACKWIZ_URL", "")
    if url.startswith("file://") and info:
        inside = os.path.dirname(urllib.parse.urlparse(url).path)
        best = None
        for m in info.get("Mounts") or []:
            dest = (m.get("Destination") or "").rstrip("/")
            if dest and (inside == dest or inside.startswith(dest + "/")):
                if best is None or len(dest) > len(best[0]):
                    best = (dest, m.get("Source") or "")
        if best and best[1]:
            host = os.path.normpath(best[1] + inside[len(best[0]):])
            if os.path.isfile(os.path.join(host, "pack.toml")):
                return host, "PACKWIZ_URL mount"
    if os.path.isfile(os.path.join(sdir, "pack.toml")):
        return sdir, "server directory"
    if server == "minecraft-crossplay" and os.path.isfile(os.path.join(LEGACY_PACK, "pack.toml")):
        return LEGACY_PACK, "legacy default"
    return None, "none"


def self_resolved(server: str) -> bool:
    """True when nothing but us turns pack metadata into jars.

    With PACKWIZ_URL set the itzg image runs packwiz-installer on every start.
    Without it, a .pw.toml is only metadata and the jars have to be fetched
    (and the superseded ones retired) here.
    """
    return not container_env(server).get("PACKWIZ_URL")


def packwiz_bin() -> str | None:
    """The manager's own build first, then PATH. packwiz is NOT on PATH on this
    host, which the old `["packwiz", ...]` call would only have discovered
    after the compose file had already been rewritten."""
    mine = os.path.join(DATA_DIR, "_bin", "packwiz")
    if os.path.isfile(mine) and os.access(mine, os.X_OK):
        return mine
    return shutil.which("packwiz")


def detect_loader(server: str, given: str | None) -> str:
    if given:
        return given
    return LOADERS.get(container_env(server).get("TYPE", "").upper(), "fabric")


# ===========================================================================
# Version discovery
# ===========================================================================
def mojang_manifest() -> dict | None:
    """Mojang's version manifest. Same retry shape as api(); stdlib only,
    because the ServerManager venv has no requests/httpx."""
    if MOJANG_MANIFEST in _cache:
        return _cache[MOJANG_MANIFEST]
    req = urllib.request.Request(MOJANG_MANIFEST, headers={"User-Agent": UA})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            _cache[MOJANG_MANIFEST] = data
            return data
        except Exception:
            time.sleep(2)
    return None


def latest_versions() -> tuple[str | None, str | None]:
    m = mojang_manifest()
    if not m:
        return None, None
    latest = m.get("latest") or {}
    return latest.get("release"), latest.get("snapshot")


def current_version(server: str) -> str | None:
    """Installed MC version: fabric manifest first, then the versions/ dir,
    then whatever the container was told to run.

    The first two work with the server stopped — which is exactly when an
    update is being planned.
    """
    sdir = os.path.join(DATA_DIR, server)
    fab = os.path.join(sdir, ".fabric-manifest.json")
    if os.path.isfile(fab):
        try:
            with open(fab, encoding="utf-8") as f:
                d = json.load(f)
            game = ((d.get("origin") or {}).get("game"))
            if game:
                return game
        except Exception:
            pass
    vdir = os.path.join(sdir, "versions")
    if os.path.isdir(vdir):
        entries = [e for e in os.listdir(vdir) if not e.startswith(".")]
        if len(entries) == 1:
            return entries[0]
    v = container_env(server).get("VERSION", "")
    if v and v.upper() not in ("LATEST", "SNAPSHOT"):
        return v
    return None


# ===========================================================================
# Pack reading
# ===========================================================================
def read_pack(pack_dir: str | None) -> list[dict]:
    """Parse the pack's mods/*.pw.toml. A pack with no mods is an empty list,
    not an error — a vanilla server is allowed to update."""
    if not pack_dir:
        return []
    mods_dir = os.path.join(pack_dir, "mods")
    if not os.path.isdir(mods_dir):
        return []
    out = []
    for fn in sorted(os.listdir(mods_dir)):
        if not fn.endswith(".pw.toml"):
            continue
        path = os.path.join(mods_dir, fn)
        try:
            with open(path, "rb") as f:
                doc = tomllib.load(f)
        except (OSError, ValueError) as e:
            warn(f"{fn}: unreadable pack file ({e}) — skipped")
            continue
        dl = doc.get("download") or {}
        mr = ((doc.get("update") or {}).get("modrinth") or {})
        out.append({
            "file": fn,
            "meta": os.path.join("mods", fn),
            "name": doc.get("name") or fn[:-8],
            "slug": fn[:-8],
            "mod_id": mr.get("mod-id"),
            "version_id": mr.get("version"),
            "filename": doc.get("filename") or "",
            "side": (doc.get("side") or "both").lower(),
            "url": dl.get("url") or "",
            "hash": (dl.get("hash") or "").lower(),
            "hash_format": (dl.get("hash-format") or "sha512").lower(),
            "pinned": bool(doc.get("pin")),
        })
    return out


# ===========================================================================
# Minecraft version predicates (fabric.mod.json "depends.minecraft")
# ===========================================================================
def _ver(s: str) -> list | None:
    core = re.split(r"[-+]", s.strip().lstrip("vV"), maxsplit=1)[0]
    out: list = []
    for p in core.split("."):
        if p in ("x", "X", "*"):
            out.append(None)
        elif p.isdigit():
            out.append(int(p))
        else:
            return None
    return out or None


def _pad(v: list, n: int = 3) -> tuple:
    return tuple((v + [0] * n)[:n])


def _term(term: str, t: tuple) -> bool | None:
    term = term.strip()
    if term in ("", "*"):
        return True
    m = re.match(r"^(>=|<=|>|<|=|~|\^)?\s*(.+)$", term)
    op, raw = m.group(1) or "=", m.group(2)
    v = _ver(raw)
    if v is None:
        return None
    if None in v:                       # 26.2.x — prefix match
        prefix = tuple(v[:v.index(None)])
        return t[:len(prefix)] == prefix
    pv = _pad(v)
    if op == "=":  return t == pv
    if op == ">=": return t >= pv
    if op == ">":  return t > pv
    if op == "<=": return t <= pv
    if op == "<":  return t < pv
    if op == "~":  return t >= pv and t[:2] == pv[:2]
    if op == "^":  return t >= pv and t[:1] == pv[:1]
    return None


def mc_satisfies(pred, target: str) -> bool | None:
    """Does `target` satisfy a Fabric version predicate? None = cannot tell
    (no predicate declared, a snapshot id, or syntax this does not parse)."""
    tv = _ver(target or "")
    if tv is None or None in tv or pred in (None, "", "-", "?"):
        return None
    t = _pad(tv)
    alts = pred if isinstance(pred, list) else [pred]
    results = []
    for alt in alts:                    # list entries are OR'd
        rs = [_term(x, t) for x in str(alt).split()]   # space-separated = AND
        results.append(False if False in rs else (None if None in rs else True))
    if True in results:
        return True
    return None if None in results else False


# ===========================================================================
# Jars packwiz does not track
# ===========================================================================
def jar_meta(path: str) -> tuple[str, object]:
    """(mod id, declared minecraft dependency) from a jar's fabric.mod.json."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for candidate in ("fabric.mod.json", "quilt.mod.json"):
                if candidate in names:
                    meta = json.loads(z.read(candidate).decode("utf-8", "replace"),
                                      strict=False)
                    if candidate == "quilt.mod.json":
                        ql = meta.get("quilt_loader") or {}
                        return ql.get("id", "?"), "?"
                    return meta.get("id", "?"), (meta.get("depends") or {}).get("minecraft", "-")
        return "<no fabric.mod.json>", "?"
    except Exception as e:
        return f"<unreadable: {type(e).__name__}>", "?"


def file_hash(path: str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 18), b""):
            h.update(chunk)
    return h.hexdigest()


def pack_side(project: dict) -> str:
    """Same rule packwiz applies when it writes a Modrinth entry."""
    if project.get("server_side") == "unsupported":
        return "client"
    if project.get("client_side") == "unsupported":
        return "server"
    return "both"


def loose_jars(server: str, pack_dir: str | None, target: str | None = None,
               loader: str = "fabric", identify: bool = True) -> list[dict]:
    """Every jar in the server's mods/ the pack does not list, classified.

    status is one of:
      adoptable    Modrinth knows this exact file; --adopt writes a .pw.toml for
                   it and packwiz manages it from then on.
      superseded   The pack already manages this mod under another jar that is
                   also on disk — this one is a stale duplicate.
      ok           Not on Modrinth, but the jar itself declares a minecraft
                   dependency the target satisfies.
      incompatible Not on Modrinth and its declared dependency excludes the target.
      unknown      Not on Modrinth and it declares nothing usable.
    """
    mods_dir = os.path.join(DATA_DIR, server, "mods")
    if not os.path.isdir(mods_dir):
        return []
    pack = read_pack(pack_dir)
    managed = {m["filename"] for m in pack if m["filename"]}
    pack_projects = {m["mod_id"]: m for m in pack if m["mod_id"]}

    jars = sorted(j for j in os.listdir(mods_dir) if j.endswith(".jar"))
    loose = [j for j in jars if j not in managed]
    if not loose:
        return []

    # Fabric ids of the jars the pack DOES manage and that are really on disk,
    # so a hand-placed second copy of the same mod is recognised as a duplicate
    # even when Modrinth has never seen it.
    managed_ids = {}
    for j in jars:
        if j in managed:
            mid, _ = jar_meta(os.path.join(mods_dir, j))
            if not mid.startswith("<") and mid != "?":
                managed_ids[mid] = j

    sha1 = {j: file_hash(os.path.join(mods_dir, j), "sha1") for j in loose}
    matches: dict = {}
    projects: dict = {}
    if identify:
        res = api("/version_files", body={"hashes": list(sha1.values()), "algorithm": "sha1"})
        matches = res if isinstance(res, dict) else {}
        pids = sorted({v.get("project_id") for v in matches.values() if v.get("project_id")})
        if pids:
            plist = api("/projects", {"ids": json.dumps(pids)}) or []
            projects = {p["id"]: p for p in plist if isinstance(p, dict) and p.get("id")}

    out = []
    for j in loose:
        path = os.path.join(mods_dir, j)
        mod_id, requires = jar_meta(path)
        row = {"jar": j, "mod_id": mod_id, "requires_mc": requires, "sha1": sha1[j],
               "status": "unknown", "detail": ""}
        ver = matches.get(sha1[j])
        pid = (ver or {}).get("project_id")

        twin = None
        if pid and pid in pack_projects:
            twin = pack_projects[pid]["filename"]
        elif mod_id in managed_ids:
            twin = managed_ids[mod_id]
        if twin and os.path.isfile(os.path.join(mods_dir, twin)):
            row.update(status="superseded", duplicate_of=twin,
                       detail=f"duplicate of {twin}, which the pack already manages")
            out.append(row)
            continue

        if ver and pid and pid not in pack_projects:
            proj = projects.get(pid) or {}
            primary = next((f for f in ver.get("files") or []
                            if (f.get("hashes") or {}).get("sha1") == sha1[j]), None)
            row.update(status="adoptable",
                       project_id=pid, version_id=ver.get("id"),
                       slug=proj.get("slug") or pid,
                       title=proj.get("title") or mod_id,
                       side=pack_side(proj),
                       url=(primary or {}).get("url", ""),
                       version_number=ver.get("version_number", ""),
                       detail="on Modrinth — can be adopted into the pack")
            if target:
                tv = compatible_version({"mod_id": pid, "slug": pid}, target, loader)
                row["target_version"] = (tv or {}).get("version_number")
                if not tv:
                    row["detail"] = f"on Modrinth, but no build for {target}/{loader}"
            out.append(row)
            continue

        if ver and pid:
            # The pack lists this project under a different file that is NOT on
            # disk: the pack and the mods folder disagree about what is installed.
            row.update(status="unknown",
                       detail=f"pack expects {pack_projects[pid]['filename']} for this mod "
                              f"but that jar is missing — run 'Download missing jars'")
            out.append(row)
            continue

        if target:
            sat = mc_satisfies(requires, target)
            if sat is True:
                row.update(status="ok",
                           detail=f"not on Modrinth; declares minecraft {requires}, "
                                  f"which {target} satisfies")
            elif sat is False:
                row.update(status="incompatible",
                           detail=f"not on Modrinth; declares minecraft {requires}, "
                                  f"which excludes {target}")
            else:
                row["detail"] = ("not on Modrinth and declares no usable minecraft "
                                 "dependency — compatibility cannot be checked")
        else:
            row["detail"] = "not on Modrinth — packwiz cannot track or update it"
        out.append(row)
    return out


def _q(s: str) -> str:
    """A TOML basic string. JSON string escaping is a subset of TOML's."""
    return json.dumps(str(s), ensure_ascii=False)


def adopt(server: str, pack_dir: str, loose: list[dict],
          retire_duplicates: bool = True) -> dict:
    """Write pack metadata for every adoptable jar and retire stale duplicates.

    The .pw.toml is written directly rather than via `packwiz modrinth add`,
    because add records Modrinth's filename and would then consider the jar
    already on disk (possibly renamed) missing. Recording the name the file
    actually has means nothing is re-downloaded.
    """
    report = {"adopted": [], "retired": [], "skipped": []}
    sdir = os.path.join(DATA_DIR, server)
    mods_dir = os.path.join(sdir, "mods")
    pack_mods = os.path.join(pack_dir, "mods")
    os.makedirs(pack_mods, exist_ok=True)

    resolved_path = os.path.join(sdir, RESOLVED_INDEX)
    resolved = {}
    if os.path.isfile(resolved_path):
        try:
            with open(resolved_path) as f:
                resolved = json.load(f)
        except Exception:
            resolved = {}
    same_dir = os.path.realpath(pack_dir) == os.path.realpath(sdir)

    for j in loose:
        if j["status"] != "adoptable":
            continue
        if not j.get("url") or not j.get("version_id"):
            report["skipped"].append({"jar": j["jar"], "why": "Modrinth gave no download URL"})
            continue
        slug = re.sub(r"[^A-Za-z0-9_.\-]", "-", j["slug"])
        meta = os.path.join(pack_mods, f"{slug}.pw.toml")
        if os.path.exists(meta):
            report["skipped"].append({"jar": j["jar"],
                                      "why": f"{slug}.pw.toml already exists for another mod"})
            continue
        sha512 = file_hash(os.path.join(mods_dir, j["jar"]), "sha512")
        text = (f"name = {_q(j['title'])}\n"
                f"filename = {_q(j['jar'])}\n"
                f"side = {_q(j['side'])}\n\n"
                f"[download]\n"
                f"url = {_q(j['url'])}\n"
                f"hash-format = \"sha512\"\n"
                f"hash = {_q(sha512)}\n\n"
                f"[update]\n[update.modrinth]\n"
                f"mod-id = {_q(j['project_id'])}\n"
                f"version = {_q(j['version_id'])}\n")
        tomllib.loads(text)             # never write a file we could not read back
        tmp = meta + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, meta)
        if same_dir:
            resolved[f"mods/{slug}.pw.toml"] = f"mods/{j['jar']}"
        report["adopted"].append({"jar": j["jar"], "name": j["title"],
                                  "meta": f"mods/{slug}.pw.toml"})
        ok(f"adopted {j['jar']:<44} -> mods/{slug}.pw.toml")

    if retire_duplicates:
        dupes = [j for j in loose if j["status"] == "superseded"]
        if dupes:
            dest = os.path.join(sdir, REPLACED_DIR, stamp())
            os.makedirs(dest, exist_ok=True)
            for j in dupes:
                shutil.move(os.path.join(mods_dir, j["jar"]), os.path.join(dest, j["jar"]))
                report["retired"].append({"jar": j["jar"], "duplicate_of": j.get("duplicate_of"),
                                          "moved_to": os.path.relpath(dest, sdir)})
                ok(f"retired duplicate {j['jar']} (kept in {os.path.relpath(dest, sdir)}/)")

    if same_dir and report["adopted"]:
        with open(resolved_path, "w") as f:
            json.dump(resolved, f, indent=2, sort_keys=True)

    if report["adopted"]:
        pw = packwiz_bin()
        if pw:
            ensure_index(pack_dir)
            r = run([pw, "refresh"], cwd=pack_dir, timeout=600)
            if r.returncode != 0:
                warn(f"packwiz refresh failed: {(r.stderr or r.stdout)[-300:]}")
        else:
            warn("packwiz not found — run `packwiz refresh` in the pack to rebuild its index")
    return report


# ===========================================================================
# Compatibility gate
# ===========================================================================
def compatible_version(mod: dict, mc_version: str, loader: str) -> dict | None:
    """Newest Modrinth version of `mod` supporting mc_version+loader, or None."""
    ident = mod["mod_id"] or mod["slug"]
    versions = api(f"/project/{ident}/version", {
        "game_versions": json.dumps([mc_version]),
        "loaders": json.dumps([loader]),
    })
    if not versions:
        return None
    # API filtering is not always exact; verify the returned metadata.
    for v in versions:
        if mc_version in v.get("game_versions", []) and loader in v.get("loaders", []):
            return v
    return None


def check_compat(pack_dir: str | None, mc_version: str, loader: str,
                 server: str | None = None,
                 allow_loose: bool = False) -> tuple[bool, list, list, list]:
    mods = read_pack(pack_dir)
    results, blockers = [], []
    resolved_ids = set()

    if not mods:
        ok("pack lists no mods — nothing to resolve")
    else:
        _out(f"  checking {len(mods)} mods against MC {mc_version} / {loader}\n")

    for m in mods:
        v = compatible_version(m, mc_version, loader)
        if v:
            resolved_ids.add(v.get("project_id"))
            results.append({"mod": m, "version": v, "status": "ok"})
            ok(f"{m['name']:<38} {v.get('version_number','?')}")
        else:
            results.append({"mod": m, "version": None, "status": "incompatible"})
            blockers.append(f"{m['name']}: no build for {mc_version}/{loader}")
            bad(f"{m['name']:<38} no compatible build")

    # Jars packwiz does not manage — classified before the dependency pass, so
    # a dependency that is satisfied by an adoptable jar is not reported missing.
    loose = loose_jars(server, pack_dir, mc_version, loader) if server else []
    adoptable_ids = {j["project_id"] for j in loose
                     if j["status"] == "adoptable" and j.get("target_version")}

    # Required dependencies must also resolve, or the pack will crash on boot
    # with a missing-dependency error rather than a clean message.
    if mods:
        step("Required dependencies")
    dep_problems = []
    for r in results:
        if r["status"] != "ok":
            continue
        for dep in (r["version"].get("dependencies") or []):
            if dep.get("dependency_type") != "required":
                continue
            pid = dep.get("project_id")
            if not pid or pid in resolved_ids or pid in adoptable_ids:
                continue
            proj = api(f"/project/{pid}")
            dname = proj.get("title", pid) if proj else pid
            dv = api(f"/project/{pid}/version", {
                "game_versions": json.dumps([mc_version]),
                "loaders": json.dumps([loader]),
            })
            if dv:
                warn(f"{r['mod']['name']} needs {dname} — compatible, but NOT in your pack")
                dep_problems.append(f"{r['mod']['name']} requires {dname} (missing from pack)")
            else:
                bad(f"{r['mod']['name']} needs {dname} — no build for {mc_version}")
                dep_problems.append(f"{r['mod']['name']} requires {dname}, which has no {mc_version} build")
    if mods and not dep_problems:
        ok("all required dependencies satisfied")
    blockers.extend(dep_problems)

    if loose:
        step("Jars packwiz does not track")
        loose_blockers = []
        for j in loose:
            st = j["status"]
            if st == "adoptable" and j.get("target_version"):
                ok(f"{j['jar']:<44} on Modrinth, {mc_version} build {j['target_version']} "
                   f"— will be adopted and updated")
            elif st == "adoptable":
                bad(f"{j['jar']:<44} on Modrinth, but no {mc_version}/{loader} build")
                loose_blockers.append(f"{j['title']} ({j['jar']}): no build for {mc_version}/{loader}")
            elif st == "superseded":
                warn(f"{j['jar']:<44} {j['detail']} — will be moved to {REPLACED_DIR}/")
            elif st == "ok":
                warn(f"{j['jar']:<44} {j['detail']}; packwiz will NOT update it")
            else:
                bad(f"{j['jar']:<44} {j['detail']}")
                loose_blockers.append(
                    f"{j['jar']} is not managed by packwiz and is not on Modrinth "
                    f"(mod-id {j['mod_id']}, requires MC {j['requires_mc']}) — {j['detail']}")
        if loose_blockers and allow_loose:
            warn("--allow-loose-jars given; continuing despite the jars above.")
        else:
            blockers.extend(loose_blockers)
    elif server:
        ok("every jar in mods/ is tracked by the pack")

    report = [{
        "name": r["mod"]["name"],
        "slug": r["mod"]["slug"],
        "status": r["status"],
        "current_file": r["mod"]["filename"],
        "target_version": (r["version"] or {}).get("version_number"),
    } for r in results]
    return (len(blockers) == 0), blockers, report, loose


# ===========================================================================
# Backup
# ===========================================================================
def verified_backup(server: str) -> str:
    step("Backup (verified before proceeding)")
    if not os.path.isfile(BACKUP_SCRIPT):
        die(f"missing {BACKUP_SCRIPT}")
    if not os.path.isdir(os.path.dirname(BORG_REPO)) and not os.path.isdir(BORG_REPO):
        die(f"backup destination {BORG_REPO} is not there — is the disk mounted? "
            f"Refusing to update without a backup.")
    r = run([BACKUP_SCRIPT, server], timeout=7200)
    if r.returncode != 0:
        die(f"backup failed, refusing to update:\n{r.stdout[-1500:]}{r.stderr[-1500:]}")

    env = {**os.environ, "BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK": "yes", "BORG_PASSPHRASE": ""}
    lst = run(["borg", "list", "--format", "{archive}{NL}", BORG_REPO], env=env)
    archives = [a for a in lst.stdout.split() if a.startswith(f"{server}-")]
    if not archives:
        die("backup reported success but no archive is present — aborting")
    latest = archives[-1]
    ok(f"archive created: {latest}")

    # A backup you have not read back is a hypothesis, not a backup.
    chk = run(["borg", "list", f"{BORG_REPO}::{latest}"], env=env)
    n = len(chk.stdout.splitlines())
    if chk.returncode != 0 or n < 20:
        die(f"archive unreadable or implausibly small ({n} entries) — aborting")
    ok(f"archive readable, {n} entries")
    return latest


# ===========================================================================
# Config edits
# ===========================================================================
def _service_span(lines: list[str], service: str) -> tuple[int, int]:
    """[start, end) line span of one service block in the compose file.

    The file defines three minecraft services plus an x-mc-common anchor, so a
    blanket VERSION replace would rewrite the wrong servers.
    """
    start = None
    for i, ln in enumerate(lines):
        if re.match(rf"^  {re.escape(service)}:\s*(#.*)?$", ln):
            start = i
            break
    if start is None:
        return -1, -1
    for j in range(start + 1, len(lines)):
        if lines[j].strip() and not lines[j].startswith("   ") and not lines[j].startswith("\t"):
            return start, j
    return start, len(lines)


def _write_atomic(path: str, text: str) -> None:
    """Replace a file in one step, so a crash never leaves it half-written."""
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        shutil.copymode(path, tmp)
    except OSError:
        pass
    os.replace(tmp, path)


def compose_version(service: str) -> str | None:
    if not os.path.isfile(COMPOSE):
        return None
    lines = open(COMPOSE, encoding="utf-8").read().splitlines(keepends=True)
    s, e = _service_span(lines, service)
    if s < 0:
        return None
    for ln in lines[s:e]:
        m = re.match(r'^\s*VERSION:\s*"?([^"\s#]+)"?', ln)
        if m:
            return m.group(1)
    return None


def set_compose_version(service: str, version: str) -> str:
    """Rewrite VERSION inside one service block. Returns the previous value."""
    lines = open(COMPOSE, encoding="utf-8").read().splitlines(keepends=True)
    s, e = _service_span(lines, service)
    if s < 0:
        raise RuntimeError(f"service '{service}' not found in {COMPOSE}")
    for i in range(s, e):
        m = re.match(r'^(\s*VERSION:\s*)"?([^"\s#]+)"?(\s*(?:#.*)?)$', lines[i])
        if m:
            old = m.group(2)
            lines[i] = f'{m.group(1)}"{version}"{m.group(3).rstrip()}\n'
            _write_atomic(COMPOSE, "".join(lines))
            return old
    raise RuntimeError(f"no VERSION key inside service '{service}'")


def set_pack_version(pack_dir: str, version: str) -> str | None:
    """Rewrite [versions] minecraft in pack.toml. Returns the previous value."""
    path = os.path.join(pack_dir, "pack.toml")
    if not os.path.isfile(path):
        return None
    text = open(path, encoding="utf-8").read()
    m = re.search(r'^(minecraft\s*=\s*)"([^"]+)"', text, re.M)
    if not m:
        return None
    old = m.group(2)
    _write_atomic(path, text[:m.start()] + f'{m.group(1)}"{version}"' + text[m.end():])
    return old


# ===========================================================================
# Snapshots — what an update is allowed to change, captured first
# ===========================================================================
def _pack_meta_files(pack_dir: str) -> list[str]:
    rels = [f for f in ("pack.toml", "index.toml") if os.path.isfile(os.path.join(pack_dir, f))]
    mods = os.path.join(pack_dir, "mods")
    if os.path.isdir(mods):
        rels += [os.path.join("mods", f) for f in sorted(os.listdir(mods))
                 if f.endswith(".pw.toml")]
    return rels


def snapshot_pack(server: str, pack_dir: str | None, extra: dict | None = None) -> dict:
    """Capture pack metadata in memory AND on disk.

    In memory for the automatic rollback; on disk (a small tar under
    _update_snapshots/) so there is still something to restore from by hand if
    the updater itself is killed halfway.
    """
    snap = {"pack_dir": pack_dir, "files": {}, "tar": None}
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    tar_path = os.path.join(SNAPSHOT_DIR, f"{server}-{stamp()}.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        if pack_dir:
            for rel in _pack_meta_files(pack_dir):
                full = os.path.join(pack_dir, rel)
                with open(full, "rb") as f:
                    snap["files"][rel] = f.read()
                tar.add(full, arcname=os.path.join("pack", rel))
        for name, text in (extra or {}).items():
            tmp = os.path.join(SNAPSHOT_DIR, f".{name}.{os.getpid()}")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            tar.add(tmp, arcname=name)
            os.remove(tmp)
    snap["tar"] = tar_path
    mine = sorted(f for f in os.listdir(SNAPSHOT_DIR)
                  if f.startswith(f"{server}-") and f.endswith(".tar.gz"))
    for old in mine[:-SNAPSHOT_KEEP]:
        try:
            os.remove(os.path.join(SNAPSHOT_DIR, old))
        except OSError:
            pass
    return snap


def restore_pack(snap: dict) -> None:
    pack_dir = snap.get("pack_dir")
    if not pack_dir:
        return
    for rel in _pack_meta_files(pack_dir):
        if rel not in snap["files"]:
            os.remove(os.path.join(pack_dir, rel))      # added during the update
    for rel, data in snap["files"].items():
        full = os.path.join(pack_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)


# ===========================================================================
# Turning pack metadata into jars (servers without PACKWIZ_URL)
# ===========================================================================
def download(entry: dict, dest: str) -> str:
    """Fetch one jar, verifying its hash. Returns '' on success, else why not."""
    url = entry["url"]
    if not url.startswith("https://"):
        return "download URL is not https"
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in DOWNLOAD_HOSTS:
        return f"refusing to download from an unexpected host ({host})"
    algo = entry["hash_format"].replace("-", "")
    if algo not in hashlib.algorithms_available:
        return f"unsupported hash format ({entry['hash_format']})"
    tmp = dest + ".part"
    last = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            total = 0
            with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > DOWNLOAD_MAX:
                        raise ValueError("file exceeds the size limit")
                    out.write(chunk)
            if entry["hash"] and file_hash(tmp, algo) != entry["hash"]:
                last = f"{algo} mismatch — the file does not match what the pack expects"
                os.remove(tmp)
                continue
            os.replace(tmp, dest)       # only now does the server see it
            return ""
        except Exception as e:
            last = f"download failed: {str(e)[:160]}"
            if os.path.exists(tmp):
                os.remove(tmp)
            time.sleep(2 * (attempt + 1))
    return last


def ensure_index(pack_dir: str) -> None:
    """packwiz refuses to do anything ("open index.toml: no such file") when
    the index a pack.toml points at has gone missing. An empty index is valid,
    and refresh rebuilds it from the files on disk."""
    try:
        with open(os.path.join(pack_dir, "pack.toml"), "rb") as f:
            rel = ((tomllib.load(f).get("index") or {}).get("file")) or "index.toml"
    except (OSError, ValueError):
        return
    path = os.path.join(pack_dir, rel)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write('hash-format = "sha256"\n')
        warn(f"{rel} was missing — recreated; packwiz refresh will rebuild it")


def packwiz_update(pack_dir: str) -> None:
    pw = packwiz_bin()
    if not pw:
        raise RuntimeError("packwiz is not installed (looked in _bin/ and PATH)")
    ensure_index(pack_dir)
    for args in (["update", "--all", "-y"], ["refresh"]):
        r = run([pw] + args, cwd=pack_dir, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f"packwiz {' '.join(args)} failed: {(r.stderr or r.stdout)[-800:]}")
        ok("packwiz " + " ".join(args))


def swap_jars(server: str, pack_dir: str, before: list[dict], record: dict) -> list[dict]:
    """After packwiz rewrote the metadata: fetch the new jars, retire the old.

    Order matters. Every new jar is downloaded and hash-verified BEFORE any old
    one is moved, so a failed download leaves the mods folder exactly as it was.
    `record` is filled in as things happen so a rollback can undo precisely
    what was done and nothing else.
    """
    sdir = os.path.join(DATA_DIR, server)
    mods_dir = os.path.join(sdir, "mods")
    os.makedirs(mods_dir, exist_ok=True)
    old_by_meta = {m["meta"]: m for m in before}
    changes = []

    for m in read_pack(pack_dir):
        if m["side"] == "client" or not m["filename"]:
            continue
        dest = os.path.join(mods_dir, m["filename"])
        prev = old_by_meta.get(m["meta"])
        if (os.path.isfile(dest) and prev and m["hash"] and prev["hash"] != m["hash"]
                and m["hash_format"].replace("-", "") in hashlib.algorithms_available
                and file_hash(dest, m["hash_format"].replace("-", "")) != m["hash"]):
            # A new build published under the SAME filename (voxy-server-side-
            # fabric.jar does this). The name matching says nothing; the old
            # file has to be set aside so the new one can take its place.
            aside = os.path.join(sdir, REPLACED_DIR, stamp() + "-samename")
            os.makedirs(aside, exist_ok=True)
            kept = os.path.join(aside, m["filename"])
            shutil.move(dest, kept)
            record["moved"].append((dest, kept))
            changes.append({"name": m["name"], "from": m["filename"],
                            "to": m["filename"] + " (new build, same name)"})
        if not os.path.isfile(dest):
            err = download(m, dest)
            if err:
                raise RuntimeError(f"{m['name']}: {err}")
            record["new"].append(dest)
            ok(f"downloaded {m['filename']}")
        if prev and prev["filename"] and prev["filename"] != m["filename"]:
            changes.append({"name": m["name"], "from": prev["filename"], "to": m["filename"]})

    if changes:
        retire = os.path.join(sdir, REPLACED_DIR, stamp())
        os.makedirs(retire, exist_ok=True)
        for c in changes:
            if c["to"].startswith(c["from"]):
                continue                    # same-name swap, already handled
            src = os.path.join(mods_dir, c["from"])
            if os.path.isfile(src):
                dst = os.path.join(retire, c["from"])
                shutil.move(src, dst)
                record["moved"].append((src, dst))

    resolved_path = os.path.join(sdir, RESOLVED_INDEX)
    if os.path.realpath(pack_dir) == os.path.realpath(sdir):
        try:
            with open(resolved_path) as f:
                resolved = json.load(f)
        except Exception:
            resolved = {}
        for m in read_pack(pack_dir):
            if m["meta"] in resolved or any(c["to"].startswith(m["filename"]) for c in changes):
                resolved[m["meta"]] = f"mods/{m['filename']}"
        with open(resolved_path, "w") as f:
            json.dump(resolved, f, indent=2, sort_keys=True)
    return changes


def undo_jars(record: dict) -> None:
    for path in record.get("new", []):
        try:
            os.remove(path)
        except OSError:
            pass
    for src, dst in reversed(record.get("moved", [])):
        try:
            shutil.move(dst, src)
        except OSError as e:
            warn(f"could not move {dst} back: {e}")


def metadata_changes(before: list[dict], pack_dir: str) -> list[dict]:
    old = {m["meta"]: m for m in before}
    return [{"name": m["name"], "from": old[m["meta"]]["filename"], "to": m["filename"]}
            for m in read_pack(pack_dir)
            if m["meta"] in old and old[m["meta"]]["filename"] != m["filename"]]


# ===========================================================================
# Recreating a container that was started with `docker run`
# ===========================================================================
def run_args(info: dict, env_overrides: dict) -> list[str]:
    """Rebuild the `docker run` argv for an existing container, with env changes.

    Only what was set on the container is carried: env the image itself
    provides is left out, so a new image's defaults are not pinned to the old
    image's values.
    """
    cfg, hc = info["Config"], info["HostConfig"]
    name = info["Name"].lstrip("/")
    img = {}
    try:
        r = run(["docker", "image", "inspect", info["Image"]], timeout=30)
        img = (json.loads(r.stdout)[0].get("Config") or {}) if r.returncode == 0 else {}
    except Exception:
        img = {}
    img_env = set(img.get("Env") or [])
    img_labels = img.get("Labels") or {}

    argv = ["docker", "run", "-d", "--name", name]
    if cfg.get("Tty"):
        argv.append("-t")
    if cfg.get("OpenStdin"):
        argv.append("-i")

    for cport, binds in sorted((hc.get("PortBindings") or {}).items()):
        for b in binds or []:
            ip = b.get("HostIp") or ""
            hp = b.get("HostPort") or ""
            argv += ["-p", f"{ip + ':' if ip else ''}{hp}:{cport}"]

    seen = set()
    for line in cfg.get("Env") or []:
        k = line.split("=", 1)[0]
        if k in env_overrides:
            if k not in seen:
                argv += ["-e", f"{k}={env_overrides[k]}"]
                seen.add(k)
        elif line not in img_env:
            argv += ["-e", line]
    for k, v in env_overrides.items():
        if k not in seen:
            argv += ["-e", f"{k}={v}"]

    for bind in hc.get("Binds") or []:
        argv += ["-v", bind]

    rp = hc.get("RestartPolicy") or {}
    if rp.get("Name") and rp["Name"] != "no":
        n = rp.get("MaximumRetryCount") or 0
        argv += ["--restart", f"{rp['Name']}:{n}" if rp["Name"] == "on-failure" and n else rp["Name"]]

    if hc.get("Memory"):
        argv += ["--memory", str(hc["Memory"])]
        if hc.get("MemorySwap") and hc["MemorySwap"] > 0:
            argv += ["--memory-swap", str(hc["MemorySwap"])]
    # `docker update --cpu-quota -1` lifts a CPU cap but leaves NanoCpus stale,
    # so a quota of -1 has to win over whatever NanoCpus still says.
    if hc.get("CpuQuota") == -1:
        pass
    elif hc.get("NanoCpus"):
        argv += ["--cpus", f"{hc['NanoCpus'] / 1e9:g}"]
    elif (hc.get("CpuQuota") or 0) > 0:
        argv += ["--cpu-quota", str(hc["CpuQuota"])]
        if hc.get("CpuPeriod"):
            argv += ["--cpu-period", str(hc["CpuPeriod"])]

    net = hc.get("NetworkMode") or ""
    if net and net not in ("default", "bridge"):
        argv += ["--network", net]

    for k, v in sorted((cfg.get("Labels") or {}).items()):
        if img_labels.get(k) != v:
            argv += ["--label", f"{k}={v}"]

    argv.append(cfg["Image"])
    return argv


# ===========================================================================
# Apply
# ===========================================================================
# Anything here means the server will not come up. The datapack/registry
# entries are not theoretical: the 26.2 upgrade failed on
# "Failed to parse deep_nether:lake_lava_deep from pack file/deep_nether"
# and the original mod-only pattern sailed straight past it and reported
# success. A boot failure is a boot failure whatever subsystem raised it.
BOOT_ERROR_PAT = re.compile(
    # mod loading
    r"Incompatible mod set|Incompatible mods found|Mod resolution failed|"
    r"Some of your mods are incompatible|but only the wrong version is present|"
    r"Missing or unsupported mand|could not be loaded|Failed to load mods|"
    r"java\.lang\.NoSuchMethodError|Mixin apply.*failed|"
    r"Caused by: .*ClassNotFoundException|"
    # datapacks / registries
    r"Failed to load datapacks|can't proceed with server load|"
    r"Failed to load registries|Registry loading errors|"
    r"Unbound values in registry|Failed to parse .* from pack|"
    # generic fatal
    r"ReportedException|Encountered an unexpected exception|"
    r"Failed to start the minecraft server", re.I)

MOD_ERROR_PAT = BOOT_ERROR_PAT   # back-compat alias

# Once any of these has been logged the new version has opened the world, and
# putting the old version back on top of it is no longer automatically safe.
WORLD_TOUCHED_PAT = re.compile(
    r"Preparing level|Preparing start region|Preparing spawn area|Loading level|"
    r"Time elapsed: \d+ ms|Done \(", re.I)


def apply_update(server: str, pack_dir: str | None, target: str, archive: str,
                 boot_timeout: int = 600, do_adopt: bool = True,
                 loose: list[dict] | None = None) -> dict:
    """Bump config, pull the new mod set, restart, and watch the boot."""
    service = compose_service_of(server)
    info = inspect(server, fresh=True)
    if not info:
        return {"ok": False, "error": f"container '{server}' not found", "rolled_back": False,
                "archive": archive}
    resolves = self_resolved(server)

    # ---- preflight: fail here, before anything has been changed ----------
    step("Preflight")
    if pack_dir and not packwiz_bin():
        return {"ok": False, "rolled_back": False, "archive": archive,
                "error": "packwiz is not installed (looked in _bin/ and PATH)"}
    if run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30).returncode != 0:
        return {"ok": False, "rolled_back": False, "archive": archive,
                "error": "cannot talk to the docker daemon"}
    compose_text = None
    if service:
        if not os.path.isfile(COMPOSE) or not os.access(COMPOSE, os.W_OK):
            return {"ok": False, "rolled_back": False, "archive": archive,
                    "error": f"{COMPOSE} is missing or not writable"}
        compose_text = open(COMPOSE, encoding="utf-8").read()
        if compose_version(service) is None:
            return {"ok": False, "rolled_back": False, "archive": archive,
                    "error": f"no VERSION key for service '{service}' in {COMPOSE}"}
    old_argv = None if service else run_args(info, {})
    new_argv = None if service else run_args(info, {"VERSION": target})
    ok("compose-managed" if service else "docker-run container (will be recreated with the same settings)")

    extra = {"container.json": json.dumps(info, indent=2)}
    if compose_text is not None:
        extra[os.path.basename(COMPOSE)] = compose_text
    if old_argv:
        extra["docker-run.txt"] = " ".join(map(_shq, old_argv)) + "\n"
    snap = snapshot_pack(server, pack_dir, extra)
    ok(f"snapshot of pack metadata + container definition: {snap['tar']}")

    state = {"compose": False, "restarted": False, "jars": {"new": [], "moved": []},
             "adopted": []}

    def container_logs() -> str:
        r = run(["docker", "logs", "--tail", "600", server], timeout=60)
        return r.stdout + r.stderr

    def undo(why: str) -> dict:
        warn(f"rolling back: {why}")
        world_touched = False
        notes = []
        try:
            if state["restarted"]:
                world_touched = bool(WORLD_TOUCHED_PAT.search(container_logs()))
                run(["docker", "stop", "-t", "90", server], timeout=180)
        except Exception as e:
            notes.append(f"stopping the container failed: {e}")
        for label, fn in (
            ("compose file", lambda: state["compose"] and _write_atomic(COMPOSE, compose_text)),
            ("mod jars", lambda: undo_jars(state["jars"])),
            ("pack metadata", lambda: restore_pack(snap)),
        ):
            try:
                fn()
            except Exception as e:
                notes.append(f"restoring {label} failed: {e}")
                bad(f"restoring {label} failed: {e} — recover it from {snap['tar']}")
        for a in state["adopted"]:
            # The .pw.toml went with the pack restore; the jar itself never moved.
            pass

        restarted_old = False
        if state["restarted"]:
            try:
                if service:
                    if not world_touched:
                        r = run(["docker", "compose", "-f", COMPOSE, "up", "-d",
                                 "--force-recreate", service], timeout=600)
                        restarted_old = r.returncode == 0
                else:
                    run(["docker", "rm", "-f", server], timeout=120)
                    argv = list(old_argv)
                    if world_touched:
                        argv[1] = "create"
                        argv.remove("-d")
                    r = run(argv, timeout=300)
                    if r.returncode != 0:
                        notes.append(f"recreating the old container failed: {r.stderr[-300:]}")
                        bad("could not recreate the old container. Its exact command is in "
                            f"docker-run.txt inside {snap['tar']}")
                    restarted_old = r.returncode == 0 and not world_touched
            except Exception as e:
                notes.append(f"restarting the previous version failed: {e}")

        bad("configuration restored to the previous version.")
        if restarted_old:
            ok("the new version never opened the world, so the server was started "
               "again on the previous version.")
        elif state["restarted"]:
            warn("the server is STOPPED. The new version had already opened the world, "
                 "so it was not started on the old version automatically.")
            _out("      to keep the world as it is now:  "
                 + (f"docker compose -f {COMPOSE} up -d {service}" if service
                    else f"docker start {server}"))
            _out(f"      to go back to before the update: borg extract {BORG_REPO}::{archive}")
        else:
            ok("the server was never restarted — it is still running the previous version.")
        return {"ok": False, "error": why, "rolled_back": True, "archive": archive,
                "snapshot": snap["tar"], "server_running": restarted_old or not state["restarted"],
                "world_touched": world_touched, "rollback_notes": notes}

    try:
        step("Applying")
        if service:
            prev = set_compose_version(service, target)
            state["compose"] = True
            ok(f"compose VERSION {prev} -> {target}")

        changes = []
        if pack_dir:
            if do_adopt and loose:
                rep = adopt(server, pack_dir, loose)
                state["adopted"] = rep["adopted"]
                # Retired duplicates are moves like any other: undo puts them back.
                sdir = os.path.join(DATA_DIR, server)
                for r_ in rep["retired"]:
                    state["jars"]["moved"].append(
                        (os.path.join(sdir, "mods", r_["jar"]),
                         os.path.join(sdir, r_["moved_to"], r_["jar"])))
            before = read_pack(pack_dir)
            prev_pack = set_pack_version(pack_dir, target)
            if prev_pack:
                ok(f"pack.toml minecraft {prev_pack} -> {target}")
            step("packwiz")
            packwiz_update(pack_dir)
            if resolves:
                step("Mod jars")
                changes = swap_jars(server, pack_dir, before, state["jars"])
            else:
                changes = metadata_changes(before, pack_dir)
            for c in changes:
                ok(f"{c['name']}: {c['from']} -> {c['to']}")

        step("Restart")
        if service:
            state["restarted"] = True
            r = run(["docker", "compose", "-f", COMPOSE, "up", "-d", "--force-recreate", service],
                    timeout=900)
            if r.returncode != 0:
                return undo(f"compose up failed: {(r.stderr or r.stdout)[-800:]}")
        else:
            state["restarted"] = True
            run(["docker", "stop", "-t", "120", server], timeout=240)
            run(["docker", "rm", "-f", server], timeout=120)
            r = run(new_argv, timeout=600)
            if r.returncode != 0:
                return undo(f"docker run failed: {(r.stderr or r.stdout)[-800:]}")
        ok("container recreated")

        step("Boot watch")
        deadline = time.time() + boot_timeout
        started = False
        restarts0 = run(["docker", "inspect", "-f", "{{.RestartCount}}", server]).stdout.strip()
        while time.time() < deadline:
            time.sleep(10)
            alive = run(["docker", "inspect", "-f", "{{.State.Running}}", server]).stdout.strip()
            text = container_logs()
            errors = [l for l in text.splitlines() if BOOT_ERROR_PAT.search(l)]
            if errors:
                return undo("boot errors in container log:\n      " + "\n      ".join(errors[:6]))
            if alive != "true":
                return undo("container exited during boot; see `docker logs " + server + "`")
            # A climbing restart count means it is crash-looping, which otherwise
            # looks identical to "still booting" for as long as the watch runs.
            rc = run(["docker", "inspect", "-f", "{{.RestartCount}}", server]).stdout.strip()
            if rc != restarts0:
                return undo(f"container is restarting ({restarts0} -> {rc}); it is "
                            f"crash-looping, not booting")
            if 'Done (' in text or "For help, type" in text:
                started = True
                break
    except KeyboardInterrupt:
        return undo("interrupted")
    except Exception as e:
        # Previously an exception here (packwiz missing, a timeout, a full
        # disk) escaped with the compose file already rewritten and nothing
        # put back.
        return undo(f"{type(e).__name__}: {e}")

    base = {"target": target, "archive": archive, "snapshot": snap["tar"],
            "mod_changes": changes, "adopted": state["adopted"]}
    if started:
        ok(f"server booted on {target}")
        return {**base, "ok": True, "booted": True}

    # Never reported Done. Do NOT roll back: the container is alive and a first
    # boot that upgrades a multi-GB world legitimately takes a long time, so
    # tearing it down mid-upgrade would be worse than waiting. But this is not
    # success either, and reporting it as success is exactly how the 26.2
    # datapack failure got waved through.
    bad(f"server did not report 'Done' within {boot_timeout}s and is still running.")
    warn("NOT rolled back — the container is alive and may still be upgrading "
         "the world. Verify before trusting it:")
    _out(f"      docker logs -f {server}")
    _out(f"      docker exec {server} rcon-cli list")
    _out(f"      roll back with: borg extract {BORG_REPO}::{archive}")
    return {**base, "ok": False, "booted": False, "rolled_back": False,
            "error": f"boot not confirmed within {boot_timeout}s"}


def _shq(s: str) -> str:
    return s if re.match(r"^[A-Za-z0-9_./:=@%+,-]+$", s) else "'" + s.replace("'", "'\\''") + "'"


def update_mods(server: str, pack_dir: str, do_adopt: bool = True) -> dict:
    """Newest build of every mod for the CURRENT Minecraft version.

    Does not restart the server: the new jars sit in mods/ and load on the
    next start, which is the admin's call to make.
    """
    if not packwiz_bin():
        return {"ok": False, "error": "packwiz is not installed (looked in _bin/ and PATH)"}
    resolves = self_resolved(server)
    snap = snapshot_pack(server, pack_dir)
    ok(f"snapshot of pack metadata: {snap['tar']}")
    record = {"new": [], "moved": []}
    adopted, retired = [], []
    try:
        if do_adopt:
            step("Jars packwiz does not track")
            loose = loose_jars(server, pack_dir)
            if not loose:
                ok("none")
            rep = adopt(server, pack_dir, loose)
            adopted, retired = rep["adopted"], rep["retired"]
            sdir = os.path.join(DATA_DIR, server)
            for r_ in retired:
                record["moved"].append((os.path.join(sdir, "mods", r_["jar"]),
                                        os.path.join(sdir, r_["moved_to"], r_["jar"])))
            for j in loose:
                if j["status"] not in ("adoptable", "superseded"):
                    warn(f"{j['jar']}: {j['detail']}")
        before = read_pack(pack_dir)
        step("packwiz")
        packwiz_update(pack_dir)
        if resolves:
            step("Mod jars")
            changes = swap_jars(server, pack_dir, before, record)
        else:
            changes = metadata_changes(before, pack_dir)
    except (Exception, KeyboardInterrupt) as e:
        warn(f"rolling back: {type(e).__name__}: {e}")
        undo_jars(record)
        try:
            restore_pack(snap)
        except Exception as e2:
            bad(f"restoring pack metadata failed: {e2} — recover it from {snap['tar']}")
        bad("mods folder and pack restored to how they were.")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "rolled_back": True,
                "snapshot": snap["tar"]}

    if changes:
        for c in changes:
            ok(f"{c['name']}: {c['from']} -> {c['to']}")
    else:
        ok("every mod is already on its newest build for this version")
    running = ((inspect(server, fresh=True) or {}).get("State") or {}).get("Running")
    if changes and resolves:
        warn(f"old jars kept in {server}/{REPLACED_DIR}/ — "
             + ("RESTART the server to load the new ones." if running
                else "they load on the next start."))
    elif changes:
        warn("this server installs mods from the pack on start (PACKWIZ_URL) — "
             "restart it to pull the new builds.")
    return {"ok": True, "mod_changes": changes, "adopted": adopted, "retired": retired,
            "snapshot": snap["tar"], "restart_needed": bool(changes) and bool(running)}


# ===========================================================================
# Chunk regeneration (delegated)
# ===========================================================================
CHUNKDIFF = os.path.join(DATA_DIR, "mc-chunkdiff.py")


def regen(argv: list[str]) -> None:
    """Hand off to mc-chunkdiff.py.

    The old implementation queried InhabitedTime through mcaselector. That
    measures how long players STOOD in a chunk, not whether they changed it,
    so it deleted quick builds and kept idled-in empty land. mc-chunkdiff.py
    compares each chunk against the same chunk regenerated from the same seed
    instead, which is the actual question.
    """
    if not os.path.isfile(CHUNKDIFF):
        die(f"missing {CHUNKDIFF}")
    sys.exit(subprocess.run([sys.executable, CHUNKDIFF] + argv).returncode)


# ===========================================================================
def hold_lock(server: str):
    """One mutating run per server at a time, across the CLI and the web UI."""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    fh = open(os.path.join(SNAPSHOT_DIR, f".{server}.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        die(f"another update is already running for '{server}'")
    return fh


def main() -> None:
    global JSON_MODE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="minecraft-crossplay")
    ap.add_argument("--pack", default=None,
                    help="pack directory (default: found from the server itself)")
    ap.add_argument("--target", help="target Minecraft version")
    ap.add_argument("--latest", action="store_true", help="target the latest release")
    ap.add_argument("--latest-snapshot", action="store_true", help="target the latest snapshot")
    ap.add_argument("--loader", default=None, help="default: from the container's TYPE")
    ap.add_argument("--check", action="store_true", help="compatibility report only")
    ap.add_argument("--update", action="store_true", help="check, back up, then update")
    ap.add_argument("--scan", action="store_true",
                    help="list jars in mods/ the pack does not track; changes nothing")
    ap.add_argument("--adopt", action="store_true",
                    help="write pack metadata for untracked jars Modrinth recognises")
    ap.add_argument("--update-mods", action="store_true",
                    help="newest build of every mod for the current MC version")
    ap.add_argument("--no-adopt", action="store_true",
                    help="do not adopt untracked jars as part of an update")
    ap.add_argument("--regen", action="store_true",
                    help="chunk analysis — delegates to mc-chunkdiff.py")
    ap.add_argument("--json", action="store_true", help="machine-readable output on stdout")
    ap.add_argument("--boot-timeout", type=int, default=600,
                    help="seconds to wait for the server to report Done (default 600)")
    ap.add_argument("--allow-loose-jars", action="store_true",
                    help="proceed even though mods/ holds jars nothing can vouch for")
    a, extra = ap.parse_known_args()
    JSON_MODE = a.json

    # The manager stops a run with SIGTERM (its own timeout, or systemd stopping
    # the service). Turn that into the same path Ctrl-C takes, so a half-applied
    # update is rolled back instead of abandoned.
    def _term(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _term)

    if a.regen:
        regen(extra)
        return

    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$", a.server):
        die("invalid server name")
    if not os.path.isdir(os.path.join(DATA_DIR, a.server)):
        die(f"no server directory {os.path.join(DATA_DIR, a.server)}")

    pack_dir, how = resolve_pack(a.server, a.pack)
    loader = detect_loader(a.server, a.loader)
    try:
        _main(a, pack_dir, how, loader)
    except ApiError as e:
        die(f"{e}. This is a network problem, not a compatibility result — "
            f"nothing was changed; try again.",
            {"server": a.server, "blockers": [], "mods": [], "can_update": False})


def _main(a, pack_dir: str | None, how: str, loader: str) -> None:
    pack_note = f"{pack_dir} ({how})" if pack_dir else "none"

    # ---- scan / adopt / update-mods: no target version involved ---------
    if a.scan or a.adopt or a.update_mods:
        step(f"{a.server}: pack {pack_note}")
        if not pack_dir:
            die("this server has no packwiz pack — run Set Up Packwiz first",
                {"server": a.server})
        base = {"server": a.server, "pack": pack_dir}
        if a.scan:
            loose = loose_jars(a.server, pack_dir)
            for j in loose:
                (ok if j["status"] == "adoptable" else warn)(
                    f"{j['jar']:<44} {j['status']:<11} {j['detail']}")
            if not loose:
                ok("every jar in mods/ is tracked by the pack")
            if JSON_MODE:
                emit({**base, "ok": True, "loose_jars": loose,
                      "managed": len(read_pack(pack_dir))}, 0)
            return
        lock = hold_lock(a.server)
        if a.adopt:
            loose = loose_jars(a.server, pack_dir)
            rep = adopt(a.server, pack_dir, loose)
            left = [j for j in loose if j["status"] not in ("adoptable", "superseded")]
            for j in left:
                warn(f"{j['jar']}: {j['detail']}")
            ok(f"adopted {len(rep['adopted'])}, retired {len(rep['retired'])} duplicate(s), "
               f"{len(left)} left untracked")
            if JSON_MODE:
                emit({**base, "ok": True, **rep, "untracked": left}, 0)
            return
        result = update_mods(a.server, pack_dir, do_adopt=not a.no_adopt)
        lock.close()
        if JSON_MODE:
            emit({**base, **result}, 0 if result["ok"] else 1)
        if not result["ok"]:
            sys.exit(1)
        return

    # ---- resolve target -------------------------------------------------
    current = current_version(a.server)
    if a.latest or a.latest_snapshot:
        release, snapshot = latest_versions()
        if not release and not snapshot:
            die("could not reach the Mojang version manifest")
        a.target = snapshot if a.latest_snapshot else release
    if not a.target:
        die("give --target, --latest, or --latest-snapshot")

    step(f"{a.server}: {current or 'unknown'} → {a.target}  ({loader})")
    _out(f"  pack: {pack_note}")
    if current == a.target:
        ok(f"already on {a.target}; nothing to do")
        if JSON_MODE:
            emit({"server": a.server, "ok": True, "current": current,
                  "target": a.target, "loader": loader, "up_to_date": True,
                  "can_update": False, "applied": False, "pack": pack_dir,
                  "blockers": [], "mods": []}, 0)
        return

    # ---- compatibility gate ---------------------------------------------
    passed, blockers, report, loose = check_compat(
        pack_dir, a.target, loader, a.server, a.allow_loose_jars)

    base = {"server": a.server, "current": current, "target": a.target,
            "loader": loader, "up_to_date": False, "pack": pack_dir,
            "compose_service": compose_service_of(a.server),
            "mods": report, "blockers": blockers, "loose_jars": loose}

    if not passed:
        step("BLOCKED")
        for b in blockers:
            bad(b)
        _out(f"\n  {C_BAD}Update refused.{C_END} Unknown compatibility is treated as "
             f"incompatible —\n  Modrinth metadata is self-reported and often lags reality.\n"
             f"  Resolve or remove the mods above, then re-run.\n")
        if JSON_MODE:
            emit({**base, "ok": False, "can_update": False}, 2)
        sys.exit(2)

    ok("all mods compatible")
    if a.check or not a.update:
        _out("\n  --check only; nothing changed.\n")
        if JSON_MODE:
            emit({**base, "ok": True, "can_update": True, "applied": False}, 0)
        return

    # ---- apply -----------------------------------------------------------
    lock = hold_lock(a.server)
    archive = verified_backup(a.server)
    result = apply_update(a.server, pack_dir, a.target, archive, a.boot_timeout,
                          do_adopt=not a.no_adopt, loose=loose)
    lock.close()

    if result["ok"]:
        step("Done")
        ok(f"{a.server} is on {a.target}")
        _out(f"\n  Roll back with:  borg extract {BORG_REPO}::{archive}\n")
    if JSON_MODE:
        emit({**base, **result, "applied": True}, 0 if result["ok"] else 1)
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
