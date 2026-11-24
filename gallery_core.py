#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Core scanner for the phone gallery tool. Read-only except local cache files."""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict


def _app_dir():
    """Writable directory for runtime data (exe folder, or LocalAppData fallback)."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        probe = os.path.join(exe_dir, ".write_probe")
        try:
            with open(probe, "w") as fh:
                fh.write("")
            os.remove(probe)
            return exe_dir
        except OSError:
            fallback = os.path.join(
                os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "PhoneGalleryCleaner"
            )
            os.makedirs(fallback, exist_ok=True)
            return fallback
    return os.path.dirname(os.path.abspath(__file__))


def _res_dir():
    """Directory holding bundled read-only resources."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


BASE = _app_dir()
RES = _res_dir()
CONFIG_PATH = os.path.join(BASE, "config.json")
FIELD_RE = re.compile(r"(\w+)=(.*?)(?=, \w+=|$)")

ADB_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Android\Sdk\platform-tools\adb.exe"),
    os.path.join(os.environ.get("PROGRAMFILES", ""), r"Android\Android Studio\platform-tools\adb.exe"),
    r"C:\platform-tools\adb.exe",
    r"C:\adb\adb.exe",
]

ALLOWED_PREFIXES = ("/sdcard/DCIM/", "/sdcard/Pictures/", "/sdcard/Download/", "/sdcard/Movies/")

# Screenshot filenames on this device end with the source app package.
APP_NAMES = {
    "com.tencent.mm": "微信",
    "com.tencent.mobileqq": "QQ",
    "com.android.chrome": "Chrome",
    "com.taobao.taobao": "淘宝",
    "com.xunmeng.pinduoduo": "拼多多",
    "com.xiaoji.egggame": "小鸡玩蛋",
    "com.autonavi.minimap": "高德地图",
    "com.limelight": "Moonlight 串流",
    "com.agefans.sp3": "AgeFans",
    "com.taobao.idlefish": "闲鱼",
    "com.sankuai.meituan.takeoutnew": "美团外卖",
    "com.sankuai.meituan": "美团",
    "com.larus.nova": "Nova 启动器",
    "com.openai.chatgpt": "ChatGPT",
    "com.google.android.youtube": "YouTube",
    "com.codexremote.app": "Codex Remote",
    "com.mcdonalds.gma.cn": "麦当劳",
    "com.realtech.xiaocan": "小餐霸王餐",
    "com.eg.android.AlipayGphone": "支付宝",
    "com.hpbr.bosszhipin": "BOSS直聘",
    "com.twitter.android": "X (Twitter)",
    "com.android.settings": "系统设置",
    "com.sankuai.movie": "猫眼电影",
    "com.netease.cloudmusic": "网易云音乐",
    "com.youkagames.gameplatform": "游卡游戏",
    "com.xiaomi.smarthome": "米家",
    "com.ss.android.ugc.aweme": "抖音",
    "com.valvesoftware.android.steam.community": "Steam",
    "com.showstartfans.activity": "秀动",
    "com.taobao.trip": "飞猪",
    "com.android.mms": "短信",
    "com.deepseek.chat": "DeepSeek",
    "com.miui.notes": "小米笔记",
    "com.elenut.gstone": "游戏 (gstone)",
    "com.miui.weather2": "天气",
    "com.jingdong.app.mall": "京东",
    "com.modian.app": "摩点",
    "com.miui.gallery": "小米相册",
    "com.miui.home": "小米桌面",
    "com.xiaomi.xmsf": "小米服务框架",
    "com.mopai.mobapad": "Mobapad",
    "com.miui.tsmclient": "小米公交",
    "com.xingin.xhs": "小红书",
    "com.miui.calculator": "计算器",
    "com.yumc.phsuperapp": "百胜中国",
    "com.lalamove.huolala.client": "货拉拉",
    "com.fastemulator.gba": "GBA 模拟器",
    "com.miui.voiceassist": "小爱同学",
}

# ADB sometimes restarts its daemon between calls; serialize all adb invocations.
_ADB_LOCK = threading.Lock()


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception:
            cfg = {}
    return cfg


def find_adb():
    cfg = load_config()
    if cfg.get("adb") and os.path.isfile(cfg["adb"]):
        return cfg["adb"]
    for p in (os.path.join(RES, "adb.exe"), os.path.join(BASE, "adb.exe")):
        if os.path.isfile(p):
            return p
    found = shutil.which("adb")
    if found:
        return found
    for p in ADB_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    raise RuntimeError("找不到 adb.exe，请在 config.json 里填写 adb 路径")


