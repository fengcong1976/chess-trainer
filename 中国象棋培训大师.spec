# -*- mode: python ; coding: utf-8 -*-
# 中国象棋培训大师 — onedir 打包配置
# 打包命令（在项目根目录执行）：
#   打包命令（在项目根目录执行，虚拟环境见 venv\Scripts）：
#   venv\Scripts\python.exe -X utf8 -m PyInstaller --noconfirm --clean 中国象棋培训大师.spec

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app_icon.ico', '.'),
        ('app_icon.png', '.'),
        ('鼠标图片.png', '.'),
        ('engine_bin\\pikafish.exe', 'engine_bin'),
        ('engine_bin\\pikafish.nnue', 'engine_bin'),
        ('sounds', 'sounds'),
    ],
    hiddenimports=['src.resources'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=True,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='中国象棋培训大师',
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
    icon='app_icon.ico',
    version='version_info.txt',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='中国象棋培训大师',
)
