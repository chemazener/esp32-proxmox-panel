"""VM Switcher API for the homelab ESP32 panel.

Endpoints:
  GET  /healthz                           -> liveness, sin auth
  GET  /status                            -> [retrocompat] solo las VMs del grupo iGPU + cual activa
  GET  /machines                          -> TODAS las CTs+VMs (excluyendo EXCLUDE_VMIDS) + flag igpu_group
  POST /start/{vmid}                      -> arranca CT o VM (autodetecta kind)
  POST /stop/{vmid}                       -> apaga CT o VM (graceful: shutdown ACPI / pct shutdown)
  POST /switch/{vmid}                     -> [retrocompat] apaga la activa del grupo iGPU + arranca destino
  POST /shutdown/{vmid}                   -> [retrocompat] alias de /stop
  POST /reset/{vmid}                      -> reset duro de VM (solo VMs)

Auth: X-API-Key header (env API_TOKEN).
Config opcional via env:
  PVE_HOST=root@192.168.1.10
  EXCLUDE_VMIDS=100              (csv)
  IGPU_GROUP=102,103,105,107     (csv: VMs que comparten iGPU UHD 630, exclusivas)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware

API_TOKEN = os.environ.get("API_TOKEN", "")
HOST = os.environ.get("PVE_HOST", "root@192.168.1.10")


def _csv_ids(env_name: str, default: str) -> set[int]:
    raw = os.environ.get(env_name, default)
    out: set[int] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            pass
    return out


EXCLUDE_VMIDS = _csv_ids("EXCLUDE_VMIDS", "100")     # CT 100 = backend host, no permitir auto-suicidio
IGPU_GROUP    = _csv_ids("IGPU_GROUP",    "102,103,105,107")

# Etiquetas cortas para algunos VMIDs (las que ya estaban en el firmware viejo).
# Si una máquina no está aquí, el firmware mostrará su `name` directamente.
LABELS = {
    102: "GAME",
    103: "OFFICE",
    105: "SSD",
    107: "OMARCHY",
}

app = FastAPI(title="VM Switcher API", version="0.3.2-host")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _auth(x_api_key: Optional[str]) -> None:
    if not API_TOKEN:
        raise HTTPException(503, "API_TOKEN not configured on server")
    if x_api_key != API_TOKEN:
        raise HTTPException(401, "invalid api key")


def _ssh(cmd: str, timeout: int = 10) -> subprocess.CompletedProcess:
    """Ejecuta `cmd` LOCALMENTE en el host PVE (esta API corre en el propio
    host, no en la CT 100). Se conserva el nombre `_ssh` para no tocar el resto
    del codigo; ya no hay salto por red — qm/pct/systemd-run son locales."""
    return subprocess.run(
        ["bash", "-lc", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _parse_qm_list(stdout: str) -> list[tuple[int, str, str]]:
    """`qm list` -> [(vmid, status, name)].

    Cabecera: 'VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID' (6 columnas, fijas)."""
    out: list[tuple[int, str, str]] = []
    for line in stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            vmid = int(parts[0])
        except ValueError:
            continue
        out.append((vmid, parts[2].lower(), parts[1]))
    return out


def _parse_pct_list(stdout: str) -> list[tuple[int, str, str]]:
    """`pct list` -> [(vmid, status, name)].

    Cabecera: 'VMID Status Lock Name'. 'Lock' suele estar vacío -> con split()
    desaparece y la fila pasa de 4 a 3 tokens; el name siempre es el último."""
    out: list[tuple[int, str, str]] = []
    for line in stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            vmid = int(parts[0])
        except ValueError:
            continue
        out.append((vmid, parts[1].lower(), parts[-1]))
    return out


# ---------- Listado con cache + refresco en segundo plano ----------
# El coste real está en preguntar a Proxmox: `pvesh get /cluster/resources`
# (~0.9 s, 1 llamada) o el fallback `qm list`+`pct list` (~2 s). Para que el
# panel NUNCA se bloquee esperando, un hilo de fondo refresca el cache cada
# _REFRESH_S y TODAS las peticiones (incluido el sondeo /machines) devuelven la
# copia cacheada al instante (~2 ms).
_LIST_TTL  = float(os.environ.get("LIST_TTL", "8"))    # antigüedad máx. del cache si el hilo de fondo muriese
_REFRESH_S = float(os.environ.get("REFRESH_S", "2"))   # periodo del hilo de fondo
_list_cache: dict = {"t": 0.0, "data": None}


def _fetch_via_pvesh() -> list[dict]:
    """1 sola llamada (~0.9 s): `pvesh get /cluster/resources --type vm` = VMs+CTs."""
    r = _ssh("pvesh get /cluster/resources --type vm --output-format json")
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(r.stderr.strip() or "pvesh sin salida")
    items: list[dict] = []
    for g in json.loads(r.stdout):
        try:
            vmid = int(g["vmid"])
        except (KeyError, ValueError, TypeError):
            continue
        if vmid in EXCLUDE_VMIDS:
            continue
        kind = "VM" if g.get("type") == "qemu" else "CT"
        items.append({
            "kind": kind, "id": vmid, "name": g.get("name", ""),
            "status": str(g.get("status", "")).lower(),
            "label": LABELS.get(vmid),
            "igpu_group": kind == "VM" and vmid in IGPU_GROUP,
        })
    return items


def _fetch_via_qm_pct() -> list[dict]:
    """Fallback (~2 s): `qm list` + `pct list` si pvesh fallara."""
    items: list[dict] = []
    rq = _ssh("qm list")
    if rq.returncode == 0:
        for vmid, status, name in _parse_qm_list(rq.stdout):
            if vmid in EXCLUDE_VMIDS:
                continue
            items.append({
                "kind": "VM", "id": vmid, "name": name, "status": status,
                "label": LABELS.get(vmid),
                "igpu_group": vmid in IGPU_GROUP,
            })
    rp = _ssh("pct list")
    if rp.returncode == 0:
        for vmid, status, name in _parse_pct_list(rp.stdout):
            if vmid in EXCLUDE_VMIDS:
                continue
            items.append({
                "kind": "CT", "id": vmid, "name": name, "status": status,
                "label": LABELS.get(vmid),
                "igpu_group": False,
            })
    return items


def _list_all(force: bool = False) -> list[dict]:
    """Listado completo de CTs+VMs. `force=False` reusa el cache si tiene
    < _LIST_TTL s (el hilo de fondo lo mantiene fresco ~cada _REFRESH_S)."""
    now = time.time()
    if (not force and _list_cache["data"] is not None
            and (now - _list_cache["t"]) < _LIST_TTL):
        return _list_cache["data"]
    try:
        items = _fetch_via_pvesh()
    except Exception:
        items = _fetch_via_qm_pct()
    items.sort(key=lambda m: m["id"])
    _list_cache["data"] = items
    _list_cache["t"] = now
    return items


def _refresh_loop() -> None:
    """Hilo daemon: mantiene el cache caliente para que /machines sea instantáneo."""
    while True:
        try:
            _list_all(force=True)
        except Exception:
            pass
        time.sleep(_REFRESH_S)


@app.on_event("startup")
def _start_bg_refresh() -> None:
    threading.Thread(target=_refresh_loop, daemon=True, name="list-refresh").start()


def _kind_of(vmid: int, listing: Optional[list[dict]] = None) -> Optional[str]:
    """Detecta si VMID corresponde a CT o VM, mirando la lista cacheada o re-listando."""
    if listing is None:
        listing = _list_all()
    for m in listing:
        if m["id"] == vmid:
            return m["kind"]
    return None


def _tool_for(kind: str) -> str:
    return "qm" if kind == "VM" else "pct"


# ---------- Endpoints ----------

@app.get("/healthz")
def healthz():
    return {"ok": True, "time": int(time.time()), "version": app.version}


@app.get("/machines")
def get_machines(x_api_key: Optional[str] = Header(None)):
    """Lista completa para el panel ESP32 (modo nuevo).
    `active_igpu`: VMID del grupo iGPU que esté running, o null."""
    _auth(x_api_key)
    items = _list_all()   # el panel sondea aquí: cache instantáneo (lo refresca el hilo de fondo)
    active_igpu: Optional[int] = None
    for m in items:
        if m["igpu_group"] and m["status"] == "running":
            active_igpu = m["id"]
            break
    return {"machines": items, "active_igpu": active_igpu, "n": len(items)}


@app.get("/status")
def get_status(x_api_key: Optional[str] = Header(None)):
    """[Retrocompat] formato antiguo: solo las VMs del grupo iGPU."""
    _auth(x_api_key)
    items = _list_all()
    out_vms = []
    active: Optional[int] = None
    for m in items:
        if not m["igpu_group"]:
            continue
        if m["status"] == "running":
            active = m["id"]
        out_vms.append({
            "id":     m["id"],
            "label":  m["label"] or m["name"],
            "name":   m["name"],
            "status": m["status"],
        })
    return {"vms": out_vms, "active": active}


def _run_detached(unit_prefix: str, vmid: int, shell_cmd: str) -> dict:
    unit = f"{unit_prefix}-{vmid}-{int(time.time())}"
    r = _ssh(f"systemd-run --no-block --unit={unit} bash -c {shell_cmd!r}", timeout=15)
    return {
        "ok": r.returncode == 0,
        "vmid": vmid,
        "unit": unit,
        "stderr": r.stderr.strip() if r.stderr else "",
    }


@app.post("/start/{vmid}")
def start(vmid: int, x_api_key: Optional[str] = Header(None)):
    """Arranca CT o VM (autodetecta kind)."""
    _auth(x_api_key)
    if vmid in EXCLUDE_VMIDS:
        raise HTTPException(403, f"vmid {vmid} excluded by config")
    kind = _kind_of(vmid)
    if kind is None:
        raise HTTPException(404, f"vmid {vmid} not found")
    res = _run_detached("api-start", vmid, f"{_tool_for(kind)} start {vmid}")
    res.update(kind=kind, action="start")
    return res


@app.post("/stop/{vmid}")
def stop(vmid: int, x_api_key: Optional[str] = Header(None)):
    """Apagado ACPI/graceful. VMs: `qm shutdown --timeout 120`. CTs: `pct shutdown --timeout 60`."""
    _auth(x_api_key)
    if vmid in EXCLUDE_VMIDS:
        raise HTTPException(403, f"vmid {vmid} excluded by config")
    kind = _kind_of(vmid)
    if kind is None:
        raise HTTPException(404, f"vmid {vmid} not found")
    if kind == "VM":
        cmd = f"qm shutdown {vmid} --timeout 120 --forceStop 0"
    else:
        cmd = f"pct shutdown {vmid} --timeout 60 --forceStop 0"
    res = _run_detached("api-stop", vmid, cmd)
    res.update(kind=kind, action="stop")
    return res


# ---- retrocompat ----

@app.post("/switch/{vmid}")
def switch(vmid: int, x_api_key: Optional[str] = Header(None)):
    """[Retrocompat] apaga la VM del grupo iGPU que esté activa y arranca `vmid`.
    Sólo válido para VMs del IGPU_GROUP."""
    _auth(x_api_key)
    if vmid not in IGPU_GROUP:
        raise HTTPException(400, f"vmid {vmid} not in IGPU_GROUP {sorted(IGPU_GROUP)}")
    items = _list_all(force=True)
    active: Optional[int] = None
    for m in items:
        if m["igpu_group"] and m["status"] == "running":
            active = m["id"]
            break
    if active == vmid:
        return {"ok": True, "msg": "already running", "from": active, "to": vmid}
    if active is None:
        cmd = f"qm start {vmid}"
    else:
        cmd = f"qm shutdown {active} --timeout 120 --forceStop 1 || true; qm start {vmid}"
    res = _run_detached("api-switch", vmid, cmd)
    res.update(action="switch", to=vmid, **({"from": active} if active else {}))
    return res


@app.post("/shutdown/{vmid}")
def shutdown_alias(vmid: int, x_api_key: Optional[str] = Header(None)):
    """[Retrocompat] alias de /stop."""
    return stop(vmid, x_api_key)  # type: ignore[arg-type]


@app.post("/reset/{vmid}")
def reset(vmid: int, x_api_key: Optional[str] = Header(None)):
    """Reset duro (qm reset). Solo VMs; no aplica a CTs."""
    _auth(x_api_key)
    kind = _kind_of(vmid)
    if kind != "VM":
        raise HTTPException(400, f"reset solo aplica a VMs (vmid {vmid} es {kind or 'desconocido'})")
    res = _run_detached("api-reset", vmid, f"qm reset {vmid}")
    res.update(kind=kind, action="reset")
    return res


# ============================================================================
# Ocupación de tareas (CPU/MEM/GPU por máquina) + GPU agregada.
# Añadido 2026-07-15 para el dashboard del panel ESP32 y las gráficas del USB.
# ============================================================================
import re as _re
from collections import defaultdict as _defaultdict

_CG_LXC = _re.compile(r"/lxc/(\d+)")
_CG_QEMU = _re.compile(r"/(\d+)\.scope")


def _pid_to_vmid(pid):
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            cg = f.read()
    except Exception:
        return None
    m = _CG_LXC.search(cg) or _CG_QEMU.search(cg)
    return int(m.group(1)) if m else None


def _gpu_per_vmid():
    """{vmid: sm%} sumando sm por proceso (nvidia-smi pmon). {} si la 3080 está en VFIO."""
    try:
        r = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"],
                           capture_output=True, text=True, timeout=4)
    except Exception:
        return {}
    if r.returncode != 0:
        return {}
    per = _defaultdict(int)
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[1])
        except Exception:
            continue
        try:
            sm = int(parts[3]) if parts[3] != "-" else 0
        except Exception:
            sm = 0
        vmid = _pid_to_vmid(pid)
        if vmid is not None:
            per[vmid] += sm
    return dict(per)


_occ_cache = {"t": 0.0, "data": None}
_OCC_TTL = 2.0


def _fetch_occupancy():
    try:
        r = _ssh("pvesh get /cluster/resources --type vm --output-format json")
        data = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else []
    except Exception:
        data = []
    gpu = _gpu_per_vmid()
    out = []
    for g in data:
        try:
            vmid = int(g["vmid"])
        except Exception:
            continue
        if vmid in EXCLUDE_VMIDS or str(g.get("status", "")).lower() != "running":
            continue
        kind = "VM" if g.get("type") == "qemu" else "CT"
        maxmem = int(g.get("maxmem") or 1)
        mem = int(g.get("mem") or 0)
        maxdisk = int(g.get("maxdisk") or 0)
        disk = int(g.get("disk") or 0)
        out.append({
            "id": vmid,
            "kind": kind,
            "label": LABELS.get(vmid) or g.get("name", ""),
            "name": g.get("name", ""),
            "cpu": round(float(g.get("cpu") or 0.0) * 100.0, 1),
            "mem": min(100.0, round(mem * 100.0 / maxmem, 1)) if maxmem > 0 else 0.0,
            "gpu": int(gpu.get(vmid, 0)),
            "disk": min(100.0, round(disk * 100.0 / maxdisk, 1)) if maxdisk > 0 else 0.0,
            "igpu_group": kind == "VM" and vmid in IGPU_GROUP,
        })
    # ordenar por más cargadas primero (cpu+gpu), luego por id
    out.sort(key=lambda m: (-(m["cpu"] + m["gpu"]), m["id"]))
    return out


def _host_stats():
    """RAM/carga/disco/hostname del HOST PVE (vía SSH) para la barra inferior del panel."""
    mem_total = mem_avail = disk_total = disk_used = 0
    load1 = 0.0
    cpus = 1
    name = "pve"
    try:
        r = _ssh(
            "awk '/^MemTotal:/{t=$2}/^MemAvailable:/{a=$2}END{print t, a}' /proc/meminfo; "
            "cut -d' ' -f1 /proc/loadavg; nproc; hostname; "
            "zpool list -Hp -o size,alloc | awk '{s+=$1; u+=$2}END{print s, u}'",
            timeout=8)
        lines = r.stdout.strip().splitlines()
        if len(lines) >= 5:
            mem_total, mem_avail = (int(x) for x in lines[0].split())
            load1 = float(lines[1])
            cpus = int(lines[2])
            name = lines[3].strip() or "pve"
            disk_total, disk_used = (int(x) for x in lines[4].split())
    except Exception:
        pass
    return {
        "mem_avail_mb": mem_avail // 1024, "mem_total_mb": mem_total // 1024,
        "load1": round(load1, 2), "cpus": cpus, "name": name,
        "disk_used_gb": round(disk_used / (1024 ** 3), 1),
        "disk_total_gb": round(disk_total / (1024 ** 3), 1),
        "gpu": _gpu_totals().get("util", 0),
    }


@app.get("/occupancy")
def get_occupancy(x_api_key: Optional[str] = Header(None)):
    """Ocupación por máquina running: cpu%, mem%, gpu%(sm). Cache 2 s."""
    _auth(x_api_key)
    now = time.time()
    if _occ_cache["data"] is not None and (now - _occ_cache["t"]) < _OCC_TTL:
        d = _occ_cache["data"]
    else:
        d = _fetch_occupancy()
        _occ_cache.update(t=now, data=d)
    return {"machines": d, "n": len(d), "host": _host_stats()}


def _gpu_totals():
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4)
        if r.returncode == 0 and r.stdout.strip():
            u, used, total, temp, pw = [x.strip() for x in r.stdout.strip().splitlines()[0].split(",")]
            return {"util": int(float(u)), "mem_used": int(float(used)), "mem_total": int(float(total)),
                    "temp": int(float(temp)), "power": round(float(pw), 1), "vfio": False}
    except Exception:
        pass
    return {"util": 0, "mem_used": 0, "mem_total": 0, "temp": 0, "power": 0.0, "vfio": True}


@app.get("/gpu")
def get_gpu(x_api_key: Optional[str] = Header(None)):
    """Estado agregado de la RTX 3080 (util, VRAM, temp, potencia)."""
    _auth(x_api_key)
    return {"gpu": _gpu_totals()}