def run_adb(args, serial=None, timeout=120):
    cmd = [find_adb()]
    serial = serial or load_config().get("serial", "")
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    with _ADB_LOCK:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            startupinfo=startup,
        )
    return proc.stdout or "", proc.stderr or "", proc.returncode


def device_status():
    out, err, code = run_adb(["devices", "-l"], timeout=20)
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        devices.append({"serial": parts[0], "state": parts[1], "raw": line})
    ready = [d for d in devices if d["state"] == "device"]
    return {"devices": devices, "ready": ready, "adb": find_adb()}


def require_device():
    st = device_status()
    if not st["ready"]:
        if any(d["state"] == "unauthorized" for d in st["devices"]):
            raise RuntimeError("手机未授权 USB 调试：请在手机屏幕点「允许」，建议勾选一律允许")
        if any(d["state"] == "offline" for d in st["devices"]):
            raise RuntimeError("手机处于 offline 状态：重新插拔数据线或撤销 USB 调试授权后重试")
        raise RuntimeError("没有检测到可用的 Android 设备：确认数据线、USB 模式和 USB 调试已开启")
    return st["ready"][0]["serial"]


def pull_thumbnails(serial):
    """Full pull on first run, then only fetch thumbnails that are missing locally."""
    local = os.path.join(BASE, "thumbs")
    os.makedirs(local, exist_ok=True)
    listing, _, _ = run_adb(["shell", "ls", "-1", "/sdcard/Pictures/.thumbnails/"], serial=serial, timeout=60)
    remote = [n.strip() for n in listing.splitlines()
              if re.match(r"^\d+\.(jpg|jpeg|png|webp)$", n.strip())]
    local_names = {f for f in os.listdir(local) if os.path.isfile(os.path.join(local, f))}
    missing = [n for n in remote if n not in local_names]

    if len(missing) > 100:
        out, err, code = run_adb(
            ["pull", "/sdcard/Pictures/.thumbnails/.", local + os.sep],
            serial=serial, timeout=420,
        )
        if code != 0 and "files pulled" not in out + err:
            raise RuntimeError("拉取缩略图失败：" + (err or out).strip()[:300])
    else:
        for name in missing:
            run_adb(
                ["pull", "/sdcard/Pictures/.thumbnails/" + name, os.path.join(local, name)],
                serial=serial, timeout=90,
            )
    return len([f for f in os.listdir(local) if os.path.isfile(os.path.join(local, f))])


def query_media(uri, projection, filename, serial):
    out, err, code = run_adb(
        ["shell", "content", "query", "--uri", uri, "--projection", projection],
        serial=serial, timeout=180,
    )
    if code != 0 and "Row:" not in out:
        raise RuntimeError("读取媒体库失败：" + (err or out).strip()[:300])
    path = os.path.join(BASE, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out)
    return path


def app_of(name, category):
    """Return (display name, package) extracted from a screenshot filename."""
    if category != "截图":
        return "", ""
    stem = re.sub(r"\.(jpg|jpeg|png|webp)$", "", name, flags=re.I)
    tail = stem.rsplit("_", 1)[-1]
    if re.match(r"^(com|cn|org|net)\.[a-z0-9_]", tail, re.I):
        pkg = re.sub(r"-edit$", "", tail)
        return APP_NAMES.get(pkg, pkg), pkg
    return "系统截图", ""


def parse_dump(filename, kind):
    items = []
    path = os.path.join(BASE, filename)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("Row:"):
                continue
            f = dict(FIELD_RE.findall(line))
            mid, name, rel = f.get("_id", ""), f.get("_display_name", ""), f.get("relative_path", "")
            if not name:
                continue
            full = rel if re.search(r"\.\w+$", rel) else (rel.rstrip("/") + "/" + name if rel else name)
            try:
                size = int(f.get("_size", 0) or 0)
            except ValueError:
                size = 0
            try:
                ts = int(f.get("date_added", 0) or 0)
            except ValueError:
                ts = 0
            try:
                w, h = int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)
            except ValueError:
                w = h = 0

            thumb = ""
            for ext in ("jpg", "jpeg", "png", "webp"):
                candidate = os.path.join(BASE, "thumbs", mid + "." + ext)
                if os.path.exists(candidate):
                    thumb = "thumbs/" + mid + "." + ext
                    break

            bucket = f.get("bucket_display_name", "") or "未分类"
            lower = name.lower()
            if kind == "video":
                category = "视频"
            elif re.search(r"screenshot|screen_dl|截屏|截图", lower) or bucket in ("Screenshots", "ScreenRecorder"):
                category = "截图"
            elif bucket in ("Camera", "相机"):
                category = "相机"
            else:
                category = "其他图片"
            app_name, app_pkg = app_of(name, category)
            dt = datetime.datetime.fromtimestamp(ts) if ts else None
            items.append({
                "id": mid,
                "name": name,
                "path": "/sdcard/" + full,
                "bucket": bucket,
                "kind": kind,
                "category": category,
                "size": size,
                "ts": ts,
                "date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "month": dt.strftime("%Y-%m") if dt else "未知",
                "w": w, "h": h,
                "thumb": thumb,
                "duration": f.get("duration", ""),
                "app": app_name,
                "appPkg": app_pkg,
                "tags": [],
            })
    return items


