# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


project_root = Path(SPECPATH).resolve().parent
web_root = project_root / "src" / "wechat_bot_bridge" / "web"
datas = [
    (str(web_root / "templates"), "wechat_bot_bridge/web/templates"),
    (str(web_root / "static"), "wechat_bot_bridge/web/static"),
    *copy_metadata("wechat-bot-bridge"),
]

# Uvicorn 根据运行平台动态选择 HTTP、WebSocket、事件循环和 lifespan 实现。
hidden_imports = collect_submodules("uvicorn")

a = Analysis(
    [str(project_root / "packaging" / "windows_entry.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="wechat-bot-bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project_root / "packaging" / "bridge.ico"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
