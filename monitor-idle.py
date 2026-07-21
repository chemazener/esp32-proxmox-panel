#!/usr/bin/env python3
"""Apaga el monitor del host PVE tras IDLE_S sin eventos de teclado/ratón,
y lo enciende ante cualquier evento.

- Vigila /dev/input/event* via python-evdev (filtrando devices con EV_KEY/EV_REL).
- DPMS off/on vía /sys/class/graphics/fbN/blank (1 = powerdown, 0 = unblank).
- Refresca la lista de devices cada DEVICE_REFRESH_S por si entran/salen
  (típico cuando el receptor Logitech está pasado por USB a una VM Windows).
- Sin devices visibles, sigue contando idle: el monitor se apagará igualmente
  pasados IDLE_S, y se encenderá en cuanto vuelva un device y haya evento.
"""
import os
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

import evdev

IDLE_S            = int(os.environ.get("MONITOR_IDLE_S", "300"))   # 5 min (= tiempo de apagado de pantalla)
POLL_S            = float(os.environ.get("MONITOR_POLL_S", "5"))
DEVICE_REFRESH_S  = 30.0
FB_PATHS          = list(Path("/sys/class/graphics").glob("fb*/blank"))
# Fichero de estado compartido: "0" = activo (pantallas ON), "1" = idle (OFF).
# Lo consumen el vm-switcher-api (-> panel ESP32) y el gpu_panel.py (LCD USB).
STATE_FILE        = Path(os.environ.get("MONITOR_STATE_FILE", "/run/monitor-idle.state"))

# --- Detección de teclado/ratón AUNQUE estén en passthrough a una VM ----------
# Cuando una VM (Office 103, etc.) se lleva el receptor Logitech por USB, el host
# ya no lo ve por evdev (driver usbfs). Pero sus URBs siguen pasando por el USB
# core del host y son visibles en usbmon: cualquier línea del device = actividad
# real (en reposo genera 0 líneas). Así los paneles siguen el input -> que es lo
# que despierta/duerme el monitor. Ver memory monitor-idle-host.
USBMON_ENABLE     = os.environ.get("MONITOR_USBMON", "1") == "1"
HID_USB_ID        = os.environ.get("MONITOR_HID_USB", "046d:c52e")   # Logitech MK260
_usb_last         = [0.0]   # timestamp de la última actividad USB del HID (lista = mutable compartida)

# (opcional, por defecto OFF) contar "hay VM iGPU corriendo" como actividad.
IGPU_KEEP_ON      = os.environ.get("MONITOR_IGPU_KEEP_ON", "0") == "1"
IGPU_GROUP        = {int(x) for x in os.environ.get("IGPU_GROUP", "102,103,105,107").split(",") if x.strip()}


def _resolve_hid_usb():
    """(bus, dev) del HID según lsusb -d VENDOR:PROD, o None si no está."""
    try:
        out = subprocess.run(["lsusb", "-d", HID_USB_ID], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return None
    m = re.match(r"Bus (\d+) Device (\d+):", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def usbmon_watcher():
    """Hilo: marca _usb_last cada vez que el HID genera un URB (incluso en passthrough)."""
    subprocess.run(["modprobe", "usbmon"], capture_output=True)
    while True:
        res = _resolve_hid_usb()
        if not res:
            time.sleep(5)
            continue
        bus, dev = res
        token = f":{bus}:{dev:03d}:"
        path = f"/sys/kernel/debug/usb/usbmon/{bus}u"
        try:
            with open(path) as f:
                deadline = time.time() + 30      # re-resolver device cada ~30s
                for line in f:                    # bloquea; el LCD (mismo bus) lo mantiene vivo
                    if token in line:
                        _usb_last[0] = time.time()
                    if time.time() > deadline:
                        break
        except Exception:
            time.sleep(2)


def igpu_vm_running():
    """True si alguna VM del grupo iGPU está 'running' (qm list)."""
    try:
        r = subprocess.run(["qm", "list"], capture_output=True, text=True, timeout=8)
    except Exception:
        return False
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "running":
            try:
                if int(parts[0]) in IGPU_GROUP:
                    return True
            except ValueError:
                pass
    return False


def find_input_devices():
    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
        except Exception:
            continue
        caps = d.capabilities()
        if evdev.ecodes.EV_KEY in caps or evdev.ecodes.EV_REL in caps:
            devs.append(d)
    return devs


def set_screen(on: bool):
    val = "0" if on else "1"
    for fb in FB_PATHS:
        try:
            fb.write_text(val)
        except Exception as e:
            print(f"[monitor-idle] write {fb}: {e}", flush=True)
    # Publicar el estado para el resto de pantallas (ESP32 vía API + LCD USB).
    try:
        STATE_FILE.write_text(val)
    except Exception as e:
        print(f"[monitor-idle] write {STATE_FILE}: {e}", flush=True)


def close_devs(devs):
    for d in devs:
        try:
            d.close()
        except Exception:
            pass


def main():
    last_event   = time.time()   # último evento evdev (teclado/ratón en el host)
    last_refresh = time.time()
    last_igpu_chk = 0.0
    igpu_active  = False
    screen_on    = True
    _usb_last[0] = time.time()
    set_screen(True)

    if USBMON_ENABLE:
        threading.Thread(target=usbmon_watcher, daemon=True).start()

    devs = find_input_devices()
    fds  = {d.fd: d for d in devs}
    print(f"[monitor-idle] watching {len(devs)} device(s), idle={IDLE_S}s, fb={len(FB_PATHS)}, "
          f"usbmon={USBMON_ENABLE}({HID_USB_ID}), igpu_keep_on={IGPU_KEEP_ON}", flush=True)

    while True:
        # Refrescar devices periódicamente
        if time.time() - last_refresh > DEVICE_REFRESH_S:
            close_devs(devs)
            devs = find_input_devices()
            fds  = {d.fd: d for d in devs}
            last_refresh = time.time()

        # (opcional) contar VM iGPU corriendo como actividad
        if IGPU_KEEP_ON and time.time() - last_igpu_chk > 10.0:
            igpu_active = igpu_vm_running()
            last_igpu_chk = time.time()

        # Esperar evento o timeout
        try:
            r, _, _ = select.select(list(fds.keys()), [], [], POLL_S)
        except (OSError, ValueError):
            # fd inválido (device se cayó) — refrescar
            r = []
            last_refresh = 0
            continue

        woke_this_tick = False
        for fd in r:
            d = fds.get(fd)
            if d is None:
                continue
            try:
                events = list(d.read())
                if events:
                    last_event = time.time()
                    woke_this_tick = True
            except (OSError, BlockingIOError):
                last_refresh = 0  # forzará re-find

        # Actividad USB (HID en passthrough) detectada por el hilo usbmon
        usb_woke = (time.time() - _usb_last[0]) < POLL_S

        # Inactivo = tiempo desde la última actividad de CUALQUIER fuente
        idle = time.time() - max(last_event, _usb_last[0])
        active = woke_this_tick or usb_woke or igpu_active
        if screen_on and idle >= IDLE_S and not igpu_active:
            set_screen(False)
            screen_on = False
            print(f"[monitor-idle] OFF after {idle:.0f}s idle", flush=True)
        elif not screen_on and active:
            set_screen(True)
            screen_on = True
            reason = "evdev" if woke_this_tick else ("usb HID" if usb_woke else "iGPU VM")
            print(f"[monitor-idle] ON (wake: {reason})", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
