#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tsursport iOS —— PC 端服务与 iPhone 系统级定位注入引擎。

架构：
  - 通过 pymobiledevice3 原生异步 API（UserspaceRsdTunnel + DvtProvider +
    LocationSimulation）直接保持 DTX 长连接通道，无需越狱、无需签名、无需装 App。
  - iOS 17.4+ 默认走纯用户态协议栈（无需管理员权限）；iOS 17.0-17.3 自动回退
    RemotePairing / tunneld；iOS < 17 走 Lockdown DtSimulateLocation。
  - 提供 HTTP API 与离线 Leaflet 网页端，PC 桌面端与手机 Safari 遥控实时同步。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import deque
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
import warnings
import webbrowser
import xml.etree.ElementTree as ET

warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", HERE)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else HERE
WEB_DIR = os.path.join(BUNDLE_DIR, "web")
DATA_FILE = os.path.join(APP_DIR, "route_saved.json")
CAMPUS_CENTER = [39.261826, 117.355648]  # GCJ-02
DEFAULT_POINTS = [
    [39.261826, 117.355648],
    [39.262210, 117.356020],
    [39.262530, 117.355480],
    [39.262120, 117.354960],
]

LOCK = threading.RLock()


# ----------------------------------------------------------------- 环境与代理清理
def sanitize_dead_proxy_env() -> list[str]:
    """若系统代理指向不可达的本机端口，临时清理进程内代理变量以免下载 DDI/TSS 失败。"""
    removed = []
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        val = os.environ.get(key)
        if not val:
            continue
        parsed = urlparse(val if "://" in val else f"http://{val}")
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if host in ("127.0.0.1", "localhost", "::1") and port:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            try:
                alive = sock.connect_ex(("127.0.0.1", port)) == 0
            except Exception:
                alive = False
            finally:
                sock.close()
            if not alive:
                os.environ.pop(key, None)
                removed.append(f"{key}={val}")
    return removed


# ----------------------------------------------------------------- 坐标系转换
PI = math.pi
AXIS = 6378245.0
EE = 0.00669342162296594323
BD_X_PI = PI * 3000.0 / 180.0


def _out_of_china(lat: float, lng: float) -> bool:
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _tlat(x: float, y: float) -> float:
    r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * PI) + 20 * math.sin(2 * x * PI)) * 2 / 3
    r += (20 * math.sin(y * PI) + 40 * math.sin(y / 3 * PI)) * 2 / 3
    r += (160 * math.sin(y / 12 * PI) + 320 * math.sin(y * PI / 30)) * 2 / 3
    return r


def _tlng(x: float, y: float) -> float:
    r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * PI) + 20 * math.sin(2 * x * PI)) * 2 / 3
    r += (20 * math.sin(x * PI) + 40 * math.sin(x / 3 * PI)) * 2 / 3
    r += (150 * math.sin(x / 12 * PI) + 300 * math.sin(x / 30 * PI)) * 2 / 3
    return r


def _gcj_delta(lat: float, lng: float) -> tuple[float, float]:
    dlat = _tlat(lng - 105.0, lat - 35.0)
    dlng = _tlng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * PI
    magic = 1.0 - EE * math.sin(rad) ** 2
    sm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((AXIS * (1.0 - EE)) / (magic * sm) * PI)
    dlng = (dlng * 180.0) / (AXIS / sm * math.cos(rad) * PI)
    return dlat, dlng


def gcj_to_wgs(lat: float, lng: float) -> tuple[float, float]:
    """高德 / 地图取点 GCJ-02 → iOS 系统所需的 WGS-84。"""
    if _out_of_china(lat, lng):
        return lat, lng
    dlat, dlng = _gcj_delta(lat, lng)
    return lat - dlat, lng - dlng


def wgs_to_gcj(lat: float, lng: float) -> tuple[float, float]:
    """GPX / iOS WGS-84 → 地图显示用的 GCJ-02。"""
    if _out_of_china(lat, lng):
        return lat, lng
    dlat, dlng = _gcj_delta(lat, lng)
    return lat + dlat, lng + dlng


def gcj_to_bd(lat: float, lng: float) -> tuple[float, float]:
    """GCJ-02 → 百度 BD-09（仅用于坐标核对显示）。"""
    z = math.hypot(lng, lat) + 0.00002 * math.sin(lat * BD_X_PI)
    t = math.atan2(lat, lng) + 0.000003 * math.cos(lng * BD_X_PI)
    return z * math.sin(t) + 0.006, z * math.cos(t) + 0.0065


# ----------------------------------------------------------------- 几何与 GPX
def dist_m(a: list[float] | tuple[float, float], b: list[float] | tuple[float, float]) -> float:
    dx = (b[1] - a[1]) * 111320.0 * math.cos(math.radians((a[0] + b[0]) * 0.5))
    dy = (b[0] - a[0]) * 110540.0
    return math.hypot(dx, dy)


def effective_path(pts: list[list[float]], loop_mode: str) -> list[list[float]]:
    if len(pts) < 2:
        return pts
    if loop_mode == "circle" and len(pts) >= 3 and dist_m(pts[-1], pts[0]) > 1.0:
        return pts + [pts[0]]
    return pts


def route_len(pts: list[list[float]]) -> float:
    return sum(dist_m(pts[i - 1], pts[i]) for i in range(1, len(pts)))


def _point_along(pts: list[list[float]], m: float) -> tuple[float, float, float]:
    """沿折线向前 m 米，返回 (lat, lng, bearing_deg)。"""
    if len(pts) < 2:
        return pts[0][0], pts[0][1], 0.0
    if m <= 0:
        b = _bearing(pts[0], pts[1])
        return pts[0][0], pts[0][1], b
    acc = 0.0
    for i in range(1, len(pts)):
        seg = dist_m(pts[i - 1], pts[i])
        if acc + seg >= m and seg > 1e-6:
            t = (m - acc) / seg
            la = pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * t
            lo = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * t
            return la, lo, _bearing(pts[i - 1], pts[i])
        acc += seg
    return pts[-1][0], pts[-1][1], _bearing(pts[-2], pts[-1])


def _bearing(a: list[float], b: list[float]) -> float:
    dx = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) * 0.5))
    dy = b[0] - a[0]
    deg = math.degrees(math.atan2(dx, dy))
    return deg + 360.0 if deg < 0 else deg


def sample_route(pts: list[list[float]], done_m: float, loop_mode: str) -> tuple[float, float, float, float]:
    """按累计里程与循环模式采样，返回 (lat, lng, bearing_deg, lap_progress)。"""
    path = effective_path(pts, loop_mode)
    lap = max(1.0, route_len(path))
    if loop_mode == "pingpong":
        cycle = 2.0 * lap
        u = done_m % cycle
        if u <= lap:
            la, lo, brg = _point_along(path, u)
        else:
            la, lo, brg = _point_along(path, cycle - u)
            brg = (brg + 180.0) % 360.0
        return la, lo, brg, min(1.0, u / cycle)
    u = done_m % lap
    la, lo, brg = _point_along(path, u)
    return la, lo, brg, min(1.0, u / lap)


def offset_meters(lat: float, lng: float, bearing_deg: float, perp_m: float, along_m: float = 0.0) -> tuple[float, float]:
    """沿航向法线/切线方向平移若干米，用于平滑 GPS 微漂移。"""
    rad = math.radians(bearing_deg)
    # North = cos(brg), East = sin(brg); Right normal = (North=-sin(brg), East=cos(brg))
    north_m = along_m * math.cos(rad) - perp_m * math.sin(rad)
    east_m = along_m * math.sin(rad) + perp_m * math.cos(rad)
    dlat = north_m / 110540.0
    cos_lat = max(0.2, math.cos(math.radians(lat)))
    dlng = east_m / (111320.0 * cos_lat)
    return lat + dlat, lng + dlng


