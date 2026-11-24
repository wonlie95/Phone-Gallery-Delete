#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Native-window app for the phone gallery tool.

Serves the local page over loopback HTTP and shows it inside a native
pywebview window (no system browser, no console window).
"""

import datetime
import json
import os
import shlex
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

import gallery_core as core

try:
    import webview
except Exception as _we:  # pragma: no cover
    webview = None
    _WEBVIEW_ERROR = str(_we)
else:
    _WEBVIEW_ERROR = ""

BASE = core.BASE
PORT = 8765
RECYCLE = os.path.join(BASE, "recycle")
LOG_PATH = os.path.join(BASE, "delete_log.jsonl")
os.makedirs(RECYCLE, exist_ok=True)

_lock = threading.Lock()
_scanning = False
_scan_error = ""
_status_cache = {"time": 0, "value": None}
_window = None
_server = None
_actual_port = PORT
VIDEO_CACHE = os.path.join(BASE, "video_cache")
os.makedirs(VIDEO_CACHE, exist_ok=True)
_video_lock = threading.Lock()
_video_state = {}  # id -> {status, error}


def video_local_path(mid):
    return os.path.join(VIDEO_CACHE, str(mid) + ".mp4")


def video_prepare(mid):
    """Pull a video from the phone into the local cache, once."""
    with _video_lock:
        st = _video_state.get(mid)
        if st and st["status"] == "ready":
            return True, ""
        if st and st["status"] == "pulling":
            return False, "正在准备视频"
        _video_state[mid] = {"status": "pulling", "error": ""}
    local = video_local_path(mid)
    try:
        if not (os.path.isfile(local) and os.path.getsize(local) > 0):
            data = core.load_data()
            item = next((i for i in (data or {}).get("items", []) if i["id"] == mid), None)
            if not item:
                raise ValueError("找不到该视频")
            path = check_item(item)
            serial = core.require_device()
            out, err, code = core.run_adb(["pull", path, local], serial=serial, timeout=600)
            if code != 0 and not os.path.isfile(local):
                raise RuntimeError((err or out).strip()[:200] or "拉取视频失败")
        with _video_lock:
            _video_state[mid] = {"status": "ready", "error": ""}
        return True, ""
    except Exception as exc:
        with _video_lock:
            _video_state[mid] = {"status": "error", "error": str(exc)}
        return False, str(exc)

# Shown while the first scan is running (before index.html exists).
BOOT_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>手机相册整理</title><style>
*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e8eaed;font:15px/1.7 "Microsoft YaHei",sans-serif}
.box{text-align:center}.spin{width:38px;height:38px;margin:0 auto 18px;border:3px solid #30343b;
border-top-color:#4c9aff;border-radius:50%;animation:sp 1s linear infinite}@keyframes sp{to{transform:rotate(360deg)}}
h1{font-size:20px;margin:0 0 8px;font-weight:600}p{color:#9aa0a6;margin:0}</style></head><body>
<div class="box"><div class="spin"></div><h1>手机相册整理</h1><p id="t">正在准备，请稍候…</p></div>
<script>async function poll(){try{const r=await fetch('/api/status');const d=await r.json();
if(d.scanning){document.getElementById('t').textContent='正在扫描手机，请稍候…'}
else if(d.scannedAt){location.href='/';return}
else if(d.scan_error){document.getElementById('t').textContent='扫描失败：'+d.scan_error}
}catch(e){}setTimeout(poll,1200)}poll()</script></body></html>"""


def api_status():
    data = core.load_data()
    now = time.time()
    if _status_cache["value"] is None or (not _scanning and now - _status_cache["time"] > 8):
        try:
            _status_cache["value"] = core.device_status()
            _status_cache["time"] = now
        except Exception:
            _status_cache["value"] = {"devices": [], "ready": [], "adb": ""}
    st = _status_cache["value"]
    ready = bool(st["ready"])
    hint = ""
    if not ready and st["devices"]:
        state = st["devices"][0]["state"]
        hint = {"unauthorized": "请在手机上点允许 USB 调试", "offline": "请重新插拔数据线"}.get(state, state)
    return {
        "device_ready": ready,
        "device": st["ready"][0]["serial"] if ready else "",
        "device_hint": hint,
        "scanning": _scanning,
        "scan_error": _scan_error,
        "scannedAt": data.get("scannedAt", "") if data else "",
        "total": data["summary"]["total"] if data else 0,
    }


