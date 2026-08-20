"""ServerPanel — Web control panel for the Telegram Video Downloader server.

FastAPI single-file app: dashboard (bot status, CPU/RAM/disk, Docker, user
processes), process kill, bot/container control, a free shell box and live
logs over WebSocket. Password-protected, intended to run inside a private
network (LAN / Tailscale) only.

Run:  uvicorn panel:app --host 0.0.0.0 --port 8080
"""

import asyncio
import json
import os
import shlex
import subprocess
import time
from collections import deque

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Config ──────────────────────────────────────────────────────────────────
PANEL_PASSWORD = os.getenv("PANEL_PASSWORD", "1611")
BOT_SERVICE = os.getenv("PANEL_BOT_SERVICE", "videobot")
LOG_FILES = os.getenv("PANEL_LOG_FILES", "data/bot.log,/var/log/syslog").split(",")
HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="ServerPanel")
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
PANEL_HTML = os.path.join(HERE, "templates", "panel.html")

# ── Auth: minimal cookie session ────────────────────────────────────────────
_SESSIONS: set[str] = set()


def _check_auth(request: Request) -> bool:
    return request.cookies.get("panel_auth") in _SESSIONS


# ── Helpers ─────────────────────────────────────────────────────────────────
def _run(cmd: list[str], timeout: int = 30) -> dict:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": True, "code": r.returncode, "out": r.stdout[-4000:], "err": r.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "out": "", "err": "timeout"}
    except FileNotFoundError:
        return {"ok": False, "code": -1, "out": "", "err": "command not found"}


def _sh(cmd: str, timeout: int = 30) -> dict:
    """Run a shell command string as the current user (must not require TTY)."""
    return _run(["/bin/bash", "-lc", cmd], timeout=timeout)


def _bot_status() -> dict:
    r = _run(["systemctl", "is-active", BOT_SERVICE], timeout=10)
    return {"service": BOT_SERVICE, "active": r.get("out", "").strip() == "active"}