def parse_gpx_to_gcj(xml_text: str, max_points: int = 200) -> list[list[float]]:
    """解析标准 WGS-84 GPX（trkpt/rtept/wpt），转换并抽稀为 GCJ-02 点列表。"""
    root = ET.fromstring(xml_text.lstrip("\ufeff").strip())
    pts_wgs: list[tuple[float, float]] = []
    for tag in ("trkpt", "rtept", "wpt"):
        for elem in root.iter():
            local = elem.tag.rsplit("}", 1)[-1]
            if local == tag:
                lat_s = elem.attrib.get("lat")
                lon_s = elem.attrib.get("lon")
                if lat_s is not None and lon_s is not None:
                    try:
                        pts_wgs.append((float(lat_s), float(lon_s)))
                    except ValueError:
                        pass
        if pts_wgs:
            break
    if len(pts_wgs) < 2:
        raise ValueError("GPX 中未找到至少 2 个有效坐标点（trkpt / rtept / wpt）")
    step = max(1, len(pts_wgs) // max_points)
    sampled = pts_wgs[::step]
    if sampled[-1] != pts_wgs[-1]:
        sampled.append(pts_wgs[-1])
    return [list(wgs_to_gcj(la, lo)) for la, lo in sampled]


def build_gpx_from_gcj(pts_gcj: list[list[float]], speed_mps: float = 2.6, loop_mode: str = "circle") -> str:
    """将内部 GCJ-02 路线转为带时间戳的 WGS-84 GPX。"""
    import datetime

    path = effective_path(pts_gcj, loop_mode)
    t = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="Tsursport iOS" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <trk>",
        "    <name>Tsursport Route</name>",
        "    <trkseg>",
    ]
    spd = max(0.5, float(speed_mps))
    prev = None
    for pt in path:
        if prev is not None:
            t += datetime.timedelta(seconds=max(0.2, dist_m(prev, pt) / spd))
        prev = pt
        wlat, wlng = gcj_to_wgs(pt[0], pt[1])
        lines.append(
            f'      <trkpt lat="{wlat:.7f}" lon="{wlng:.7f}">'
            f'<time>{t.strftime("%Y-%m-%dT%H:%M:%SZ")}</time></trkpt>'
        )
    lines.extend(["    </trkseg>", "  </trk>", "</gpx>"])
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- 纯 Python QR SVG
_QR_CAP_L = {1: 17, 2: 32, 3: 53, 4: 78, 5: 106, 6: 134}
_QR_BLOCKS = {1: [(1, 19, 7)], 2: [(1, 34, 10)], 3: [(1, 55, 15)], 4: [(1, 80, 20)], 5: [(1, 108, 26)], 6: [(2, 68, 18)]}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    return 0 if (a == 0 or b == 0) else _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_ecc(data: list[int], n: int) -> list[int]:
    gen = [1]
    for i in range(n):
        ng = [0] * (len(gen) + 1)
        a = _GF_EXP[i]
        for j, c in enumerate(gen):
            ng[j] ^= c
            ng[j + 1] ^= _gf_mul(c, a)
        gen = ng
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(gen)):
                rem[i + j] ^= _gf_mul(gen[j], c)
    return rem[len(data):]


def qr_svg(text: str) -> str:
    """生成离线可用的纯净 SVG 二维码（支持 UTF-8 ≤ 134 字节，ECC-L）。"""
    raw = text.encode("utf-8")
    ver = next((v for v, cap in _QR_CAP_L.items() if len(raw) <= cap), None)
    if ver is None:
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 40"><text x="6" y="24" font-size="11">URL过长</text></svg>'

    blocks = _QR_BLOCKS[ver]
    data_cw = sum(cnt * dc for cnt, dc, _ in blocks)
    bits: list[int] = []

    def push(val: int, n: int) -> None:
        for k in range(n - 1, -1, -1):
            bits.append((val >> k) & 1)

    push(0b0100, 4)
    push(len(raw), 8)
    for b in raw:
        push(b, 8)
    for _ in range(min(4, data_cw * 8 - len(bits))):
        bits.append(0)
    while len(bits) % 8:
        bits.append(0)
    cw = [sum(bits[i + k] << (7 - k) for k in range(8)) for i in range(0, len(bits), 8)]
    pi = 0
    while len(cw) < data_cw:
        cw.append(0xEC if (pi & 1) == 0 else 0x11)
        pi += 1

    d_blocks, e_blocks, idx = [], [], 0
    for cnt, dc, ec in blocks:
        for _ in range(cnt):
            blk = cw[idx:idx + dc]
            idx += dc
            d_blocks.append(blk)
            e_blocks.append(_rs_ecc(blk, ec))

    interleaved: list[int] = []
    for i in range(max(len(b) for b in d_blocks)):
        for b in d_blocks:
            if i < len(b):
                interleaved.append(b[i])
    for i in range(max(len(b) for b in e_blocks)):
        for b in e_blocks:
            if i < len(b):
                interleaved.append(b[i])

    size = 17 + 4 * ver
    grid: list[list[bool | None]] = [[None] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def put(r: int, c: int, val: bool) -> None:
        grid[r][c] = val
        fixed[r][c] = True

    def finder(r0: int, c0: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    in_outer = 0 <= dr <= 6 and 0 <= dc <= 6
                    dark = in_outer and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4))
                    put(r, c, dark)

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for r in _QR_ALIGN[ver]:
        for c in _QR_ALIGN[ver]:
            if grid[r][c] is not None:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)

    for i in range(8, size - 8):
        if not fixed[6][i]:
            put(6, i, i % 2 == 0)
        if not fixed[i][6]:
            put(i, 6, i % 2 == 0)
    put(size - 8, 8, True)

    for i in range(9):
        if i != 6:
            fixed[8][i] = fixed[i][8] = True
    for i in range(8):
        fixed[8][size - 1 - i] = fixed[size - 1 - i][8] = True

    bitstream = [(byte >> k) & 1 for byte in interleaved for k in range(7, -1, -1)]
    bi, up, col = 0, True, size - 1
    while col > 0:
        if col == 6:
            col -= 1
        for r in (range(size - 1, -1, -1) if up else range(size)):
            for dc in (0, -1):
                cc = col + dc
                if not fixed[r][cc]:
                    grid[r][cc] = bool(bitstream[bi]) if bi < len(bitstream) else False
                    bi += 1
        up = not up
        col -= 2

    masks = (
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    )
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
               (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    coords2 = [(size - 1, 8), (size - 2, 8), (size - 3, 8), (size - 4, 8), (size - 5, 8),
               (size - 6, 8), (size - 7, 8), (8, size - 8), (8, size - 7), (8, size - 6),
               (8, size - 5), (8, size - 4), (8, size - 3), (8, size - 2), (8, size - 1)]

    best_pen, best_m = 10**9, None
    for mask_id, fn in enumerate(masks):
        m = [row[:] for row in grid]
        for r in range(size):
            for c in range(size):
                if not fixed[r][c] and fn(r, c):
                    m[r][c] = not m[r][c]
        data5 = (0b01 << 3) | mask_id
        rem = data5 << 10
        for k in range(14, 9, -1):
            if (rem >> k) & 1:
                rem ^= 0b10100110111 << (k - 10)
        fb = ((data5 << 10) | rem) ^ 0b101010000010010
        for i in range(15):
            v = bool((fb >> (14 - i)) & 1)
            m[coords1[i][0]][coords1[i][1]] = v
            m[coords2[i][0]][coords2[i][1]] = v

        pen = 0
        for r in range(size):
            run = 1
            for c in range(1, size):
                if m[r][c] == m[r][c - 1]:
                    run += 1
                    pen += 3 if run == 5 else (1 if run > 5 else 0)
                else:
                    run = 1
        for c in range(size):
            run = 1
            for r in range(1, size):
                if m[r][c] == m[r - 1][c]:
                    run += 1
                    pen += 3 if run == 5 else (1 if run > 5 else 0)
                else:
                    run = 1
        for r in range(size - 1):
            for c in range(size - 1):
                if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                    pen += 3
        if pen < best_pen:
            best_pen, best_m = pen, m

    quiet = 3
    vb = size + quiet * 2
    rects = []
    for r in range(size):
        for c in range(size):
            if best_m[r][c]:
                rects.append(f'<rect x="{c + quiet}" y="{r + quiet}" width="1" height="1"/>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb} {vb}" shape-rendering="crispEdges">'
        f'<rect width="{vb}" height="{vb}" fill="#ffffff"/>'
        f'<g fill="#0d1117">{"".join(rects)}</g></svg>'
    )


# ----------------------------------------------------------------- 网络与防火墙
_VIRTUAL_IFACE_HINTS = ("vmware", "vmnet", "virtualbox", "vbox", "hyper-v", "vethernet", "wsl", "loopback", "meta", "clash", "tun", "tap")


def lan_ips() -> list[dict]:
    """枚举可用局域网 IPv4 地址，优先真实无线/有线网卡，排除虚拟网段干扰。"""
    results: list[dict] = []
    seen: set[str] = set()
    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            name = str(adapter.nice_name or adapter.name or "")
            is_virt = any(h in name.lower() for h in _VIRTUAL_IFACE_HINTS)
            for ip in adapter.ips:
                addr = ip.ip if isinstance(ip.ip, str) else None
                if not addr or addr in seen or addr.startswith("127.") or addr.startswith("169.254.") or addr.startswith("198.18."):
                    continue
                seen.add(addr)
                results.append({"ip": addr, "adapter": name, "virtual": is_virt})
    except Exception:
        pass

    # UDP 探测兜底排序
    primary = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("114.114.114.114", 80))
        primary = s.getsockname()[0]
    except Exception:
        try:
            s.connect(("8.8.8.8", 80))
            primary = s.getsockname()[0]
        except Exception:
            pass
    finally:
        s.close()

    if primary and primary not in seen and not primary.startswith("127."):
        results.insert(0, {"ip": primary, "adapter": "默认路由", "virtual": False})

    results.sort(key=lambda item: (item["virtual"], item["ip"] != primary, item["ip"]))
    if not results:
        results.append({"ip": "127.0.0.1", "adapter": "Loopback", "virtual": True})
    return results