def do_scan():
    global _scanning, _scan_error
    with _lock:
        _scanning = True
        _scan_error = ""
        try:
            serial = core.require_device()
            core.build_data(serial)
            return True, ""
        except Exception as exc:
            _scan_error = str(exc)
            return False, _scan_error
        finally:
            _scanning = False


def check_item(item):
    path = item.get("path", "")
    if not path.startswith(core.ALLOWED_PREFIXES):
        raise ValueError("路径不在允许范围：" + path)
    if ".." in path or "\\" in path:
        raise ValueError("路径含非法字符：" + path)
    return path


def remote_exists(path, serial):
    cmd = "if [ -e " + shlex.quote(path) + " ]; then echo 1; else echo 0; fi"
    out, _, _ = core.run_adb(["shell", cmd], serial=serial, timeout=30)
    return "1" in out.splitlines()[:2]


def remove_remote(paths, serial):
    cmd = "rm -f " + " ".join(shlex.quote(p) for p in paths)
    out, err, code = core.run_adb(["shell", cmd], serial=serial, timeout=120)
    if code != 0:
        raise RuntimeError((err or out).strip()[:200])


def write_log(entries):
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def do_delete(ids, mode, confirmed):
    global _scanning
    data = core.load_data()
    if not data:
        return {"ok": False, "error": "没有扫描数据，请先重新扫描"}
    if _scanning:
        return {"ok": False, "error": "正在扫描中，请稍后再试"}
    items = {i["id"]: i for i in data["items"]}
    targets = []
    for mid in ids:
        if mid in items:
            try:
                targets.append((items[mid], check_item(items[mid])))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
    if not targets:
        return {"ok": False, "error": "没有可删除的文件"}
    if len(targets) > 500:
        return {"ok": False, "error": "单次最多处理 500 项，请分批勾选"}

    with _lock:
        try:
            serial = core.require_device()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        details, ok_count, fail_count = [], 0, 0
        logs = []

        if mode == "trash":
            batch = os.path.join(RECYCLE, stamp)
            os.makedirs(batch, exist_ok=True)
            manifest = []
            for item, path in targets:
                rel = path[len("/sdcard/"):]
                local = os.path.join(batch, *rel.split("/"))
                try:
                    os.makedirs(os.path.dirname(local), exist_ok=True)
                    out, err, code = core.run_adb(["pull", path, local], serial=serial, timeout=180)
                    if code != 0 or not os.path.isfile(local):
                        raise RuntimeError((err or out).strip()[:160] or "复制失败")
                    if item["size"] and os.path.getsize(local) != item["size"]:
                        raise RuntimeError("备份大小不一致，已跳过删除")
                    remove_remote([path], serial)
                    if remote_exists(path, serial):
                        raise RuntimeError("手机上仍存在该文件")
                    ok_count += 1
                    details.append("已备份并移除：" + item["name"])
                    manifest.append({**item, "backup": os.path.relpath(local, BASE)})
                    logs.append({"action": "trash", **item, "backup": local, "time": stamp})
                except Exception as exc:
                    fail_count += 1
                    details.append("失败 " + item["name"] + "：" + str(exc))
            with open(os.path.join(batch, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
        elif mode == "permanent":
            if not confirmed:
                return {"ok": False, "error": "永久删除需要明确确认"}
            for start in range(0, len(targets), 10):
                batch_targets = targets[start:start + 10]
                paths = [p for _, p in batch_targets]
                try:
                    remove_remote(paths, serial)
                    gone = []
                    for item, path in batch_targets:
                        if remote_exists(path, serial):
                            fail_count += 1
                            details.append("仍存在：" + item["name"])
                        else:
                            ok_count += 1
                            gone.append(item["name"])
                            logs.append({"action": "permanent", **item, "time": stamp})
                    details.append("第 %d 批完成：%d 项" % (start // 10 + 1, len(gone)))
                except Exception as exc:
                    fail_count += len(batch_targets)
                    details.append("第 %d 批失败：%s" % (start // 10 + 1, str(exc)))
        else:
            return {"ok": False, "error": "未知删除模式"}

        write_log(logs)
        # Drop cached videos for successfully deleted items.
        for mid in ids:
            p = video_local_path(str(mid))
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
            _video_state.pop(str(mid), None)
        core.build_data(serial)
        return {"ok": True, "deleted": ok_count, "failed": fail_count, "details": details}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_video(self, mid):
        local = video_local_path(mid)
        if not os.path.isfile(local):
            self.send_error(404, "视频尚未准备好")
            return
        size = os.path.getsize(local)
        ctype = "video/mp4"
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        status = 200
        if rng.startswith("bytes="):
            try:
                part = rng[6:].split(",")[0]
                if "-" in part:
                    a, b = part.split("-", 1)
                    if a:
                        start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                    status = 206
            except Exception:
                start, end, status = 0, size - 1, 200
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(local, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            index = os.path.join(BASE, "index.html")
            if os.path.isfile(index):
                self.send_file(index, "text/html; charset=utf-8")
            else:
                body = BOOT_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif path.startswith("/thumbs/"):
            name = os.path.basename(path)
            self.send_file(os.path.join(BASE, "thumbs", name), "image/jpeg")
        elif path == "/api/video/status":
            mid = unquote(urlparse(self.path).query).split("id=", 1)[-1].split("&", 1)[0]
            with _video_lock:
                st = dict(_video_state.get(mid, {"status": "idle", "error": ""}))
            local = video_local_path(mid)
            if st["status"] == "idle" and os.path.isfile(local):
                st = {"status": "ready", "error": ""}
            self.send_json(st)
        elif path.startswith("/api/video"):
            self.serve_video(unquote(urlparse(self.path).query).split("id=", 1)[-1].split("&", 1)[0])
        elif path == "/api/status":
            self.send_json(api_status())
        elif path == "/api/health":
            self.send_json({"ok": True})
        else:
            self.send_error(404)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(body or "{}")
        except Exception:
            payload = {}
        if path == "/api/refresh":
            ok, err = do_scan()
            self.send_json({"ok": ok, "error": err} if not ok else {"ok": True})
        elif path == "/api/video/prepare":
            ok, err = video_prepare(payload.get("id", ""))
            self.send_json({"ok": ok, "error": err}, 200 if ok else 400)
        elif path == "/api/delete":
            result = do_delete(payload.get("ids", []), payload.get("mode", ""), bool(payload.get("confirm")))
            self.send_json(result, 200 if result.get("ok") else 400)
        elif path == "/api/shutdown":
            self.send_json({"ok": True})
            threading.Timer(0.25, request_quit).start()
        else:
            self.send_error(404)


def port_available(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def pick_port():
    """Reuse a running instance's port if healthy, else find a free one."""
    for p in range(PORT, PORT + 20):
        if port_available(p):
            return p
        try:
            with urlopen("http://127.0.0.1:%d/api/health" % p, timeout=1) as r:
                if json.loads(r.read().decode("utf-8")).get("ok"):
                    return p  # existing instance; window will attach to it
        except Exception:
            pass
    for p in range(PORT + 20, PORT + 120):
        if port_available(p):
            return p
    raise RuntimeError("找不到可用端口")


def request_quit():
    # Stop the adb server first: it runs from the exe's temp folder and holds
    # a file handle that would otherwise block PyInstaller's cleanup on exit.
    try:
        adb = core.find_adb()
        import subprocess
        subprocess.run([adb, "kill-server"], capture_output=True, timeout=10)
    except Exception:
        pass

    # Delegate to pywebview so the window, GUI loop and server all unwind
    # cleanly (main's finally shuts down the server when start() returns).
    if webview is not None and _window is not None:
        try:
            _window.destroy()
        except Exception:
            pass


class Api:
    """Exposed to the page as window.pywebview.api."""

    def quit(self):
        request_quit()


def main():
    global _window, _server, _actual_port

    if webview is None:
        raise RuntimeError("原生窗口组件未就绪：" + _WEBVIEW_ERROR)

    _actual_port = pick_port()
    _server = ThreadingHTTPServer(("127.0.0.1", _actual_port), Handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()

    # Scan in background so the window appears immediately.
    threading.Thread(target=do_scan, daemon=True).start()

    url = "http://127.0.0.1:%d/" % _actual_port
    _window = webview.create_window(
        "手机相册整理",
        url,
        js_api=Api(),
        width=1280,
        height=860,
        min_size=(960, 640),
        background_color="#0f1115",
        text_select=True,
    )
    try:
        webview.start()
    finally:
        if _server is not None:
            try:
                _server.shutdown()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        with open(os.path.join(BASE, "error.log"), "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        raise
