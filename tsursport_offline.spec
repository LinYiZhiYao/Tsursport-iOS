# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— 自包含离线版（不依赖本机 Python）。

只做最小化钩子，避免 collect_submodules('webview') 卡死。
输出：dist/Tsursport-iOS/（整个目录即离线可运行）
"""

from PyInstaller.utils.hooks import collect_data_files

hiddenimports = [
    "pytun_pmd3",
    "pytun_pmd3.wintun",
    "webview",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr_loader",
    "pythonnet",
    "clr",
    # iOS 设备通信：这些是函数内 import，静态分析看不到，必须显式列出
    "pymobiledevice3",
    "pymobiledevice3.usbmux",
    "pymobiledevice3.lockdown",
    "pymobiledevice3.exceptions",
    "pymobiledevice3.doctor",
    "pymobiledevice3.remote.userspace_tunnel",
    "pymobiledevice3.remote.remote_service_discovery",
    "pymobiledevice3.services.amfi",
    "pymobiledevice3.services.mobile_image_mounter",
    "pymobiledevice3.services.simulate_location",
    "pymobiledevice3.services.dvt.instruments.dvt_provider",
    "pymobiledevice3.services.dvt.instruments.location_simulation",
    "pymobiledevice3.tunneld.api",
]

datas = [
    ("web", "web"),
    ("tsursport_pc.py", "."),
    ("tsursport.ico", "."),
]
datas += collect_data_files("webview")
datas += collect_data_files("pymobiledevice3")
datas += collect_data_files("pytun_pmd3", include_py_files=False)

a = Analysis(
    ["tsursport_gui.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test", "test", "unittest", "PyQt5", "PyQt6", "PySide2", "PySide6", "gtk",
              "torch", "torchvision", "torchaudio", "llvmlite", "numba", "scipy", "matplotlib",
              "pandas", "PIL", "jedi", "IPython", "notebook", "sklearn", "sympy", "cv2",
              "pytest", "sphinx", "docutils", "babel", "pyarrow", "sqlalchemy"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Tsursport-iOS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["tsursport.ico"],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Tsursport-iOS",
)