def lan_ip() -> str:
    return lan_ips()[0]["ip"]


def check_amds_service() -> dict:
    """检查 Windows Apple Mobile Device Service 安装与运行状态（无需管理员权限）。"""
    if sys.platform != "win32":
        return {"installed": True, "running": True, "source": "non-windows"}
    installed = False
    running = False
    source = "missing"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Services\Apple Mobile Device Service",
        ) as k:
            img, _ = winreg.QueryValueEx(k, "ImagePath")
            installed = True
            source = "Apple 设备 (Store)" if "windowsapps" in str(img).lower() else "Apple Mobile Device Support"
    except OSError:
        pass
    if installed:
        try:
            r = subprocess.run(
                ["sc", "query", "Apple Mobile Device Service"],
                capture_output=True, text=True, errors="ignore", timeout=3
            )
            running = "RUNNING" in (r.stdout or "").upper()
        except Exception:
            pass
    return {"installed": installed, "running": running, "source": source}


def check_firewall_hint(port: int) -> dict:
    """检查 Windows 防火墙是否可能拦截局域网手机访问。"""
    cmd = f'netsh advfirewall firewall add rule name="Tsursport iOS ({port})" dir=in action=allow protocol=TCP localport={port}'
    if sys.platform != "win32":
        return {"ok": True, "detail": "非 Windows 环境", "command": ""}
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"],
            capture_output=True, text=True, errors="ignore", timeout=4
        )
        out = r.stdout or ""
        if str(port) in out or "Tsursport" in out:
            return {"ok": True, "detail": f"已发现端口 {port} 相关放行规则", "command": cmd}
        return {"ok": False, "detail": f"未发现端口 {port} 的显式入站规则（若手机白屏超时请放行）", "command": cmd}
    except Exception as e:
        return {"ok": False, "detail": f"无法检查防火墙: {e}", "command": cmd}


