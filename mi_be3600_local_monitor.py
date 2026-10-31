#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mi-be3600-local-monitor · 小米 BE3600 本地监控服务（纯 Python 标准库，无需 SSH）。

通过路由器本地 Web 管理接口读取连接设备及实时流量，按采样周期累计每台设备的
"当日流量"（估算口径，见 README），并通过内置 HTTP 服务输出暗色 ECharts 仪表盘。

登录算法（BE3600 新固件，已实测确认）：
  - 从登录页 /cgi-bin/luci/web 抓取 key 与 deviceId（deviceId 为发起登录设备的 MAC）
  - nonce = "0_{deviceId}_{int(time.time())}_{random}"
  - password = sha256(nonce + sha256(密码 + key))（十六进制）
  - POST /cgi-bin/luci/api/xqsystem/login 返回 token(stok)
  - 后续接口通过 /cgi-bin/luci/;stok=<token>/... 访问，401 时自动重登一次

用法：
  python3 mi_be3600_local_monitor.py            # 常驻：采集 + 仪表盘
  python3 mi_be3600_local_monitor.py --check    # 仅测试登录
  python3 mi_be3600_local_monitor.py --once     # 单次采集并打印文本报告
"""

import hashlib
import http.server
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

DEFAULTS = {
    "ROUTER_HOST": "192.168.31.1",
    "ROUTER_USER": "admin",
    "ROUTER_PASSWORD": "",
    "POLL_INTERVAL": "30",
    "RETAIN_DAYS": "30",
    "DATA_FILE": "/data/miwifi_data.json",
    "WEB_HOST": "0.0.0.0",
    "WEB_PORT": "10000",
}


def load_config():
    cfg = dict(DEFAULTS)
    for k in cfg:
        if k in os.environ:
            cfg[k] = os.environ[k]
    if os.path.exists("/app/config.json"):
        try:
            with open("/app/config.json", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


class MiWiFiClient:
    """小米路由器本地 Web API 客户端。只读。"""

    def __init__(self, host, user, password, timeout=8):
        self.base = f"http://{host}/cgi-bin/luci"
        self.user = user
        self.password = password
        self.timeout = timeout
        self.key = None
        self.device_id = "0"
        self.token = None
        self.logged_in = False
        self.opener = urllib.request.build_opener()
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 mi-be3600-local-monitor"),
            ("Accept", "application/json, text/javascript, */*; q=0.1"),
        ]

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def _post_form(self, path, data):
        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self.opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def fetch_key(self):
        """从登录页抓取 key 与 deviceId（发起登录设备的 MAC）。"""
        html = self._get("/web")
        m = re.search(r"key:\s*'([^']+)'", html)
        if not m:
            m = re.search(r'"key"\s*:\s*"([^"]+)"', html)
        if not m:
            m = re.search(r"key\s*=\s*'([^']+)'", html)
        if not m:
            raise RuntimeError("无法从登录页解析 key，请确认 ROUTER_HOST 与后台地址正确")
        self.key = m.group(1)
        d = re.search(r"deviceId\s*[=:]\s*'([^']+)'", html)
        self.device_id = d.group(1) if d else "0"

    def _make_nonce(self):
        return f"0_{self.device_id}_{int(time.time())}_{random.randint(0, 9999999)}"

    def login(self):
        """BE3600 登录：sha256(nonce + sha256(password + key))，返回 token(stok)。"""
        self.fetch_key()
        nonce = self._make_nonce()
        inner = hashlib.sha256((self.password + self.key).encode("utf-8")).hexdigest()
        pwd_hash = hashlib.sha256((nonce + inner).encode("utf-8")).hexdigest()
        data = {
            "username": self.user,
            "password": pwd_hash,
            "logtype": "2",
            "nonce": nonce,
        }
        raw = self._post_form("/api/xqsystem/login", data)
        try:
            resp = json.loads(raw)
        except Exception:
            raise RuntimeError("登录接口返回非 JSON：" + raw[:200])
        token = resp.get("token") if isinstance(resp, dict) else None
        if not token:
            raise RuntimeError(f"登录失败：{resp.get('msg') or resp.get('code')}（请检查管理密码）")
        self.token = token
        self.logged_in = True
        return resp

    def _authed_get(self, path, params=None, _retry=True):
        """携带 stok 的已认证请求；token 失效(code=401)时自动重登一次。"""
        if not self.logged_in or not self.token:
            self.login()
        url = f"{self.base}/;stok={self.token}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get("code") == 401 and _retry:
            self.token = None
            self.logged_in = False
            return self._authed_get(path, params=params, _retry=False)
        return payload

    def get_device_list(self):
        """当前连接设备列表。返回 list[dict]。"""
        payload = self._authed_get(
            "/api/misystem/devicelist", {"t": int(time.time() * 1000)}
        )
        return payload.get("list", []) or []

    def get_status(self):
        """路由器状态（uptime/cpu/mem/netdev 等）。"""
        return self._authed_get(
            "/api/misystem/status", {"t": int(time.time() * 1000)}
        )


class DeviceStore:
    """按 MAC 持久化每日流量累计（估算口径）。"""

    def __init__(self, path, retain_days=30):
        self.path = path
        self.retain_days = int(retain_days)
        self.data = {"today": "1970-01-01", "devices": {}}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {"today": "1970-01-01", "devices": {}}

    def _save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
        except Exception:
            pass

    def flush(self):
        """把内存中的当日累计写盘（每轮采集结束调用一次）。"""
        if self._dirty:
            self._save()
            self._dirty = False

    def _roll(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.data.get("today") != today:
            prev = self.data.get("today", "1970-01-01")
            for mac, d in self.data.get("devices", {}).items():
                hist = d.get("history", [])
                hist.append({"date": prev, "down": d.get("down", 0), "up": d.get("up", 0)})
                d["history"] = hist[-self.retain_days:]
                d["down"] = 0
                d["up"] = 0
            self.data["today"] = today
            self._save()
        return today

    def add_sample(self, mac, name, down_bps, up_bps, delta_seconds):
        self._roll()
        dev = self.data.setdefault("devices", {}).setdefault(
            mac, {"name": name, "down": 0, "up": 0, "history": []}
        )
        dev["name"] = name or dev.get("name", "")
        dev["down"] += down_bps * delta_seconds
        dev["up"] += up_bps * delta_seconds

    def add_bytes(self, mac, name, down_bytes, up_bytes):
        """直接累加字节数（基于状态接口累计计数器差值，比速率估算更精确）。"""
        self._roll()
        dev = self.data.setdefault("devices", {}).setdefault(
            mac, {"name": name, "down": 0, "up": 0, "history": []}
        )
        dev["name"] = name or dev.get("name", "")
        dev["down"] += down_bytes
        dev["up"] += up_bytes
        self._dirty = True

    def today_totals(self):
        self._roll()
        out = {}
        for mac, d in self.data.get("devices", {}).items():
            out[mac] = {
                "down": d.get("down", 0),
                "up": d.get("up", 0),
                "name": d.get("name", ""),
                "history": d.get("history", []),
            }
        return out


HISTORY_MEM = 2880         # 内存缓冲点数：最近 24h（30s 采样）
HISTORY_RETAIN = 86400     # 落盘保留点数：30 天（30s 采样）


class TrendStore:
    """全网流量趋势历史：落盘(append-only) + 小内存缓冲 + 长窗口读盘缓存。

    每次采样 append 一行到 <data_dir>/trend.jsonl（管道分隔 t|down|up）；
    内存只保留最近约 24h 供短窗口快速查询，周/月等长窗口按需读盘并缓存，
    稳态内存占用很小；脚本重启后从磁盘恢复历史。
    """

    def __init__(self, path, mem_points=HISTORY_MEM, retain=HISTORY_RETAIN):
        self.path = path
        self.mem_points = int(mem_points)
        self.retain = int(retain)
        self.buf = []          # [(t, down, up)] 内存缓冲（最近 ~24h）
        self.max_ts = 0        # 已追加的最新采样时间
        self._cache = {}       # window -> {"max_ts":.., "result":(trend, stats)}
        self._load_tail()

    def _parse_line(self, line):
        line = line.strip()
        if not line:
            return None
        try:
            t, d, u = line.split("|")
            return (int(t), float(d), float(u))
        except Exception:
            return None

    def _read_all(self):
        pts = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    p = self._parse_line(line)
                    if p:
                        pts.append(p)
        except FileNotFoundError:
            pass
        return pts

    def _write_all(self, pts):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                for t, d, u in pts:
                    f.write(f"{t}|{d:.3f}|{u:.3f}\n")
        except Exception:
            pass

    def _load_tail(self):
        pts = self._read_all()
        if len(pts) > self.retain:
            pts = pts[-self.retain:]
            self._write_all(pts)
        self.buf = pts[-self.mem_points:]
        self.max_ts = pts[-1][0] if pts else 0

    def append(self, t, down, up):
        self.buf.append((t, down, up))
        if len(self.buf) > self.mem_points:
            self.buf = self.buf[-self.mem_points:]
        if t > self.max_ts:
            self.max_ts = t
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{t}|{down:.3f}|{up:.3f}\n")
        except Exception:
            pass

    def points_since(self, start, now):
        if not self.buf:
            return []
        if start >= self.buf[0][0]:          # 短窗口：内存缓冲
            return [p for p in self.buf if p[0] >= start]
        return [p for p in self._read_all() if p[0] >= start]  # 长窗口：读盘

    def cache_get(self, window):
        c = self._cache.get(window)
        if c and c["max_ts"] == self.max_ts:
            return c["result"]
        return None

    def cache_put(self, window, result):
        self._cache[window] = {"max_ts": self.max_ts, "result": result}


class Collector:
    """周期性采集 + 累计，并维护仪表盘数据。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.client = MiWiFiClient(
            cfg["ROUTER_HOST"], cfg["ROUTER_USER"], cfg["ROUTER_PASSWORD"]
        )
        self.store = DeviceStore(cfg.get("DATA_FILE", "/data/miwifi_data.json"),
                                 cfg.get("RETAIN_DAYS", 30))
        self.status = {"ok": False, "last_ok": None, "last_error": "", "online": 0}
        self.rates = {}      # mac -> {down, up, name, type}
        self.last_sample = {}  # mac -> {down, up}
        self.uptime = 0
        self.mem = {}
        self.cpu = {}
        self.wan = {}
        self.hardware = {}
        self.count = {}
        self.temperature = 0
        data_dir = os.path.dirname(cfg.get("DATA_FILE", "/data/miwifi_data.json"))
        self.trend_store = TrendStore(os.path.join(data_dir, "trend.jsonl"))

    def run_once(self):
        try:
            devs = self.client.get_device_list()
            meta = {}
            for d in devs:
                mac = d.get("mac", "")
                if mac:
                    meta[mac] = {
                        "type": d.get("type", 0),
                        "name": d.get("name", "") or d.get("oname", "") or mac,
                    }
            st = self.client.get_status()
            online = 0
            for dev in (st.get("dev") or []):
                mac = dev.get("mac", "")
                if not mac:
                    continue
                # 掉线设备(online 字段为 0)不纳入在线：避免误显示"在线"，且其 type 在
                # devicelist 里查不到会误标为有线。字段缺失视为在线(兼容)。
                if dev.get("online") is not None and not int(dev.get("online") or 0):
                    continue
                online += 1
                m = meta.get(mac, {})
                typ = m.get("type", 0)
                name = m.get("name") or dev.get("devname", "") or mac
                self.rates[mac] = {
                    "down": float(dev.get("downspeed") or 0),
                    "up": float(dev.get("upspeed") or 0),
                    "name": name, "type": typ,
                }
                down_c = float(dev.get("download") or 0)
                up_c = float(dev.get("upload") or 0)
                prev = self.last_sample.get(mac)
                if prev:
                    dd = down_c - prev["down"]
                    du = up_c - prev["up"]
                    if dd >= 0 and du >= 0:  # 计数器回退(重连)时跳过本次
                        self.store.add_bytes(mac, name, dd, du)
                self.last_sample[mac] = {"down": down_c, "up": up_c}
            td = sum(r["down"] for r in self.rates.values())
            tu = sum(r["up"] for r in self.rates.values())
            self.trend_store.append(int(time.time()), td, tu)
            if isinstance(st, dict):
                self.uptime = float(st.get("upTime") or 0)
                self.mem = st.get("mem") or {}
                self.cpu = st.get("cpu") or {}
                self.wan = st.get("wan") or {}
                self.hardware = st.get("hardware") or {}
                self.count = st.get("count") or {}
                self.temperature = st.get("temperature") or 0
            self.status.update({"ok": True, "last_ok": time.time(),
                                "last_error": "", "online": online})
            self.store.flush()
        except Exception as e:
            self.status["ok"] = False
            self.status["last_error"] = str(e)

    def trend(self, minutes=120):
        """近 minutes 分钟的下载/上传速率趋势（采样级）。"""
        return self.rates


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def fmt_bps(n):
    n = float(n or 0) * 8
    for unit in ["bps", "Kbps", "Mbps", "Gbps"]:
        if n < 1000:
            return f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.1f} Gbps"