def mark_tags(items):
    by_size, by_shot = defaultdict(list), defaultdict(list)
    now = datetime.datetime.now().timestamp()
    for it in items:
        if it["size"] > 0 and it["w"] and it["h"]:
            by_size[(it["size"], it["w"], it["h"])].append(it)
        m = re.search(r"Screenshot_(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})", it["name"])
        if m:
            by_shot[m.group(1)].append(it)
        if it["size"] >= 20 * 1024 * 1024:
            it["tags"].append("大文件")
        if it["ts"] and now - it["ts"] < 30 * 86400:
            it["tags"].append("近30天")
    groups = []
    for key, g in by_size.items():
        if len(g) > 1:
            for it in g:
                it["tags"].append("疑似重复")
            groups.append({"type": "同尺寸同大小", "key": str(key), "ids": [x["id"] for x in g]})
    for key, g in by_shot.items():
        if len(g) > 1:
            for it in g:
                if "疑似重复" not in it["tags"]:
                    it["tags"].append("同一时刻截图")
            groups.append({"type": "同一时刻截图", "key": key, "ids": [x["id"] for x in g]})
    return groups


def atomic_write(path, text):
    """Write via temp file + atomic replace so readers never see a partial file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return (f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B")
        n /= 1024
    return f"{n:.1f} TB"


def build_data(serial):
    pull_thumbnails(serial)
    query_media(
        "content://media/external/images/media",
        "_id:_display_name:relative_path:date_added:_size:width:height:bucket_display_name",
        "images.txt", serial,
    )
    query_media(
        "content://media/external/video/media",
        "_id:_display_name:relative_path:date_added:_size:width:height:duration:bucket_display_name",
        "videos.txt", serial,
    )
    items = parse_dump("images.txt", "image") + parse_dump("videos.txt", "video")
    dups = mark_tags(items)
    items.sort(key=lambda x: x["ts"], reverse=True)
    buckets = Counter(i["bucket"] for i in items)
    cats = Counter(i["category"] for i in items)
    months = Counter(i["month"] for i in items)
    apps = Counter(i["app"] for i in items if i["app"])
    summary = {
        "images": len([i for i in items if i["kind"] == "image"]),
        "videos": len([i for i in items if i["kind"] == "video"]),
        "total": len(items),
        "bytes": sum(i["size"] for i in items),
        "size": human(sum(i["size"] for i in items)),
        "buckets": len(buckets),
        "thumbnails": len([i for i in items if i["thumb"]]),
        "dups": len(dups),
        "dup_items": len([i for i in items if any("重复" in t or "同一时刻" in t for t in i["tags"])]),
    }
    data = {
        "items": items,
        "buckets": buckets.most_common(),
        "categories": cats.most_common(),
        "months": months.most_common(),
        "apps": apps.most_common(),
        "dupGroups": dups,
        "summary": summary,
        "device": serial,
        "scannedAt": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_write(os.path.join(BASE, "data.json"),
                 json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    render_html(data)
    return data


def render_html(data):
    tpl = os.path.join(RES, "template.html")
    out = os.path.join(BASE, "index.html")
    with open(tpl, "r", encoding="utf-8") as fh:
        html = fh.read()
    payload = json.dumps({k: v for k, v in data.items() if k != "items"}, ensure_ascii=False, separators=(",", ":"))
    items = json.dumps(data["items"], ensure_ascii=False, separators=(",", ":"))
    html = html.replace("/*__META__*/", payload).replace("/*__ITEMS__*/", items)
    atomic_write(out, html)


def load_data():
    path = os.path.join(BASE, "data.json")
    if not os.path.exists(path):
        return None
    for _ in range(4):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            time.sleep(0.15)
    return None
