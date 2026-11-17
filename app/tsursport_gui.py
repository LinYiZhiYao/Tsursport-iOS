#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tsursport iOS 桌面应用 —— 内嵌 Edge WebView2 交互式地图与设备控制台。

特点：
  - 复用 web/index.html，支持真实地图点选/拖拽路线、GPX 导入导出、轨迹 PNG 导出
  - 同进程启动 HTTP 服务，手机 Safari 扫码即可同步遥控
  - 找不到 pywebview 时自动回退到 Edge --app 模式，保证任何情况下都能启动
  - 退出时自动恢复 iPhone 真实定位
"""

from __future__ import annotations

import argparse
import base64
from contextlib import suppress
import ctypes
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import webbrowser

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
def _resolve_app_data():
    """便携模式：exe 同目录存在 portable.flag 时，把运行数据放在 exe 旁边的 data 目录里。
    这样不会写入 %LOCALAPPDATA%，避免被系统策略拒绝（拒绝访问/错误 5）。"""
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    if os.path.exists(os.path.join(base, "portable.flag")):
        d = os.path.join(base, "data")
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".ok")
            with open(probe, "w") as f:
                f.write("1")
            os.remove(probe)
            return d
        except Exception:
            pass
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Tsursport")


_APP_DATA = _resolve_app_data()
_LOG_FILE = os.path.join(_APP_DATA, "gui.log")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

if sys.stdout is None or sys.stderr is None:
    try:
        os.makedirs(_APP_DATA, exist_ok=True)
        _log_fh = open(_LOG_FILE, "w", encoding="utf-8", buffering=1)
    except Exception:
        _log_fh = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log_fh
    if sys.stderr is None:
        sys.stderr = _log_fh


def _append_log(msg: str) -> None:
    """将关键启动与异常日志即时追加写入 gui.log（避免缓冲导致 0 字节）。"""
    try:
        os.makedirs(_APP_DATA, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as lf:
            lf.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _prepare_clean_dotnet_runtime() -> str | None:
    """冻结运行时将 .NET/WebView2 相关组件同步至 %LOCALAPPDATA% 普通权限目录，避免 HRESULT 0x80131515。"""
    default_icon = os.path.join(core.BUNDLE_DIR, "tsursport.ico")
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return default_icon if os.path.isfile(default_icon) else None

    src_root = core.BUNDLE_DIR
    dst_root = os.path.join(_APP_DATA, "dotnet_rt")
    try:
        for sub in ("clr_loader", "pythonnet", os.path.join("webview", "lib")):
            sp = os.path.join(src_root, sub)
            if not os.path.isdir(sp):
                continue
            for dp, _, fs in os.walk(sp):
                rel = os.path.relpath(dp, src_root)
                tdp = os.path.join(dst_root, rel)
                os.makedirs(tdp, exist_ok=True)
                for fn in fs:
                    sf = os.path.join(dp, fn)
                    df = os.path.join(tdp, fn)
                    if not os.path.isfile(df) or os.path.getsize(df) != os.path.getsize(sf):
                        shutil.copyfile(sf, df)
        if os.path.isfile(default_icon):
            dst_icon = os.path.join(dst_root, "tsursport.ico")
            if not os.path.isfile(dst_icon) or os.path.getsize(dst_icon) != os.path.getsize(default_icon):
                shutil.copyfile(default_icon, dst_icon)
            icon_out = dst_icon
        else:
            icon_out = None
    except Exception as err:
        _append_log(f"sync dotnet_rt warn: {err}")
        return default_icon if os.path.isfile(default_icon) else None

    try:
        import clr_loader.ffi as _clr_ffi
        import pythonnet as _pynet
        import webview.util as _wv_util

        _clr_ffi.__file__ = os.path.join(dst_root, "clr_loader", "ffi", "__init__.py")
        _pynet.__file__ = os.path.join(dst_root, "pythonnet", "__init__.py")

        _orig_interop = _wv_util.interop_dll_path
        _wv_lib = os.path.join(dst_root, "webview", "lib")

        def _clean_interop_dll_path(dll_name: str) -> str:
            name = dll_name
            if name == "WebBrowserInterop.dll":
                name = "WebBrowserInterop.x64.dll" if sys.maxsize > 2**32 else "WebBrowserInterop.x86.dll"
            for cand in (
                os.path.join(_wv_lib, name),
                os.path.join(_wv_lib, "runtimes", name, "native"),
            ):
                if os.path.exists(cand):
                    return cand
            return _orig_interop(dll_name)

        _wv_util.interop_dll_path = _clean_interop_dll_path
        native_dir = os.path.join(_wv_lib, "runtimes", "win-x64" if sys.maxsize > 2**32 else "win-x86", "native")
        if os.path.isdir(native_dir):
            os.environ["PATH"] = native_dir + os.pathsep + os.environ.get("PATH", "")
            with suppress(Exception):
                os.add_dll_directory(native_dir)
    except Exception as err:
        _append_log(f"patch dotnet_rt warn: {err}")

    return icon_out


def _show_error_box(title: str, msg: str) -> None:
    """在无控制台 GUI 模式下弹窗提示启动异常，避免静默闪退。"""
    if sys.platform == "win32":
        with suppress(Exception):
            ctypes.windll.user32.MessageBoxW(0, str(msg), str(title), 0x10)
            return
    with suppress(Exception):
        print(f"[{title}] {msg}", file=sys.stderr)

import tsursport_pc as core  # noqa: E402


class DesktopAPI:
    """暴露给 pywebview 前端的原生桥接（文件对话框、剪贴板、系统操作）。"""

    def __init__(self, runner: core.Runner, port: int):
        self._runner = runner
        self._port = port
        self._window = None

    def pick_gpx(self) -> dict:
        import webview

        if self._window is None:
            return {"ok": False, "error": "窗口未就绪"}
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("GPX 文件 (*.gpx)", "所有文件 (*.*)"),
        )
        if not paths:
            return {"ok": False, "canceled": True}
        path = paths[0]
        try:
            with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                xml_text = f.read()
            pts = core.parse_gpx_to_gcj(xml_text)
            self._runner.update_config({"points": pts})
            self._runner.log(f"已从文件导入 GPX：{os.path.basename(path)}（{len(pts)} 点）")
            return {"ok": True, "points": pts, "name": os.path.basename(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def save_gpx(self, default_name: str, gpx_text: str) -> dict:
        import webview

        if self._window is None:
            return {"ok": False}
        res = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=default_name or "tsursport-route.gpx",
            file_types=("GPX 文件 (*.gpx)",),
        )
        if not res:
            return {"ok": False, "canceled": True}
        out_path = res[0] if isinstance(res, (list, tuple)) else str(res)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(gpx_text)
        self._runner.log(f"已导出 GPX：{out_path}")
        return {"ok": True, "path": out_path}

    def save_png(self, default_name: str, data_url: str) -> dict:
        import webview

        if self._window is None:
            return {"ok": False}
        res = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            save_filename=default_name or "tsursport-track.png",
            file_types=("PNG 图片 (*.png)",),
        )
        if not res:
            return {"ok": False, "canceled": True}
        out_path = res[0] if isinstance(res, (list, tuple)) else str(res)
        b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64))
        self._runner.log(f"已保存轨迹图：{out_path}")
        return {"ok": True, "path": out_path}

    def copy_text(self, text: str) -> bool:
        try:
            import win32clipboard

            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(str(text), win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return True
        except Exception:
            return False

    def open_external(self, url: str) -> bool:
        webbrowser.open(url)
        return True

    def open_firewall_uac(self) -> dict:
        """显式触发 Windows UAC 提权添加当前端口入站放行规则。"""
        return core.run_host_action("firewall", self._port, self._runner)

    def install_apple_driver(self) -> dict:
        """在可见的 PowerShell 窗口中通过 winget 安装 Apple Mobile Device Support 官方驱动。"""
        return core.run_host_action("install_driver", self._port, self._runner)

    def start_amds_uac(self) -> dict:
        """显式触发 UAC 启动已安装的 Apple Mobile Device Service 服务。"""
        return core.run_host_action("start_amds", self._port, self._runner)

    def open_apple_store(self) -> dict:
        """打开微软商店「Apple 设备」官方下载页。"""
        return core.run_host_action("open_store", self._port, self._runner)


def _find_edge_exe() -> str | None:
    for exe in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if os.path.isfile(exe):
            return exe
    return None


def _launch_browser_fallback(url: str) -> None:
    exe = _find_edge_exe()
    if exe:
        subprocess.Popen([exe, f"--app={url}", "--window-size=1200,800"])
        return
    webbrowser.open(url)


def _edge_profile_candidates():
    """Edge profile 候选目录，绝不落在程序目录里（避免污染 dist）。"""
    data = _APP_DATA
    yield os.path.join(data, "edge_app")
    yield os.path.join(data, "edge_app_%d" % os.getpid())
    local = os.environ.get("LOCALAPPDATA") or ""
    if local:
        yield os.path.join(local, "Temp", "Tsursport-edge-%d" % os.getpid())


def _edge_profile_temp():
    """最后兜底：系统临时目录下新建唯一目录（失败返回 None）。"""
    try:
        return tempfile.mkdtemp(prefix="Tsursport-edge-")
    except Exception:
        return None


def _writable(path: str) -> bool:
    probe = os.path.join(path, ".tsursport_write_test")
    with open(probe, "w", encoding="utf-8") as f:
        f.write("ok")
    os.remove(probe)
    return True


def _launch_edge_app_and_wait(url: str) -> bool:
    """用 Edge 独立 profile 拉起 --app 窗口；任何异常都降级，绝不向上抛。"""
    try:
        exe = _find_edge_exe()
    except Exception as e:
        _append_log(f"find edge failed: {e}")
        return False
    if not exe:
        _append_log("edge not found")
        return False

    candidates = list(_edge_profile_candidates())
    extra = _edge_profile_temp()
    if extra:
        candidates.append(extra)

    for profile in candidates:
        try:
            os.makedirs(profile, exist_ok=True)
            _writable(profile)
        except OSError as e:
            _append_log(f"edge profile unusable ({profile}): {e}")
            continue
        try:
            proc = subprocess.Popen(
                [
                    exe,
                    f"--app={url}",
                    f"--user-data-dir={profile}",
                    "--window-size=1200,800",
                    "--no-first-run",
                    "--no-default-browser-check",
                ]
            )
            _append_log(f"edge app started, profile={profile}")
            proc.wait()
            return True
        except Exception as e:
            _append_log(f"edge app start failed ({profile}): {e}")
            continue
    return False



def _show_info_window(url: str, port: int) -> None:
    """所有内嵌窗口都失败时的兜底提示窗：不是错误框，而是告知浏览器模式地址。"""
    try:
        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        root.title("Tsursport - 浏览器模式")
        root.geometry("520x220")
        root.configure(bg="#0f1319")
        msg = (
            "内嵌窗口不可用，已改用系统浏览器打开控制台。\n\n"
            f"电脑端：{url}\n"
            f"手机端：http://{core.lan_ip()}:{port}\n\n"
            "关闭本窗口会停止后台定位服务并恢复真实定位。"
        )
        tk.Label(root, text=msg, bg="#0f1319", fg="#e8edf6", justify="left",
                 wraplength=480, font=("Microsoft YaHei UI", 9)).pack(padx=16, pady=14)
        ttk.Button(root, text="重新打开页面",
                   command=lambda: _launch_browser_fallback(url)).pack(pady=4)
        ttk.Button(root, text="停止并退出", command=root.destroy).pack(pady=4)
        root.mainloop()
    except Exception as e:
        _append_log(f"info window failed: {e}")


def _acquire_single_instance() -> bool:
    """防止重复启动争抢同一个 WebView2 / Edge profile（这是 WinError 5 的常见来源）。"""
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Global\\TsursportIOS_SingleInstance")
        if not handle:
            return True
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            ctypes.windll.user32.MessageBoxW(
                0,
                "Tsursport 已经在运行。\n\n请切换到已打开的窗口，或先在任务栏关闭它再启动。",
                "Tsursport", 0x40)
            return False
        globals()["_INSTANCE_MUTEX"] = handle   # 保持引用，进程退出前不释放
        return True
    except Exception:
        return True



def _kill_stale_webview2() -> int:
    """结束仍占用本项目 WebView2 缓存目录的残留进程（0x800700AA 的根因）。"""
    if sys.platform != "win32":
        return 0
    killed = 0
    try:
        import subprocess as _sp
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
            "Where-Object { $_.CommandLine -like '*Tsursport*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
        )
        r = _sp.run(["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, text=True, timeout=20)
        killed = len([x for x in (r.stdout or "").splitlines() if x.strip()])
    except Exception as e:
        _append_log(f"stale webview2 cleanup failed: {e}")
    if killed:
        _append_log(f"killed {killed} stale msedgewebview2 process(es)")
    return killed


def main() -> int:
    ap = argparse.ArgumentParser(description="Tsursport iOS 图形客户端")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--mock", action="store_true", help="启动虚拟 iPhone 演练模式（无需连接真机）")
    ap.add_argument("--selftest", action="store_true", help="启动并自检界面加载后自动退出")
    ap.add_argument("--selftest-out", default="", help="将自检结果 JSON 写入指定文件")
    args = ap.parse_args()

    if not args.selftest and not _acquire_single_instance():
        return 0

    srv, dev, runner, actual_port = core.create_app_server(port=args.port, host="0.0.0.0", mock_mode=args.mock)
    srv_thread = threading.Thread(target=srv.serve_forever, name="tsursport-http", daemon=True)
    srv_thread.start()

    url = f"http://127.0.0.1:{actual_port}/?desktop=1"
    runner.log(f"桌面服务已就绪：http://{core.lan_ip()}:{actual_port}")
    _append_log(f"start url={url} frozen={getattr(sys, 'frozen', False)}")

    try:
        import webview

        icon_path = _prepare_clean_dotnet_runtime()
        selftest_res = {"ok": False, "title": "", "ready": False}
        wv_state = {"loaded": False, "fallback_reason": ""}

        class _WvErrWatcher(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.ERROR:
                    msg = self.format(record)
                    _append_log(f"pywebview err: {msg}")
                    if not wv_state["loaded"] and not wv_state["fallback_reason"]:
                        wv_state["fallback_reason"] = msg
                        with suppress(Exception):
                            if webview.windows:
                                webview.windows[0].destroy()

        wv_logger = logging.getLogger("pywebview")
        wv_watcher = _WvErrWatcher()
        if not args.selftest:
            wv_logger.addHandler(wv_watcher)

        def _launch_wv(storage_dir: str, temp_cleanup: bool) -> None:
            webview.windows.clear()
            wv_state["loaded"] = False
            wv_state["fallback_reason"] = ""
            api = DesktopAPI(runner, actual_port)
            win = webview.create_window(
                "Tsursport · iOS 定位模拟",
                url=url,
                width=1200,
                height=800,
                min_size=(940, 620),
                background_color="#0f1319",
                js_api=api,
            )
            api._window = win

            if args.selftest:
                def _watchdog():
                    with suppress(Exception):
                        win.destroy()

                timer = threading.Timer(12.0, _watchdog)
                timer.daemon = True
                timer.start()

                def _on_loaded():
                    import time

                    def _check():
                        try:
                            for _ in range(30):
                                if win.evaluate_js("Boolean(window.TSURSPORT_READY)"):
                                    break
                                time.sleep(0.15)
                            selftest_res["title"] = str(win.evaluate_js("document.title") or "")
                            selftest_res["ready"] = bool(win.evaluate_js("Boolean(window.TSURSPORT_READY)"))
                            selftest_res["pt_count"] = int(win.evaluate_js("points.length") or 0)
                            win.evaluate_js("openWizard(0)")
                            for _ in range(20):
                                if win.evaluate_js("document.getElementById('wizardModal').classList.contains('show') && document.getElementById('wizBody').innerHTML.length > 50"):
                                    selftest_res["wiz_open"] = True
                                    break
                                time.sleep(0.15)
                            selftest_res["ok"] = bool(selftest_res["ready"] and selftest_res["pt_count"] >= 2 and selftest_res.get("wiz_open"))
                        except Exception as err:
                            selftest_res["err"] = str(err)
                        finally:
                            timer.cancel()
                            with suppress(Exception):
                                win.destroy()

                    threading.Thread(target=_check, daemon=True).start()

                win.events.loaded += _on_loaded
            else:
                def _blank_watchdog():
                    if not wv_state["loaded"]:
                        wv_state["fallback_reason"] = wv_state["fallback_reason"] or "WebView2 渲染超时（超过 3.5s 未完成加载）"
                        _append_log(f"watchdog trigger: {wv_state['fallback_reason']}")
                        with suppress(Exception):
                            win.destroy()

                blank_timer = threading.Timer(3.5, _blank_watchdog)
                blank_timer.daemon = True
                blank_timer.start()

                def _on_ui_loaded():
                    wv_state["loaded"] = True
                    blank_timer.cancel()
                    _append_log("webview loaded ok")

                win.events.loaded += _on_ui_loaded

            try:
                webview.start(
                    gui="edgechromium",
                    private_mode=False,
                    storage_path=storage_dir,
                    icon=icon_path,
                )
            finally:
                if not args.selftest:
                    with suppress(Exception):
                        blank_timer.cancel()
                if temp_cleanup:
                    with suppress(Exception):
                        shutil.rmtree(storage_dir, ignore_errors=True)

        try:
            if args.selftest:
                _launch_wv(tempfile.mkdtemp(prefix="tsursport_wv_selftest_"), temp_cleanup=True)
            else:
                _kill_stale_webview2()
                shared = os.path.join(_APP_DATA, "webview")
                own = os.path.join(_APP_DATA, "webview_%d" % os.getpid())
                last_err = None
                for storage, cleanup in ((shared, False), (own, False)):
                    try:
                        os.makedirs(storage, exist_ok=True)
                        _launch_wv(storage, temp_cleanup=cleanup)
                        last_err = None
                        break
                    except Exception as wv_err:
                        last_err = wv_err
                        _append_log(f"webview storage failed ({storage}): {wv_err}")
                        runner.log(f"缓存目录不可用，换下一个：{wv_err}")
                if last_err is not None:
                    runner.log(f"默认 WebView 缓存启动失败，改用临时目录重试：{last_err}")
                    _launch_wv(tempfile.mkdtemp(prefix="tsursport_wv_"), temp_cleanup=True)
        finally:
            if not args.selftest:
                wv_logger.removeHandler(wv_watcher)

        if args.selftest:
            if args.selftest_out:
                with suppress(Exception):
                    with open(args.selftest_out, "w", encoding="utf-8") as sf:
                        json.dump(selftest_res, sf, ensure_ascii=False, indent=2)
            print("SELFTEST:", selftest_res)
            return 0 if selftest_res.get("ok") else 1

        if wv_state["fallback_reason"] and not wv_state["loaded"]:
            runner.log(f"内嵌 WebView 未能就绪（{wv_state['fallback_reason']}），自动切换到 Edge 桌面应用窗口")
            try:
                if _launch_edge_app_and_wait(url):
                    return 0
            except Exception as edge_err:
                _append_log(f"edge fallback raised: {edge_err}")
            # 连 Edge 应用窗口都起不来：退到系统浏览器，不再弹“启动失败”
            _append_log("all window modes failed, fallback to system browser")
            _launch_browser_fallback(url)
            _show_info_window(url, actual_port)
            return 0
        return 0
    except Exception as e:
        if args.selftest:
            print("SELFTEST_ERR:", e)
            return 1
        tb = traceback.format_exc()
        _append_log(f"WebView fallback:\n{tb}")
        runner.log(f"WebView 启动失败，已切到系统浏览器模式：{e}")
        try:
            if _launch_edge_app_and_wait(url):
                return 0
        except Exception as edge_err:
            _append_log(f"edge fallback raised: {edge_err}")
        _launch_browser_fallback(url)
        try:
            import tkinter as tk
            from tkinter import ttk

            root = tk.Tk()
            root.title("Tsursport · iOS 服务运行中")
            root.geometry("500x210")
            ttk.Label(
                root,
                text=f"内嵌窗口加载失败（{type(e).__name__}: {e}）\n已在浏览器打开控制台：{url}\n手机遥控地址：http://{core.lan_ip()}:{actual_port}",
                padding=16,
                wraplength=460,
            ).pack()
            ttk.Button(root, text="重新打开页面", command=lambda: _launch_browser_fallback(url)).pack(pady=4)
            ttk.Button(root, text="恢复定位并退出", command=root.destroy).pack(pady=6)
            root.mainloop()
        except Exception:
            if sys.stdin is not None and not getattr(sys.stdin, "closed", True):
                with suppress(EOFError, KeyboardInterrupt):
                    input("服务运行中，按 Enter 键恢复真实定位并退出…")
            else:
                _show_error_box(
                    "Tsursport · iOS 浏览器模式",
                    f"内嵌窗口加载失败，已在浏览器打开：\n{url}\n\n关闭本弹窗将停止后台定位服务。\n详细日志：{_LOG_FILE}",
                )
        return 0
    finally:
        with suppress(Exception):
            runner.clear(disconnect=True)
        with suppress(Exception):
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:
        traceback.print_exc()
        if "--selftest" not in sys.argv:
            _show_error_box("Tsursport · iOS 启动失败", f"{exc}\n\n详情请看日志：{_LOG_FILE}")
        sys.exit(1)