def conn_meta(t):
    """BE3600 数字 type → (接入文字, 标签类, 频段)。0=有线, 1=2.4G, 2=5G。"""
    t = int(t or 0)
    if t == 0:
        return ("有线", "wire", "-")
    if t == 1:
        return ("无线", "wifi", "2.4G")
    if t == 2:
        return ("无线", "wifi", "5G")
    return ("无线", "wifi", "-")


BJ_OFFSET = 8 * 3600  # 北京时间 UTC+8


def bjtime(ts):
    """把 epoch 秒转成北京时间结构体（不依赖容器时区，始终按 UTC+8）。"""
    return time.gmtime(int(ts) + BJ_OFFSET)


WINDOWS = {"month": 30 * 86400, "week": 7 * 86400, "day": 86400,
           "hour": 3600, "10m": 600}


def _build_trend(store, window="hour"):
    """按窗口取流量趋势（TrendStore），点数过多降采样；带缓存避免频繁读盘。"""
    span = WINDOWS.get(window, 3600)
    now = int(time.time())
    start = now - span
    cached = store.cache_get(window)
    if cached is not None:
        return cached
    pts = store.points_since(start, now)
    if not pts:
        result = ([], {"total_down": "-", "avg_down": "-", "peak_down": "-",
                       "total_up": "-", "avg_up": "-", "peak_up": "-", "points": 0})
    else:
        down_vals = [p[1] for p in pts]
        up_vals = [p[2] for p in pts]
        total_db = total_ub = 0.0
        for i in range(1, len(pts)):
            dt = pts[i][0] - pts[i - 1][0]
            if 0 < dt < 600:
                total_db += (pts[i][1] + pts[i - 1][1]) / 2.0 * dt
                total_ub += (pts[i][2] + pts[i - 1][2]) / 2.0 * dt
        stats = {
            "total_down": fmt_bytes(total_db),
            "avg_down": fmt_bps(sum(down_vals) / len(down_vals)),
            "peak_down": fmt_bps(max(down_vals)),
            "total_up": fmt_bytes(total_ub),
            "avg_up": fmt_bps(sum(up_vals) / len(up_vals)),
            "peak_up": fmt_bps(max(up_vals)),
            "points": len(pts),
        }
        max_points = 720
        if len(pts) <= max_points:
            display = [{"t": time.strftime("%H:%M:%S", bjtime(p[0])),
                        "down": p[1], "up": p[2]} for p in pts]
        else:
            n = len(pts)
            bucket = (n + max_points - 1) // max_points
            display = []
            for i in range(0, n, bucket):
                g = pts[i:i + bucket]
                display.append({
                    "t": time.strftime("%m-%d %H:%M", bjtime(g[-1][0])),
                    "down": sum(x[1] for x in g) / len(g),
                    "up": sum(x[2] for x in g) / len(g),
                })
        result = (display, stats)
    store.cache_put(window, result)
    return result