# ----------------------------------------------------------------- iOS 调试桥接（直连 pymobiledevice3）
class IOSController:
    """管理专用 SelectorEventLoop 线程，直接操作 pymobiledevice3 隧道与定位模拟通道。"""

    def __init__(self, log_fn=None, mock_mode: bool = False):
        self._log = log_fn or (lambda msg: None)
        self._loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="ios-asyncio", daemon=True)
        self._thread.start()

        self.mock_mode = bool(mock_mode)
        self._mock_connected = False
        self._mock_last_log = 0.0
        self.mock_trace: deque[tuple[float, float, float]] = deque(maxlen=200)

        # 活跃会话状态（仅在 _loop 内变更）
        self._tunnel = None
        self._rsd = None
        self._lockdown = None
        self._dvt = None
        self._loc_sim = None
        self._legacy_sim = None
        self._mode = None  # "userspace" | "tunneld" | "lockdown" | "mock-dtx"

        # 缓存快照（线程安全读）
        self.snap_lock = threading.Lock()
        self.snapshot = {
            "mock_mode": self.mock_mode,
            "pmd3_installed": self._check_pmd3(),
            "usbmux_ok": None,
            "usbmux_error": "",
            "amds": check_amds_service(),
            "devices": [],
            "connected": False,
            "tunnel_mode": None,
            "active_udid": "",
            "active_name": "",
            "active_model": "",
            "active_ios": "",
            "dev_mode": None,
            "ddi_mounted": None,
            "hint": "正在检测环境…",
            "last_error": "",
        }
        self._last_probe_ts = 0.0

    @staticmethod
    def _check_pmd3() -> bool:
        try:
            import pymobiledevice3  # noqa: F401
            return True
        except Exception:
            return False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout: float = 35.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def _update_snap(self, **kwargs) -> None:
        with self.snap_lock:
            self.snapshot.update(kwargs)

    def get_snapshot(self) -> dict:
        with self.snap_lock:
            return dict(self.snapshot)

    def _has_active_session(self) -> bool:
        return self._mock_connected or self._loc_sim is not None or self._legacy_sim is not None

    def set_mock_mode(self, enabled: bool) -> dict:
        enabled = bool(enabled)
        if enabled == self.mock_mode:
            return self.get_snapshot()
        self.disconnect(clear_first=True)
        self.mock_mode = enabled
        self._mock_connected = False
        self._update_snap(mock_mode=enabled, connected=False, tunnel_mode=None, last_error="")
        if enabled:
            self._log("已开启「虚拟 iPhone 演练模式」（仅本地模拟推点，未连接真实设备）")
        else:
            self._log("已切回「真机模式」，正在重新扫描设备…")
        return self.probe(force=True)

    # ---- 环境探测（不触发信任弹窗）
    async def _probe_async(self) -> dict:
        if self.mock_mode:
            self._update_snap(
                mock_mode=True,
                pmd3_installed=True,
                usbmux_ok=True,
                usbmux_error="",
                amds={"installed": True, "running": True, "source": "虚拟 iPhone 演示环境"},
                devices=[{
                    "udid": "00008140-DEMO-IPHONE17PRO",
                    "connection": "USB (演练)",
                    "name": "演示用 iPhone 17 Pro",
                    "model": "iPhone 17 Pro",
                    "ios_version": "26.1",
                    "paired": True,
                    "dev_mode": True,
                }],
                active_udid="00008140-DEMO-IPHONE17PRO" if self._mock_connected else "",
                active_name="演示用 iPhone 17 Pro" if self._mock_connected else "",
                active_model="iPhone 17 Pro" if self._mock_connected else "",
                active_ios="26.1" if self._mock_connected else "",
                dev_mode=True,
                ddi_mounted=True if self._mock_connected else None,
                hint="演练模式已开启：虚拟 iPhone 17 Pro (iOS 26.1 · 仅本地仿真)",
            )
            return self.get_snapshot()

        if not self._check_pmd3():
            self._update_snap(
                pmd3_installed=False, usbmux_ok=False, connected=False,
                hint="缺少 pymobiledevice3，请运行 pip install -U pymobiledevice3"
            )
            return self.get_snapshot()

        # 若已处于活跃模拟通道，直接返回活跃态，避免干扰 PyTCP 单例栈
        if self._has_active_session():
            return self.get_snapshot()

        from packaging.version import Version
        from pymobiledevice3 import usbmux
        from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError
        from pymobiledevice3.lockdown import create_using_usbmux

        devices_out = []
        usbmux_ok = True
        usbmux_err = ""
        raw_devs = []

        try:
            raw_devs = await asyncio.wait_for(usbmux.list_devices(), timeout=3.0)
        except ConnectionFailedToUsbmuxdError as e:
            usbmux_ok = False
            usbmux_err = str(e)
        except Exception as e:
            usbmux_ok = False
            usbmux_err = f"{type(e).__name__}: {e}"

        # 优先 USB 设备
        raw_devs.sort(key=lambda d: (0 if d.is_usb else 1, d.serial))
        for d in raw_devs:
            info = {
                "udid": d.serial,
                "connection": d.connection_type,
                "name": "iPhone",
                "model": "",
                "ios_version": "",
                "paired": False,
                "dev_mode": None,
            }
            ld = None
            try:
                ld = await asyncio.wait_for(
                    create_using_usbmux(serial=d.serial, autopair=False),
                    timeout=3.5,
                )
                info["name"] = (ld.all_values or {}).get("DeviceName") or ld.display_name or "iPhone"
                info["model"] = ld.display_name or ld.product_type or ""
                info["ios_version"] = ld.product_version or ""
                info["paired"] = bool(ld.paired)
                if ld.paired and info["ios_version"]:
                    if Version(info["ios_version"]).major >= 16:
                        with suppress(Exception):
                            info["dev_mode"] = await ld.get_developer_mode_status()
            except Exception as e:
                info["error"] = str(e)
            finally:
                if ld is not None:
                    with suppress(Exception):
                        await ld.close()
            devices_out.append(info)

        amds = check_amds_service()
        if not usbmux_ok:
            if amds["installed"] and not amds["running"]:
                hint = "Apple 移动设备服务已安装但未运行，请启动服务"
            else:
                hint = "未检测到 Apple 移动设备服务 (usbmuxd)：请安装 Apple Mobile Device Support（或微软商店「Apple 设备」/ iTunes）"
        elif not devices_out:
            hint = "未检测到 iPhone：请用数据线连接并解锁屏幕（若已配对 Wi-Fi 请确保同局域网）"
        else:
            primary = devices_out[0]
            if not primary["paired"]:
                hint = f"发现 {primary['name']}，请在 iPhone 上解锁并点击「信任此电脑」"
            elif primary["dev_mode"] is False:
                hint = f"发现 {primary['name']} (iOS {primary['ios_version']})，需先打开「设置 → 隐私与安全性 → 开发者模式」并重启"
            else:
                hint = f"已就绪：{primary['name']} ({primary['model']} · iOS {primary['ios_version']})"

        self._update_snap(
            pmd3_installed=True,
            usbmux_ok=usbmux_ok,
            usbmux_error=usbmux_err,
            amds=amds,
            devices=devices_out,
            hint=hint,
        )
        return self.get_snapshot()

    def probe(self, force: bool = False) -> dict:
        now = time.time()
        if not force and now - self._last_probe_ts < 2.5:
            return self.get_snapshot()
        self._last_probe_ts = now
        try:
            return self._call(self._probe_async(), timeout=8.0)
        except Exception as e:
            self._update_snap(last_error=str(e))
            return self.get_snapshot()

    # ---- 建立持久连接与定位通道
    async def _close_session_async(self, clear_first: bool = False) -> None:
        if clear_first:
            with suppress(Exception):
                if self._loc_sim is not None:
                    await asyncio.wait_for(self._loc_sim.clear(), timeout=3.0)
                elif self._legacy_sim is not None:
                    await asyncio.wait_for(self._legacy_sim.clear(), timeout=3.0)

        for obj, method in (
            (self._loc_sim, "close"),
            (self._dvt, "close"),
            (self._tunnel, "aclose"),
            (self._rsd, "close"),
            (self._lockdown, "close"),
        ):
            if obj is not None:
                with suppress(Exception):
                    await asyncio.wait_for(getattr(obj, method)(), timeout=3.0)

        self._loc_sim = None
        self._dvt = None
        self._tunnel = None
        self._rsd = None
        self._lockdown = None
        self._legacy_sim = None
        self._mode = None
        self._update_snap(connected=False, tunnel_mode=None)

    async def _connect_session_async(self, serial: str | None = None) -> dict:
        if self.mock_mode:
            if not self._mock_connected:
                await asyncio.sleep(0.35)
                self._mock_connected = True
                self._mode = "mock-dtx"
                self._update_snap(
                    mock_mode=True,
                    connected=True,
                    tunnel_mode="mock-dtx",
                    active_udid="00008140-DEMO-IPHONE17PRO",
                    active_name="演示用 iPhone 17 Pro",
                    active_model="iPhone 17 Pro",
                    active_ios="26.1",
                    dev_mode=True,
                    ddi_mounted=True,
                    last_error="",
                    hint="演练模式已连线：演示用 iPhone 17 Pro (iOS 26.1 · mock-dtx)",
                )
                self._log("[演练模式] 已建立虚拟安全隧道与 DTX 定位通道（未接真机）")
            return self.get_snapshot()

        if self._has_active_session():
            return self.get_snapshot()

        sanitize_dead_proxy_env()
        from packaging.version import Version
        from pymobiledevice3 import usbmux
        from pymobiledevice3.exceptions import (
            AlreadyMountedError,
            ConnectionFailedToUsbmuxdError,
            DeveloperDiskImageServiceError,
            InvalidServiceError,
            NoDeviceConnectedError,
            NotPairedError,
            UserspaceTunnelUnavailableError,
        )
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.services.amfi import AmfiService
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.mobile_image_mounter import auto_mount
        from pymobiledevice3.services.simulate_location import DtSimulateLocation
        from pymobiledevice3.tunneld.api import TUNNELD_DEFAULT_ADDRESS, get_tunneld_devices

        # 1. 查找设备（USB 优先）
        try:
            devs = await asyncio.wait_for(usbmux.list_devices(), timeout=4.0)
        except ConnectionFailedToUsbmuxdError as e:
            raise RuntimeError(
                "未检测到 Apple 移动设备服务 (usbmuxd)。请先安装 Apple Mobile Device Support（或微软商店「Apple 设备」/ iTunes）。"
            ) from e

        if not devs:
            raise RuntimeError("未检测到 iPhone：请用数据线连接电脑、解锁手机并点击「信任此电脑」。")

        devs.sort(key=lambda d: (0 if d.is_usb else 1, d.serial))
        target = next((d for d in devs if d.serial == serial), devs[0]) if serial else devs[0]
        target_serial = target.serial

        # 2. Lockdown 握手与前置校验
        ld = await asyncio.wait_for(create_using_usbmux(serial=target_serial, autopair=True), timeout=15.0)
        keep_ld = False
        try:
            if not ld.paired:
                raise RuntimeError("设备尚未配对：请在 iPhone 弹出提示时点击「信任」并输入锁屏密码。")
            name = (ld.all_values or {}).get("DeviceName") or ld.display_name or "iPhone"
            model = ld.display_name or ld.product_type or ""
            ios_ver = ld.product_version or "0"
            udid = ld.udid or target_serial
            ver = Version(ios_ver)
            dev_mode = True
            if ver.major >= 16:
                dev_mode = await ld.get_developer_mode_status()
                if not dev_mode:
                    with suppress(Exception):
                        await AmfiService(ld).reveal_developer_mode_option_in_ui()
                    raise RuntimeError(
                        "iPhone 未开启「开发者模式」：已尝试显示开关，请到「设置 → 隐私与安全性 → 开发者模式」打开并重启。"
                    )
            if ver < Version("17.0"):
                self._lockdown = ld
                keep_ld = True
        finally:
            if not keep_ld:
                with suppress(Exception):
                    await ld.close()

        self._update_snap(
            active_udid=udid,
            active_name=name,
            active_model=model,
            active_ios=ios_ver,
            dev_mode=dev_mode,
        )

        # 3. iOS < 17 旧通道
        if ver < Version("17.0"):
            try:
                sim = DtSimulateLocation(self._lockdown)
                self._legacy_sim = sim
                self._mode = "lockdown"
                self._update_snap(connected=True, tunnel_mode="lockdown", last_error="", hint=f"已连接 {name} (iOS {ios_ver})")
                self._log(f"已连接 {name} (iOS {ios_ver} · Lockdown)")
                return self.get_snapshot()
            except Exception as e:
                await self._close_session_async()
                raise RuntimeError(f"iOS < 17 通道建立失败：{e}") from e

        # 4. iOS 17+ / 26+：纯用户态安全隧道（免管理员）→ 回退 tunneld
        rsd = None
        mode = "userspace"
        try:
            self._log(f"正在与 {name} (iOS {ios_ver}) 建立用户态隧道…")
            tunnel = UserspaceRsdTunnel(serial=udid, autopair=True, remotepairing_fallback=True)
            rsd = await asyncio.wait_for(tunnel.aopen(), timeout=25.0)
            self._tunnel = tunnel
        except UserspaceTunnelUnavailableError as ue:
            self._log("用户态隧道不适用，尝试连接本机 tunneld…")
            rsds = await get_tunneld_devices(TUNNELD_DEFAULT_ADDRESS)
            rsd = next((r for r in rsds if r.udid == udid), rsds[0] if rsds else None)
            for other in rsds:
                if other is not rsd:
                    with suppress(Exception):
                        await other.close()
            if rsd is None:
                raise RuntimeError(
                    "iOS 17.0–17.3 需开启同局域网 Wi-Fi，或以管理员身份运行 pymobiledevice3 remote tunneld。"
                ) from ue
            self._rsd = rsd
            mode = "tunneld"
        except (NotPairedError, NoDeviceConnectedError) as e:
            await self._close_session_async()
            raise RuntimeError(f"设备连接或配对失败：{e}") from e
        except Exception as e:
            await self._close_session_async()
            raise RuntimeError(f"隧道建立失败：{e}") from e

        # 5. 打开 DVT + LocationSimulation（若缺 DDI 自动挂载）
        try:
            dvt = DvtProvider(rsd)
            try:
                await asyncio.wait_for(dvt.connect(), timeout=12.0)
            except (InvalidServiceError, DeveloperDiskImageServiceError):
                with suppress(Exception):
                    await dvt.close()
                self._log("检测到未挂载开发者镜像，正在自动下载并挂载 DeveloperDiskImage…")
                with suppress(AlreadyMountedError):
                    await asyncio.wait_for(auto_mount(rsd), timeout=60.0)
                dvt = DvtProvider(rsd)
                await asyncio.wait_for(dvt.connect(), timeout=12.0)

            loc_sim = LocationSimulation(dvt)
            await asyncio.wait_for(loc_sim.connect(), timeout=10.0)
            self._dvt = dvt
            self._loc_sim = loc_sim
            self._mode = mode
            self._update_snap(
                connected=True,
                tunnel_mode=mode,
                ddi_mounted=True,
                last_error="",
                hint=f"已连接 {name} (iOS {ios_ver} · {mode})",
            )
            self._log(f"隧道与定位通道已就绪：{name} ({model} · iOS {ios_ver})")
            return self.get_snapshot()
        except Exception as e:
            await self._close_session_async()
            raise RuntimeError(f"打开 DVT 定位通道失败：{e}") from e

    def connect(self, serial: str | None = None) -> dict:
        try:
            return self._call(self._connect_session_async(serial), timeout=75.0)
        except Exception as e:
            msg = str(e)
            self._update_snap(connected=False, last_error=msg, hint=msg)
            raise

    def disconnect(self, clear_first: bool = True) -> dict:
        if self._mock_connected:
            self._mock_connected = False
            self._mode = None
            self._update_snap(connected=False, tunnel_mode=None, ddi_mounted=None)
        with suppress(Exception):
            self._call(self._close_session_async(clear_first=clear_first), timeout=8.0)
        return self.get_snapshot()

    async def _set_loc_async(self, lat_wgs: float, lng_wgs: float) -> None:
        if self._mock_connected:
            now = time.time()
            self.mock_trace.append((now, lat_wgs, lng_wgs))
            if now - self._mock_last_log >= 2.5:
                self._mock_last_log = now
                self._log(f"[虚拟DTX] 注入 WGS-84: {lat_wgs:.6f}, {lng_wgs:.6f}")
            return
        if self._loc_sim is not None:
            await asyncio.wait_for(self._loc_sim.set(lat_wgs, lng_wgs), timeout=5.0)
            return
        if self._legacy_sim is not None:
            from pymobiledevice3.exceptions import DeveloperDiskImageServiceError, InvalidServiceError
            from pymobiledevice3.services.mobile_image_mounter import auto_mount

            try:
                await asyncio.wait_for(self._legacy_sim.set(lat_wgs, lng_wgs), timeout=5.0)
            except (InvalidServiceError, DeveloperDiskImageServiceError):
                await asyncio.wait_for(auto_mount(self._lockdown), timeout=60.0)
                await asyncio.wait_for(self._legacy_sim.set(lat_wgs, lng_wgs), timeout=5.0)
            return
        raise RuntimeError("定位通道尚未建立")

    def set_location_wgs(self, lat_wgs: float, lng_wgs: float) -> None:
        try:
            self._call(self._set_loc_async(lat_wgs, lng_wgs), timeout=8.0)
        except Exception as e:
            with suppress(Exception):
                self._call(self._close_session_async(clear_first=False), timeout=5.0)
            msg = f"定位写入失败（可能已拔线或锁屏断连）：{e}"
            self._update_snap(connected=False, last_error=msg, hint=msg)
            raise RuntimeError(msg) from e

    def clear_location(self, disconnect: bool = False) -> None:
        if self._mock_connected:
            if disconnect:
                self.disconnect(clear_first=True)
            return
        if disconnect:
            self.disconnect(clear_first=True)
            return
        try:
            async def _clr():
                if self._loc_sim is not None:
                    await asyncio.wait_for(self._loc_sim.clear(), timeout=4.0)
                elif self._legacy_sim is not None:
                    await asyncio.wait_for(self._legacy_sim.clear(), timeout=4.0)

            self._call(_clr(), timeout=6.0)
        except Exception:
            self.disconnect(clear_first=False)

    def pair_device(self) -> tuple[bool, str]:
        """主动向已连接的 iPhone 发起配对/信任弹窗。"""
        if self.mock_mode:
            return True, "[演练模式] 已配对虚拟设备：演示用 iPhone 17 Pro"

        async def _act():
            from pymobiledevice3 import usbmux
            from pymobiledevice3.exceptions import (
                ConnectionFailedToUsbmuxdError,
                PairingDialogResponsePendingError,
                PasscodeRequiredError,
                PasswordRequiredError,
                UserDeniedPairingError,
            )
            from pymobiledevice3.lockdown import create_using_usbmux

            try:
                devs = await asyncio.wait_for(usbmux.list_devices(), timeout=3.0)
            except ConnectionFailedToUsbmuxdError:
                return False, "Apple 移动设备服务未运行，请先完成第 1 步驱动安装"
            if not devs:
                return False, "未检测到已连接的 iPhone，请先插好数据线并解锁屏幕"
            devs.sort(key=lambda d: (0 if d.is_usb else 1, d.serial))
            ld = None
            try:
                ld = await asyncio.wait_for(create_using_usbmux(serial=devs[0].serial, autopair=True), timeout=18.0)
                if ld.paired:
                    name = (ld.all_values or {}).get("DeviceName") or ld.display_name or "iPhone"
                    return True, f"已成功配对并信任：{name}"
                return False, "配对尚未完成，请在手机上点击「信任」后重试"
            except PairingDialogResponsePendingError:
                return False, "信任弹窗已在 iPhone 上显示，请点击「信任」并输入锁屏密码后稍候"
            except UserDeniedPairingError:
                return False, "你在 iPhone 上点了「不信任」，请重新拔插数据线后再试"
            except (PasscodeRequiredError, PasswordRequiredError):
                return False, "iPhone 处于锁定状态，请先解锁手机屏幕再试"
            finally:
                if ld is not None:
                    with suppress(Exception):
                        await ld.close()

        try:
            ok, msg = self._call(_act(), timeout=22.0)
            self.probe(force=True)
            return ok, msg
        except Exception as e:
            return False, f"配对请求失败：{e}"

    def reveal_dev_mode(self) -> tuple[bool, str]:
        if self.mock_mode:
            return True, "[演练模式] 虚拟 iPhone 开发者模式已开启"

        async def _act():
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.services.amfi import AmfiService

            ld = await create_using_usbmux(autopair=True)
            try:
                if await ld.get_developer_mode_status():
                    return True, "开发者模式已是开启状态"
                await AmfiService(ld).reveal_developer_mode_option_in_ui()
                return True, "已在 iPhone 显示「开发者模式」选项，请打开并按提示重启"
            finally:
                await ld.close()

        try:
            return self._call(_act(), timeout=15.0)
        except Exception as e:
            return False, f"唤醒失败：{e}"

    def enable_wifi_connections(self) -> tuple[bool, str]:
        if self.mock_mode:
            return True, "[演练模式] 已模拟开启 Wi-Fi 无线调试支持"

        async def _act():
            from pymobiledevice3.lockdown import create_using_usbmux

            ld = await create_using_usbmux(autopair=True)
            try:
                await ld.set_enable_wifi_connections(True)
                return True, "已开启该设备的 Wi-Fi 无线连接支持（需与电脑同局域网）"
            finally:
                await ld.close()

        try:
            return self._call(_act(), timeout=15.0)
        except Exception as e:
            return False, f"开启 Wi-Fi 支持失败：{e}"

    def run_doctor(self) -> dict:
        """运行 pymobiledevice3.doctor 全链路体检并附带局域网/防火墙信息。"""
        if self.mock_mode:
            return {
                "environment": "虚拟 iPhone 演练环境 (非真机)",
                "has_problems": False,
                "checks": [
                    {"scope": "Host", "title": "usbmux daemon", "status": "OK", "detail": "虚拟 Apple 移动设备服务已就绪", "impact": None, "hint": None},
                    {"scope": "Host", "title": "Tunnel (userspace)", "status": "OK", "detail": "mock-dtx 仿真通道", "impact": None, "hint": None},
                    {"scope": "Device", "title": "Device", "status": "OK", "detail": "演示用 iPhone 17 Pro (iOS 26.1), paired", "impact": None, "hint": None},
                    {"scope": "Device", "title": "Developer mode", "status": "OK", "detail": "enabled (mock)", "impact": None, "hint": None},
                ],
            }

        async def _doc():
            from pymobiledevice3.doctor import DEVICE, HOST, run_checks

            rep = await run_checks()
            checks = []
            for scope, lst in ((HOST, rep.host), (DEVICE, rep.device)):
                for c in lst:
                    hint = c.hint
                    if c.title == "usbmux daemon" and c.status.value == "FAIL" and sys.platform == "win32":
                        hint = "未找到 Apple 移动设备服务，请执行 winget install Apple.AppleMobileDeviceSupport（或安装 Apple 设备 / iTunes）"
                    checks.append({
                        "scope": scope,
                        "title": c.title,
                        "status": c.status.value,
                        "detail": c.detail,
                        "impact": c.impact,
                        "hint": hint,
                    })
            return {
                "environment": rep.environment,
                "has_problems": bool(rep.problems),
                "checks": checks,
            }

        try:
            if self._has_active_session():
                # 不打断运行中隧道，返回已有快照摘要
                snap = self.get_snapshot()
                return {
                    "environment": f"活跃通道 ({snap['tunnel_mode']})",
                    "has_problems": False,
                    "checks": [
                        {
                            "scope": "Device",
                            "title": "活跃定位隧道",
                            "status": "OK",
                            "detail": f"{snap['active_name']} (iOS {snap['active_ios']})",
                            "impact": None,
                            "hint": None,
                        }
                    ],
                }
            return self._call(_doc(), timeout=20.0)
        except Exception as e:
            return {"environment": "unknown", "has_problems": True, "checks": [{"scope": "Host", "title": "体检异常", "status": "FAIL", "detail": str(e), "impact": None, "hint": None}]}


