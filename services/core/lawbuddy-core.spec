# -*- mode: python ; coding: utf-8 -*-
"""lawbuddy-core 打包 spec（Windows onedir）。

目标：产出免装 Python 的独立 Core 目录，由 electron-builder 以
extraResources 复制进安装包 `resources/core`，Electron Main 直接
spawn `lawbuddy-core.exe`（见 apps/desktop/src/main/coreProcess.ts）。

约定：
- console=True 必须保留：JSON-RPC 走 stdout，窗口化会丢 stdio；
- 排除 paddle 系（约 500MB+）：OCR 引擎链 rapidocr(ONNX) 优先且模型随包，
  Paddle 仅是末端回退，冻结产物不含它不影响主路径（engine.py 探测缺失自动跳过）；
- 排除 modelscope：表格还原模型以 LAWBUDDY_TABLE_MODEL_PATH 指向随包
  models/slanet-plus.onnx（见 lawbuddy/__main__.py），无需下载库；
- langgraph/langchain_core collect_submodules：存在运行时动态导入。
"""

import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

datas = []
binaries = []
hiddenimports = []

# 模型与数据：rapidocr 内置检测/识别模型、onnxruntime DLL、pdfium、sqlcipher 等
for pkg in (
    "rapidocr_onnxruntime",
    "rapid_table",
    "onnxruntime",
    "pypdfium2",
    "sqlcipher3",
    "reportlab",
):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

for pkg in ("langgraph", "langchain_core"):
    hiddenimports += collect_submodules(pkg)

# 表格还原模型（离线）：随包分发；模型路径由 __main__ 设
# LAWBUDDY_TABLE_MODEL_PATH
datas.append(
    (os.path.join(SPECPATH, "models", "slanet-plus.onnx"), os.path.join("models"))
)

a = Analysis(
    [os.path.join(SPECPATH, "frozen_entry.py")],
    pathex=[SPECPATH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "paddleocr",
        "paddle",
        "paddlex",
        "modelscope",
        "tkinter",
        "matplotlib",
        "IPython",
        "pytest",
        "evals",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="lawbuddy-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="lawbuddy-core",
)