def _sysinfo() -> dict:
    info = {}
    try:
        with open("/proc/loadavg") as f:
            info["load"] = f.read().split()[:3]
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                k, _, v = line.partition(":")
                mem[k] = int(v.strip().split()[0]) * 1024
            info["ram"] = {"total": mem.get("MemTotal", 0), "free": mem.get("MemAvailable", 0)}
        disk = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()
        info["disk"] = disk[1].split() if len(disk) > 1 else []
    except Exception:
        pass
    info["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    info["uptime"] = str(subprocess.run(["uptime", "-p"], capture_output=True, text=True).stdout).strip()
    return info


def _user_processes() -> list[dict]:
    r = subprocess.run(
        ["ps", "-eo", "pid,user,%cpu,%mem,etime,cmd", "--sort=-%cpu"],
        capture_output=True, text=True, timeout=10,
    )
    procs = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split(None, 5)
        if len(parts) == 6:
            procs.append({"pid": parts[0], "user": parts[1], "cpu": parts[2],
                          "mem": parts[3], "etime": parts[4], "cmd": parts[5][:150]})
    return procs[:60]


def _docker() -> list[dict]:
    r = subprocess.run(["docker", "ps", "-a", "--format", "{{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}"],
                       capture_output=True, text=True, timeout=15)
    out = []
    for line in r.stdout.splitlines():
        if "|" in line:
            cid, name, status, image = line.split("|", 3)
            out.append({"id": cid[:12], "name": name, "status": status, "image": image})
    return out


_wire_conns: set[WebSocket] = set()
_log_buffer: deque[str] = deque(maxlen=400)


def _tail_log(path: str, n: int = 400) -> str:
    try:
        r = subprocess.run(["tail", "-n", str(n), path], capture_output=True, text=True, timeout=10)
        return r.stdout
    except Exception:
        return ""


@app.on_event("startup")
async def _startup():
    async def _log_feeder():
        """Poll log files and push new lines to connected WebSockets."""
        cursors = {p: len(_tail_log(p).encode()) for p in LOG_FILES if os.path.exists(p)}
        while True:
            await asyncio.sleep(1.5)
            for path in list(cursors):
                if not os.path.exists(path):
                    continue
                full = _tail_log(path)
                data = full.encode()
                if len(data) > cursors[path]:
                    chunk = data[cursors[path]:]
                    cursors[path] = len(data)
                    for line in chunk.decode(errors="replace").splitlines():
                        _log_buffer.append(f"[{os.path.basename(path)}] {line}")
                elif len(data) < cursors[path]:
                    cursors[path] = 0
            if _wire_conns and _log_buffer:
                batch = "\n".join(_log_buffer)
                _log_buffer.clear()
                for ws in list(_wire_conns):
                    try:
                        await ws.send_text(batch)
                    except Exception:
                        _wire_conns.discard(ws)

    asyncio.create_task(_log_feeder())


# ── Routes ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(PANEL_HTML)


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if body.get("password") == PANEL_PASSWORD:
        token = os.urandom(16).hex()
        _SESSIONS.add(token)
        resp = JSONResponse({"ok": True})
        resp.set_cookie("panel_auth", token, httponly=True, samesite="strict")
        return resp
    return JSONResponse({"ok": False}, status_code=401)


@app.get("/api/logout")
async def logout(request: Request):
    token = request.cookies.get("panel_auth")
    if token:
        _SESSIONS.discard(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("panel_auth")
    return resp


@app.get("/api/status")
async def status(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "auth": False}, status_code=401)
    return {
        "ok": True,
        "bot": _bot_status(),
        "sys": _sysinfo(),
        "docker": _docker(),
        "procs": _user_processes(),
    }


@app.post("/api/bot")
async def bot_action(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "auth": False}, status_code=401)
    body = await request.json()
    action = body.get("action")  # start | stop | restart
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "err": "bad action"}
    return _run(["sudo", "systemctl", action, BOT_SERVICE], timeout=20)


@app.post("/api/docker")
async def docker_action(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "auth": False}, status_code=401)
    body = await request.json()
    return _run(["docker", body.get("action", "restart"), body.get("name", "")], timeout=30)


@app.post("/api/kill")
async def kill_proc(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "auth": False}, status_code=401)
    body = await request.json()
    pid = str(body.get("pid", "")).strip()
    if not pid.isdigit():
        return {"ok": False, "err": "invalid pid"}
    return _run(["kill", "-TERM", pid], timeout=10)


@app.post("/api/shell")
async def shell(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "auth": False}, status_code=401)
    body = await request.json()
    cmd = str(body.get("cmd", "")).strip()
    if not cmd:
        return {"ok": False, "err": "empty"}
    result = _sh(cmd, timeout=60)
    result["cmd"] = cmd
    return result


@app.post("/api/reboot")
async def reboot(request: Request):
    if not _check_auth(request):
        return JSONResponse({"ok": False, "auth": False}, status_code=401)
    body = await request.json()
    mode = body.get("mode", "reboot")  # reboot | poweroff
    if mode not in ("reboot", "poweroff"):
        return {"ok": False, "err": "bad mode"}
    # detach: the panel dies with the machine, so run it async
    subprocess.Popen(["/bin/bash", "-c",
                      f"sleep 2 && sudo systemctl {mode} 2>/dev/null || sudo {mode} 2>/dev/null || systemctl {mode}"])
    return {"ok": True, "msg": f"{mode} scheduled in 2s"}


@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    _wire_conns.add(websocket)
    try:
        # send recent buffer first
        if _log_buffer:
            await websocket.send_text("\n".join(_log_buffer))
        while True:
            await websocket.receive_text()  # keepalive/ping
    except WebSocketDisconnect:
        _wire_conns.discard(websocket)
    except Exception:
        _wire_conns.discard(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PANEL_PORT", "8080")))