# ----------------------------------------------------------------- 模拟跑步运行器
class Runner:
    def __init__(self, dev: IOSController):
        self.dev = dev
        self.points: list[list[float]] = [list(p) for p in DEFAULT_POINTS]
        self.speed: float = 2.6
        self.loop: str = "pingpong"  # "pingpong" | "circle"
        self.target_m: float = 0.0    # 0 = 无限循环
        self.jitter: bool = True
        self.drift: int = 1           # 0=关闭, 1=±1.5m, 2=±3.0m
        self.freq_hz: float = 2.0     # 1.0 or 2.0 Hz
        self.presets: dict[str, list[list[float]]] = {
            "默认校内环线": [list(p) for p in DEFAULT_POINTS],
        }

        self.state: str = "idle"      # "idle" | "connecting" | "running" | "paused" | "pinned" | "finished"
        self.done: float = 0.0
        self.elapsed: float = 0.0
        self.cur_gcj: list[float] = list(self.points[0])
        self.eff_speed: float = self.speed
        self.msg: str = "待机"
        self.route_ver: int = 1
        self.logs: deque[str] = deque(maxlen=150)

        self._stop_evt = threading.Event()
        self._worker: threading.Thread | None = None
        self._perp_m = 0.0
        self._along_m = 0.0
        self._speed_noise = 0.0
        self._load()
        self.dev._log = self.log

    def log(self, text: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        with LOCK:
            self.logs.append(line)

    def _load(self) -> None:
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                j = json.load(f)
            pts = [[float(p[0]), float(p[1])] for p in j.get("points", []) if len(p) >= 2]
            if len(pts) >= 2:
                self.points = pts
                self.cur_gcj = list(pts[0])
            self.speed = max(0.8, min(15.0, float(j.get("speed", self.speed))))
            raw_loop = j.get("loop", self.loop)
            if isinstance(raw_loop, bool):
                self.loop = "circle" if raw_loop else "pingpong"
            elif raw_loop in ("pingpong", "circle"):
                self.loop = raw_loop
            self.target_m = max(0.0, float(j.get("target_m", 0.0)))
            self.jitter = bool(j.get("jitter", True))
            self.drift = max(0, min(2, int(j.get("drift", 1))))
            self.freq_hz = 1.0 if float(j.get("freq_hz", 2.0)) <= 1.2 else 2.0
            if isinstance(j.get("presets"), dict):
                loaded_presets = {}
                for k, v in j["presets"].items():
                    if isinstance(v, list) and len(v) >= 2:
                        loaded_presets[str(k)] = [[float(p[0]), float(p[1])] for p in v]
                if loaded_presets:
                    self.presets = loaded_presets
        except Exception:
            pass

    def save(self) -> None:
        try:
            payload = {
                "points": self.points,
                "speed": self.speed,
                "loop": self.loop,
                "target_m": self.target_m,
                "jitter": self.jitter,
                "drift": self.drift,
                "freq_hz": self.freq_hz,
                "presets": self.presets,
            }
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def total(self) -> float:
        return max(1.0, route_len(effective_path(self.points, self.loop)))

    def _stop_worker(self) -> None:
        self._stop_evt.set()
        w = self._worker
        if w is not None and w.is_alive() and w is not threading.current_thread():
            w.join(timeout=2.0)
        self._worker = None

    def start(self) -> None:
        with LOCK:
            if self.state in ("connecting", "running"):
                return
            if self.state == "paused" and self._worker is not None and self._worker.is_alive():
                self.state = "running"
                self.msg = f"运行中 · {self.speed:.1f} m/s"
                self.log("继续模拟")
                return
            if len(self.points) < 2:
                self.msg = "路线不足两个点"
                self.log(self.msg)
                return

        self._stop_worker()
        with LOCK:
            if self.state in ("finished", "pinned"):
                self.done = 0.0
                self.elapsed = 0.0
            self.state = "connecting"
            self.msg = "正在连接 iPhone 与安全隧道…"
            self._stop_evt.clear()
            self._worker = threading.Thread(target=self._run_loop, name="tsursport-runner", daemon=True)
            self._worker.start()

    def pause(self) -> None:
        with LOCK:
            if self.state == "running":
                self.state = "paused"
                self.msg = "已暂停（当前坐标已锁定在 iPhone）"
                self.log(self.msg)

    def clear(self, disconnect: bool = False) -> None:
        self._stop_worker()
        with LOCK:
            self.state = "idle"
            self.done = 0.0
            self.elapsed = 0.0
            self.cur_gcj = list(self.points[0]) if self.points else list(CAMPUS_CENTER)
            self.msg = "已恢复真实定位"
        self.dev.clear_location(disconnect=disconnect)
        self.log("已发送 clear，恢复 iPhone 真实定位")

    def pin(self, lat_gcj: float, lng_gcj: float) -> None:
        self._stop_worker()
        with LOCK:
            self.cur_gcj = [float(lat_gcj), float(lng_gcj)]
            self.state = "connecting"
            self.msg = "正在定身到指定坐标…"
            self._stop_evt.clear()
            self._worker = threading.Thread(target=self._pin_loop, name="tsursport-pin", daemon=True)
            self._worker.start()

    def _pin_loop(self) -> None:
        try:
            self.dev.connect()
            with LOCK:
                la, lo = self.cur_gcj
                self.state = "pinned"
                self.msg = f"已定身：{la:.6f}, {lo:.6f}"
            self.log(self.msg)
            wlat, wlng = gcj_to_wgs(la, lo)
            self.dev.set_location_wgs(wlat, wlng)
            while not self._stop_evt.wait(3.0):
                with LOCK:
                    if self.state != "pinned":
                        break
                    la, lo = self.cur_gcj
                wlat, wlng = gcj_to_wgs(la, lo)
                self.dev.set_location_wgs(wlat, wlng)
        except Exception as e:
            with LOCK:
                self.state = "idle"
                self.msg = str(e)
            self.log(f"定身失败：{e}")

    def _run_loop(self) -> None:
        try:
            self.dev.connect()
        except Exception as e:
            with LOCK:
                self.state = "idle"
                self.msg = str(e)
            self.log(f"连接失败：{e}")
            return

        with LOCK:
            if self._stop_evt.is_set():
                return
            self.state = "running"
            self.msg = f"运行中 · {self.speed:.1f} m/s"
        self.log(f"开始模拟：{self.speed:.1f} m/s ({self.loop})")

        last_t = time.monotonic()
        last_keepalive = 0.0
        while not self._stop_evt.is_set():
            now = time.monotonic()
            dt = min(1.5, max(0.05, now - last_t))
            last_t = now

            with LOCK:
                cur_state = self.state
                pts = list(self.points)
                spd = self.speed
                loop_mode = self.loop
                target_m = self.target_m
                jitter = self.jitter
                drift = self.drift
                hz = self.freq_hz

            if cur_state == "paused":
                if now - last_keepalive >= 4.0:
                    last_keepalive = now
                    with LOCK:
                        la, lo = self.cur_gcj
                    wlat, wlng = gcj_to_wgs(la, lo)
                    try:
                        self.dev.set_location_wgs(wlat, wlng)
                    except Exception as e:
                        with LOCK:
                            self.state = "idle"
                            self.msg = str(e)
                        self.log(str(e))
                        return
                self._stop_evt.wait(0.35)
                continue

            if cur_state != "running":
                break

            # 自然速度抖动（±8% 平滑随机游走 + 慢速正弦呼吸感）
            if jitter:
                self._speed_noise = self._speed_noise * 0.72 + random.uniform(-0.06, 0.06) * 0.28
                wave = math.sin(now * 0.45) * 0.035
                eff = max(0.5, spd * (1.0 + max(-0.08, min(0.08, self._speed_noise + wave))))
            else:
                eff = spd

            with LOCK:
                self.done += eff * dt
                self.elapsed += dt
                self.eff_speed = eff
                reached_target = target_m > 0 and self.done >= target_m
                if reached_target:
                    self.done = target_m
                la, lo, brg, _ = sample_route(pts, self.done, loop_mode)
                if drift > 0:
                    max_d = 1.5 if drift == 1 else 3.2
                    self._perp_m = max(-max_d, min(max_d, self._perp_m * 0.82 + random.uniform(-0.45, 0.45)))
                    self._along_m = max(-0.8, min(0.8, self._along_m * 0.8 + random.uniform(-0.2, 0.2)))
                    la, lo = offset_meters(la, lo, brg, self._perp_m, self._along_m)
                self.cur_gcj = [la, lo]
                if reached_target:
                    self.state = "finished"
                    self.msg = f"已达目标 {int(target_m)} m（位置已锁定，请结束运动后点「恢复真实定位」）"

            wlat, wlng = gcj_to_wgs(la, lo)
            try:
                self.dev.set_location_wgs(wlat, wlng)
            except Exception as e:
                with LOCK:
                    self.state = "idle"
                    self.msg = str(e)
                self.log(str(e))
                return

            if reached_target:
                self.log(self.msg)
                # 到达目标里程后保持锁定，直到用户手动恢复真实定位
                while not self._stop_evt.wait(3.0):
                    with LOCK:
                        if self.state != "finished":
                            return
                        la2, lo2 = self.cur_gcj
                    try:
                        self.dev.set_location_wgs(*gcj_to_wgs(la2, lo2))
                    except Exception:
                        return
                return

            self._stop_evt.wait(1.0 / hz)

    def update_config(self, data: dict) -> None:
        with LOCK:
            if "points" in data and isinstance(data["points"], list):
                pts = [[float(p[0]), float(p[1])] for p in data["points"] if len(p) >= 2]
                if self.state in ("running", "paused") and len(pts) < 2:
                    raise ValueError("运行中路线不能少于 2 个点")
                self.points = pts
                self.route_ver += 1
                if self.state == "idle" and pts:
                    self.cur_gcj = list(pts[0])
                    self.done = 0.0
            if "speed" in data:
                self.speed = max(0.8, min(15.0, float(data["speed"])))
                if self.state == "running":
                    self.msg = f"运行中 · {self.speed:.1f} m/s"
            if "loop" in data and data["loop"] in ("pingpong", "circle"):
                self.loop = data["loop"]
            if "target_m" in data:
                self.target_m = max(0.0, float(data["target_m"]))
            if "jitter" in data:
                self.jitter = bool(data["jitter"])
            if "drift" in data:
                self.drift = max(0, min(2, int(data["drift"])))
            if "freq_hz" in data:
                self.freq_hz = 1.0 if float(data["freq_hz"]) <= 1.2 else 2.0
            self.save()

    @staticmethod
    def _pace(v: float) -> str:
        if v <= 0.05:
            return "--"
        s = 1000.0 / v
        m = int(s // 60)
        sec = int(round(s % 60))
        if sec == 60:
            m += 1
            sec = 0
        return f"{m}'{sec:02d}\""

    def status(self) -> dict:
        with LOCK:
            pts = self.points
            lap_total = round(route_len(effective_path(pts, self.loop)), 1) if len(pts) >= 2 else 0.0
            if len(pts) >= 2:
                _, _, _, lap_prog = sample_route(pts, self.done, self.loop)
            else:
                lap_prog = 0.0
            prog = min(1.0, self.done / self.target_m) if self.target_m > 0 else lap_prog
            wlat, wlng = gcj_to_wgs(self.cur_gcj[0], self.cur_gcj[1])
            snap = self.dev.get_snapshot()
            return {
                "state": self.state,
                "playing": self.state == "running",
                "paused": self.state == "paused",
                "connecting": self.state == "connecting",
                "pinned": self.state == "pinned",
                "finished": self.state == "finished",
                "speed": round(self.speed, 2),
                "eff_speed": round(self.eff_speed if self.state == "running" else self.speed, 2),
                "pace": self._pace(self.speed),
                "done": round(self.done, 1),
                "elapsed": int(self.elapsed),
                "total": lap_total,
                "target_m": int(self.target_m),
                "progress": round(prog, 3),
                "points_count": len(pts),
                "route_ver": self.route_ver,
                "loop": self.loop,
                "jitter": self.jitter,
                "drift": self.drift,
                "freq_hz": self.freq_hz,
                "message": self.msg,
                "lat": round(self.cur_gcj[0], 7),
                "lng": round(self.cur_gcj[1], 7),
                "wgs_lat": round(wlat, 7),
                "wgs_lng": round(wlng, 7),
                "device": snap,
                "logs": list(self.logs)[-40:],
            }


# ----------------------------------------------------------------- HTTP 服务
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

RUNNER: Runner | None = None
DEVICE: IOSController | None = None
SERVER_PORT: int = 8765


class QuietHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with suppress(Exception):
            self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _static(self, rel_path: str) -> None:
        rel = rel_path.lstrip("/").replace("/", os.sep)
        if rel == "web" or rel.startswith("web" + os.sep):
            sub = rel[4:] if rel.startswith("web" + os.sep) else "index.html"
        else:
            sub = rel
        web_root = os.path.abspath(WEB_DIR)
        full = os.path.abspath(os.path.join(web_root, sub))
        try:
            inside = os.path.commonpath([web_root, full]) == web_root
        except ValueError:
            inside = False
        if not inside or not os.path.isfile(full):
            self._send(404, b"not found", "text/plain")
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._static("web/index.html")
        if u.path.startswith("/web/"):
            return self._static(u.path)

        assert RUNNER is not None and DEVICE is not None

        if u.path == "/api/status":
            if q.get("probe") == ["1"]:
                DEVICE.probe(force=False)
            return self._json(RUNNER.status())

        if u.path == "/api/route":
            with LOCK:
                return self._json({
                    "points": RUNNER.points,
                    "speed": RUNNER.speed,
                    "loop": RUNNER.loop,
                    "target_m": RUNNER.target_m,
                    "jitter": RUNNER.jitter,
                    "drift": RUNNER.drift,
                    "freq_hz": RUNNER.freq_hz,
                    "presets": RUNNER.presets,
                })

        if u.path == "/api/netinfo":
            ips = lan_ips()
            urls = [{"url": f"http://{item['ip']}:{SERVER_PORT}", **item} for item in ips]
            return self._json({
                "port": SERVER_PORT,
                "primary_url": urls[0]["url"],
                "urls": urls,
                "firewall": check_firewall_hint(SERVER_PORT),
            })

        if u.path == "/api/qr.svg":
            target_url = (q.get("url") or [f"http://{lan_ip()}:{SERVER_PORT}"])[0]
            return self._send(200, qr_svg(target_url).encode("utf-8"), "image/svg+xml; charset=utf-8")

        if u.path == "/api/export_gpx":
            with LOCK:
                if len(RUNNER.points) < 2:
                    return self._json({"error": "路线不足两点"}, 400)
                gpx = build_gpx_from_gcj(RUNNER.points, RUNNER.speed, RUNNER.loop)
            self.send_response(200)
            self.send_header("Content-Type", "application/gpx+xml; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="tsursport-route.gpx"')
            raw = gpx.encode("utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            with suppress(Exception):
                self.wfile.write(raw)
            return

        if u.path == "/api/start":
            RUNNER.start()
            return self._json(RUNNER.status())

        if u.path == "/api/stop" or u.path == "/api/pause":
            RUNNER.pause()
            return self._json(RUNNER.status())

        if u.path == "/api/clear":
            RUNNER.clear(disconnect=False)
            return self._json(RUNNER.status())

        if u.path == "/api/disconnect":
            RUNNER.clear(disconnect=True)
            return self._json(RUNNER.status())

        if u.path == "/api/speed":
            try:
                RUNNER.update_config({"speed": float(q.get("v", ["2.6"])[0])})
            except Exception:
                pass
            return self._json(RUNNER.status())

        if u.path == "/api/device":
            return self._json(DEVICE.probe(force=True))

        if u.path == "/api/pair":
            ok, msg = DEVICE.pair_device()
            RUNNER.log(msg)
            return self._json({"ok": ok, "message": msg, "device": DEVICE.get_snapshot()}, 200 if ok else 400)

        if u.path == "/api/connect":
            try:
                snap = DEVICE.connect()
                return self._json({"ok": True, "device": snap})
            except Exception as e:
                RUNNER.log(f"预连接失败：{e}")
                return self._json({"ok": False, "error": str(e), "device": DEVICE.get_snapshot()}, 400)

        if u.path == "/api/doctor":
            rep = DEVICE.run_doctor()
            rep["netinfo"] = {
                "port": SERVER_PORT,
                "urls": [{"url": f"http://{item['ip']}:{SERVER_PORT}", **item} for item in lan_ips()],
                "firewall": check_firewall_hint(SERVER_PORT),
            }
            return self._json(rep)

        if u.path == "/api/reveal_dev_mode":
            ok, msg = DEVICE.reveal_dev_mode()
            RUNNER.log(msg)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        if u.path == "/api/enable_wifi":
            ok, msg = DEVICE.enable_wifi_connections()
            RUNNER.log(msg)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        if u.path == "/api/mock":
            en = (q.get("enabled") or ["1"])[0] in ("1", "true", "yes")
            if RUNNER.state in ("running", "paused", "connecting", "pinned", "finished"):
                RUNNER.clear(disconnect=True)
            snap = DEVICE.set_mock_mode(en)
            return self._json({"ok": True, "device": snap, "status": RUNNER.status()})

        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        assert RUNNER is not None

        try:
            body = json.loads(raw.decode("utf-8", errors="ignore") or "{}")
        except Exception as e:
            return self._json({"error": f"无效 JSON: {e}"}, 400)

        if u.path == "/api/route":
            try:
                RUNNER.update_config(body)
                return self._json(RUNNER.status())
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if u.path == "/api/pin":
            try:
                RUNNER.pin(float(body["lat"]), float(body["lng"]))
                return self._json(RUNNER.status())
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if u.path == "/api/import_gpx":
            try:
                xml_text = str(body.get("gpx") or "")
                pts = parse_gpx_to_gcj(xml_text)
                RUNNER.update_config({"points": pts})
                RUNNER.log(f"已导入 GPX：{len(pts)} 个路线点（已自动 WGS-84 → GCJ-02）")
                return self._json({"points": pts, "status": RUNNER.status()})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if u.path == "/api/presets":
            action = body.get("action")
            name = str(body.get("name") or "").strip()
            with LOCK:
                if action == "save" and name:
                    pts = body.get("points") or RUNNER.points
                    if len(pts) < 2:
                        return self._json({"error": "路线至少需要 2 个点才能保存"}, 400)
                    RUNNER.presets[name] = [[float(p[0]), float(p[1])] for p in pts]
                    RUNNER.save()
                    RUNNER.log(f"已保存预设路线：{name}")
                elif action == "load" and name in RUNNER.presets:
                    RUNNER.update_config({"points": RUNNER.presets[name]})
                    RUNNER.log(f"已切换路线：{name}")
                elif action == "delete" and name in RUNNER.presets:
                    del RUNNER.presets[name]
                    RUNNER.save()
                    RUNNER.log(f"已删除预设：{name}")
                else:
                    return self._json({"error": "未知预设操作或名称不存在"}, 400)
                return self._json({"presets": RUNNER.presets, "points": RUNNER.points, "status": RUNNER.status()})

        if u.path == "/api/host_action":
            client_ip = (self.client_address[0] or "").strip()
            if client_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                return self._json({"ok": False, "error": "仅允许本机控制台触发系统操作"}, 403)
            act = str(body.get("action") or "").strip()
            res = run_host_action(act, SERVER_PORT, RUNNER)
            return self._json(res, 200 if res.get("ok") else 400)

        self._send(404, b"not found", "text/plain")


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _ps_encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def _run_ps_uac(script: str) -> dict:
    if sys.platform != "win32":
        return {"ok": False, "error": "仅支持 Windows"}
    elevated = _ps_encoded(script)
    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-EncodedCommand','{elevated}'",
            ],
            creationflags=_NO_WINDOW,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_host_action(action: str, port: int, runner: Runner | None = None) -> dict:
    """在本机桌面端触发防火墙放行、Apple 驱动安装/启动或打开微软商店。"""
    if action == "firewall":
        return _run_ps_uac(
            f'netsh advfirewall firewall delete rule name="Tsursport iOS ({port})" | Out-Null; '
            f'netsh advfirewall firewall add rule name="Tsursport iOS ({port})" '
            f'dir=in action=allow protocol=TCP localport={port}'
        )
    if action == "start_amds":
        return _run_ps_uac("Start-Service 'Apple Mobile Device Service'")
    if action == "install_driver":
        if sys.platform != "win32":
            return {"ok": False, "error": "仅支持 Windows"}
        if not shutil.which("winget"):
            return {"ok": False, "error": "当前系统未找到 winget，请改用微软商店安装「Apple 设备」"}
        enc = _ps_encoded(
            "$Host.UI.RawUI.WindowTitle = 'Tsursport - 安装 Apple 移动设备驱动'\n"
            "Write-Host '正在通过 winget 安装 Apple Mobile Device Support (官方轻量驱动)...' -ForegroundColor Cyan\n"
            "winget install Apple.AppleMobileDeviceSupport --accept-package-agreements --accept-source-agreements\n"
            "Start-Service 'Apple Mobile Device Service' -ErrorAction SilentlyContinue\n"
            "Write-Host ''\n"
            "Write-Host '执行结束，可关闭此窗口并回到 Tsursport 点「刷新检测」。' -ForegroundColor Green\n"
            "Read-Host '按回车键关闭'\n"
        )
        try:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-EncodedCommand", enc],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            if runner is not None:
                runner.log("已打开安装窗口：正在安装 Apple Mobile Device Support")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    if action == "open_store":
        try:
            if sys.platform == "win32":
                os.startfile("ms-windows-store://pdp/?ProductId=9NP83LWLPZ9K")
            else:
                webbrowser.open("https://apps.microsoft.com/detail/9NP83LWLPZ9K")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"未知本机操作: {action}"}


# ----------------------------------------------------------------- 服务启动与自检
def create_app_server(port: int = 8765, host: str = "0.0.0.0", mock_mode: bool = False) -> tuple[ThreadingHTTPServer, IOSController, Runner, int]:
    """创建并初始化后台控制器、运行器与 HTTP 服务（端口占用时自动顺延尝试）。"""
    global RUNNER, DEVICE, SERVER_PORT
    sanitize_dead_proxy_env()
    dev = IOSController(mock_mode=mock_mode)
    runner = Runner(dev)

    # 启动背景设备轮询（仅在空闲时探测，不阻塞启动）
    def _bg_probe():
        while True:
            with LOCK:
                idle = runner.state == "idle"
            if idle:
                dev.probe(force=False)
            time.sleep(3.5)

    threading.Thread(target=_bg_probe, name="ios-probe", daemon=True).start()

    srv = None
    chosen = port
    for p in range(port, port + 15):
        try:
            srv = QuietHTTPServer((host, p), Handler)
            chosen = p
            break
        except OSError:
            continue
    if srv is None:
        srv = QuietHTTPServer((host, 0), Handler)
        chosen = srv.server_address[1]

    DEVICE = dev
    RUNNER = runner
    SERVER_PORT = chosen
    return srv, dev, runner, chosen


def env_check() -> int:
    """命令行环境体检：检查 Python、pymobiledevice3、Apple 驱动服务与已连接 iPhone。"""
    sanitize_dead_proxy_env()
    print("=" * 58)
    print("  Tsursport iOS 环境自检")
    print("=" * 58)
    print(f"· Python: {sys.version.split()[0]}")
    dev = IOSController()
    snap = dev.probe(force=True)
    doc = dev.run_doctor()
    print(f"· pymobiledevice3: {'已安装' if snap['pmd3_installed'] else '未安装'}")
    for c in doc.get("checks", []):
        tag = f"[{c['status']}]"
        print(f"  {tag:6s} {c['title']}: {c['detail']}")
        if c.get("hint"):
            print(f"         建议: {c['hint']}")
    print("· 局域网地址:")
    for item in lan_ips():
        mark = " (虚拟网卡)" if item["virtual"] else ""
        print(f"    http://{item['ip']}:{SERVER_PORT}  [{item['adapter']}{mark}]")
    return 0 if not doc.get("has_problems") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Tsursport iOS 定位模拟控制台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--mock", action="store_true", help="启动虚拟 iPhone 演练模式（无需连接真机）")
    ap.add_argument("--check", action="store_true", help="运行环境自检后退出")
    ap.add_argument("--open", action="store_true", help="启动后在默认浏览器打开本机控制页")
    args = ap.parse_args()

    if args.check:
        return env_check()

    srv, dev, runner, port = create_app_server(port=args.port, host=args.host, mock_mode=args.mock)
    ips = lan_ips()
    primary = ips[0]["ip"]
    print("=" * 60)
    print("  Tsursport iOS 服务已启动")
    print(f"  本机控制台： http://127.0.0.1:{port}/?desktop=1")
    for item in ips[:3]:
        tag = "虚拟" if item["virtual"] else item["adapter"]
        print(f"  手机遥控：   http://{item['ip']}:{port}  [{tag}]")
    print("  Safari 分享 → 添加到主屏幕，可全屏使用")
    print("  按 Ctrl+C 退出（会自动恢复 iPhone 真实定位）")
    print("=" * 60)

    if args.open:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}/?desktop=1")

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n正在恢复 iPhone 真实定位并退出…")
    finally:
        with suppress(Exception):
            runner.clear(disconnect=True)
        with suppress(Exception):
            srv.shutdown()
            srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