def build_api_data(cfg, collector, window="hour"):
    daily = collector.store.today_totals()
    # 合并：当前在线设备(rates) + 今日有流量记录的全部设备(daily，含掉线)
    seen = {}
    for mac, r in collector.rates.items():
        seen[mac] = {"online": True, "type": r.get("type", 0),
                     "name": r.get("name", ""), "down": r.get("down", 0), "up": r.get("up", 0)}
    for mac, d in daily.items():
        if mac not in seen:
            seen[mac] = {"online": False, "type": None,
                         "name": d.get("name", ""), "down": 0, "up": 0}
    online_devs = []
    for mac, r in seen.items():
        typ = r["type"]
        if typ is None:
            label, tag, band = "未知", "unk", "-"
        else:
            label, tag, band = conn_meta(typ)
        _d = r["down"]
        _u = r["up"]
        _td = daily.get(mac, {}).get("down", 0)
        _tu = daily.get(mac, {}).get("up", 0)
        online_devs.append({
            "mac": mac, "name": r["name"],
            "type": label, "tag": tag, "bandwidth": band,
            "down": fmt_bps(_d), "up": fmt_bps(_u),
            "today_down": fmt_bytes(_td), "today_up": fmt_bytes(_tu),
            "down_v": _d, "up_v": _u, "td_v": _td, "tu_v": _tu,
            "type_n": (int(typ) if typ is not None else -1),
            "online": r["online"],
        })
    online_devs.sort(key=lambda x: (not x["online"], -(float(x["down"].replace("bps", "").split()[0]) if x["down"] else 0)))
    tot_down = sum(d.get("down", 0) for d in daily.values())
    tot_up = sum(d.get("up", 0) for d in daily.values())
    uptime = int(collector.uptime)
    up_str = f"{uptime // 3600}时{uptime % 3600 // 60}分" if uptime else "-"
    hw = collector.hardware or {}
    cpu = collector.cpu or {}
    mem = collector.mem or {}
    wan = collector.wan or {}
    cnt = collector.count or {}
    trend, trend_stats = _build_trend(collector.trend_store, window)
    return {
        "ok": collector.status["ok"],
        "last_error": collector.status["last_error"],
        "last_ok": collector.status["last_ok"],
        "online": collector.status["online"],
        "today_total_down": fmt_bytes(tot_down),
        "today_total_up": fmt_bytes(tot_up),
        "uptime": up_str,
        "uptime_sec": uptime,
        "router_model": hw.get("displayName", ""),
        "firmware": hw.get("version", "") or hw.get("DisplayRomVer", ""),
        "cpu_load": cpu.get("load"),
        "cpu_hz": cpu.get("hz"),
        "mem_usage": mem.get("usage"),
        "mem_total": mem.get("total"),
        "wan_down": fmt_bps(wan.get("downspeed", 0)),
        "wan_up": fmt_bps(wan.get("upspeed", 0)),
        "wan_total_down": fmt_bytes(wan.get("download", 0)),
        "wan_total_up": fmt_bytes(wan.get("upload", 0)),
        "device_total": cnt.get("all"),
        "device_online": cnt.get("online"),
        "temperature": collector.temperature,
        "trend": trend,
        "trend_stats": trend_stats,
        "online_devices": online_devs,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    cfg = None
    collector = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/data"):
            window = "hour"
            if "?" in self.path:
                from urllib.parse import parse_qs
                w = parse_qs(self.path.split("?", 1)[1]).get("window")
                if w and w[0] in WINDOWS:
                    window = w[0]
            api = build_api_data(self.cfg, self.collector, window)
            body = json.dumps(api, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = HTML_DASHBOARD.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mi-be3600-local-monitor 流量监控</title>
<style>
:root{--bg:#0f1420;--card:#1a2130;--line:#2a3448;--txt:#e6eaf2;--sub:#8a93a8;--ok:#2ecc71;--err:#e74c3c;--acc:#4a9eff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:20px}
h1{font-size:20px;margin-bottom:4px}
.sub{color:var(--sub);font-size:13px;margin-bottom:16px}
.statusbar{display:inline-block;padding:4px 12px;border-radius:6px;font-size:13px;margin-bottom:16px}
.statusbar.ok{background:rgba(46,204,113,.15);color:var(--ok);border:1px solid var(--ok)}
.statusbar.err{background:rgba(231,76,60,.15);color:var(--err);border:1px solid var(--err)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.card .k{color:var(--sub);font-size:12px;margin-bottom:6px}
.card .v{font-size:22px;font-weight:600}
.chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px;height:300px}
.wins{margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap}
.wbtn{background:var(--card);border:1px solid var(--line);color:var(--sub);border-radius:6px;padding:6px 14px;font-size:12px;cursor:pointer}
.wbtn.on{color:#fff;border-color:var(--acc);background:rgba(74,158,255,.18)}
.trendstats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:20px}
.trendstats .s{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.trendstats .k{color:var(--sub);font-size:11px}
.trendstats .v{font-size:15px;font-weight:600;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--sub);font-weight:500}
th[data-key]{cursor:pointer;user-select:none}
th[data-key]:hover{color:var(--txt)}
.tag{padding:2px 8px;border-radius:4px;font-size:11px}
.tag.wifi{background:rgba(74,158,255,.15);color:var(--acc)}
.tag.wire{background:rgba(255,193,7,.15);color:#ffc107}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}
.dot.on{background:#22c55e}
.dot.off{background:#6b7280}
.empty{color:var(--sub);text-align:center;padding:40px}
</style>
</head>
<body>
<h1>mi-be3600-local-monitor 流量监控</h1>
<div class="sub" id="sub">加载中…</div>
<div id="statusbar" class="statusbar">正在连接…</div>
<div class="cards" id="cards"></div>
<div class="wins" id="wins">
<button data-w="10m" class="wbtn">近10分钟</button>
<button data-w="hour" class="wbtn on">近1小时</button>
<button data-w="day" class="wbtn">近1天</button>
<button data-w="week" class="wbtn">近1周</button>
<button data-w="month" class="wbtn">近1月</button>
</div>
<div class="chart" id="chartTrend"></div>
<div class="trendstats" id="trendstats"></div>
<h2 style="font-size:16px;margin-bottom:12px">设备列表</h2>
<table>
<thead id="thead"><tr>
<th data-key="online" data-label="状态">状态</th>
<th data-key="name" data-label="设备">设备</th>
<th data-key="type_n" data-label="接入">接入</th>
<th data-key="type_n" data-label="频段">频段</th>
<th data-key="down_v" data-label="实时下载">实时下载</th>
<th data-key="up_v" data-label="实时上传">实时上传</th>
<th data-key="td_v" data-label="今日下载">今日下载</th>
<th data-key="tu_v" data-label="今日上传">今日上传</th>
</tr></thead>
<tbody id="tbody"></tbody>
</table>
<script>
let trendChart=null;
let currentWindow='hour';
let sortKey='online', sortDir=1;
function fmtUptime(sec){if(!sec)return "-";return Math.floor(sec/3600)+"时"+Math.floor(sec%3600/60)+"分";}
document.getElementById('wins').addEventListener('click',e=>{
  let b=e.target.closest('.wbtn');if(!b)return;
  currentWindow=b.dataset.w;
  document.querySelectorAll('.wbtn').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');refresh();
});
document.querySelectorAll('#thead th[data-key]').forEach(th=>{
  th.addEventListener('click',()=>{
    let k=th.dataset.key;
    if(sortKey===k){sortDir=-sortDir;}else{sortKey=k;sortDir=1;}
    refresh();
  });
});
async function refresh(){
  let r=await fetch('/api/data?window='+currentWindow);let d=await r.json();
  let sb=document.getElementById('statusbar');
  if(d.ok){sb.className='statusbar ok';sb.textContent='采集正常 · 最近成功 '+(d.last_ok?new Date(d.last_ok*1000).toLocaleTimeString():'-');}
  else{sb.className='statusbar err';sb.textContent='异常（登录失败：'+(d.last_error||'未知')+'）';}
  document.getElementById('sub').textContent=(d.router_model||'小米路由器BE3600')+' · 在线设备 '+d.online+' 台';
  let cards=document.getElementById('cards');
  cards.innerHTML=[
    ['在线设备',(d.online??'-')+' / '+(d.device_total??'-')+' 台'],
    ['今日总下载',d.today_total_down],['今日总上传',d.today_total_up],
    ['路由器运行',d.uptime],
    ['WAN 实时',(d.wan_down||'-')+' ↓ / '+(d.wan_up||'-')+' ↑'],
    ['WAN 总流量',(d.wan_total_down||'-')+' ↓ / '+(d.wan_total_up||'-')+' ↑'],
    ['CPU 负载',d.cpu_load!=null?Math.round(d.cpu_load*100)+'%':'-'],
    ['内存使用',d.mem_usage!=null?Math.round(d.mem_usage*100)+'%':' -'],
    ['固件版本',d.firmware||'-']
  ].map(x=>`<div class="card"><div class="k">${x[0]}</div><div class="v">${x[1]}</div></div>`).join('');
  let tb=document.getElementById('tbody');
  document.querySelectorAll('#thead th[data-key]').forEach(th=>{
    let arrow=th.dataset.key===sortKey?(sortDir===1?' ▲':' ▼'):'';
    th.textContent=(th.dataset.label||'')+arrow;
  });
  let arr=d.online_devices.slice();
  arr.sort((a,b)=>{
    let v;
    if(sortKey==='name'){let av=(a.name||'').toLowerCase(),bv=(b.name||'').toLowerCase();v=av<bv?-1:av>bv?1:0;}
    else{let av=a[sortKey]||0,bv=b[sortKey]||0;v=av<bv?-1:av>bv?1:0;}
    return v*sortDir;
  });
  if(arr.length){tb.innerHTML=arr.map(x=>
    `<tr><td>${x.online?'<span class="dot on"></span>在线':'<span class="dot off"></span>离线'}</td>`+
    `<td>${x.name||x.mac}</td><td><span class="tag ${x.tag}">${x.type}</span></td>`+
    `<td>${x.bandwidth||'-'}</td><td>${x.down}</td><td>${x.up}</td><td>${x.today_down}</td><td>${x.today_up}</td></tr>`).join('');
  }else{tb.innerHTML='<tr><td colspan="8" class="empty">暂无设备数据</td></tr>';}
  let hist=d.trend||[];
  let ts=d.trend_stats||{};
  document.getElementById('trendstats').innerHTML=[
    ['总下载',ts.total_down||'-'],['平均下载',ts.avg_down||'-'],['峰值下载',ts.peak_down||'-'],
    ['总上传',ts.total_up||'-'],['平均上传',ts.avg_up||'-'],['峰值上传',ts.peak_up||'-']
  ].map(x=>`<div class="s"><div class="k">${x[0]}</div><div class="v">${x[1]}</div></div>`).join('');
  if(window.echarts){
    if(!trendChart){trendChart=echarts.init(document.getElementById('chartTrend'));}
    trendChart.setOption({
      tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
      legend:{data:['下载','上传'],textStyle:{color:'#8a93a8'},top:0},
      grid:{left:64,right:16,top:30,bottom:26},
      xAxis:{type:'category',data:hist.map(x=>x.t),axisLabel:{color:'#8a93a8',formatter:function(v){return v.length>5?v.slice(0,5):v;}}},
      yAxis:{type:'value',axisLabel:{color:'#8a93a8',formatter:function(v){return fmtS(v);}}},
      series:[
        {name:'下载',type:'line',smooth:true,symbol:'none',data:hist.map(x=>x.down),
         lineStyle:{color:'#4a9eff',width:2},itemStyle:{color:'#4a9eff'},
         areaStyle:{opacity:.18,color:'#4a9eff'}},
        {name:'上传',type:'line',smooth:true,symbol:'none',data:hist.map(x=>x.up),
         lineStyle:{color:'#ffc107',width:2},itemStyle:{color:'#ffc107'},
         areaStyle:{opacity:.18,color:'#ffc107'}}
      ]
    },true);
  }else{
    document.getElementById('chartTrend').innerHTML='<div class="empty">图表库加载失败（网络原因），不影响设备数据</div>';
  }
}
function fmtS(v){v=v||0;if(v<1024)return v.toFixed(0)+' B/s';if(v<1048576)return (v/1024).toFixed(1)+' KB/s';return (v/1048576).toFixed(2)+' MB/s';}
refresh();setInterval(refresh,5000);
(function(){
  if(window.echarts)return;
  var urls=["https://cdn.staticfile.org/echarts/5.4.3/echarts.min.js",
            "https://unpkg.com/echarts@5.4.3/dist/echarts.min.js",
            "https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"];
  var i=0;
  (function next(){
    if(i>=urls.length){document.getElementById('chartTrend').innerHTML='<div class="empty">图表库加载失败（网络原因），不影响设备数据</div>';return;}
    var s=document.createElement('script');s.src=urls[i++];
    s.onload=function(){if(window.echarts)refresh();};s.onerror=next;
    document.head.appendChild(s);
  })();
})();
</script>
</body>
</html>
"""


def main():
    cfg = load_config()
    if not cfg["ROUTER_PASSWORD"]:
        print("[错误] 未设置 ROUTER_PASSWORD（环境变量或 config.json）")
        return 1

    if "--check" in sys.argv:
        try:
            c = MiWiFiClient(cfg["ROUTER_HOST"], cfg["ROUTER_USER"], cfg["ROUTER_PASSWORD"])
            c.login()
            print("检查通过：登录成功 ✔")
            return 0
        except Exception as e:
            print(f"检查失败：{e}")
            return 1

    collector = Collector(cfg)
    Handler.cfg = cfg
    Handler.collector = collector

    if "--once" in sys.argv:
        collector.run_once()
        api = build_api_data(cfg, collector)
        print(json.dumps(api, ensure_ascii=False, indent=2))
        return 0

    def loop():
        while True:
            collector.run_once()
            time.sleep(int(cfg["POLL_INTERVAL"]))

    threading.Thread(target=loop, daemon=True).start()

    port = int(cfg["WEB_PORT"])
    print(f"[启动] 仪表盘: http://{cfg['WEB_HOST']}:{port}")
    http.server.ThreadingHTTPServer(
        (cfg["WEB_HOST"], port), Handler
    ).serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
