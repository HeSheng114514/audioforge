# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件桌面版 AudioForge。

    python -m PyInstaller AudioForge.spec --noconfirm

产物：dist/AudioForge.exe（约 60–90 MB，含 Python 运行时 + ffmpeg 动态库）
"""
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

BASE = os.path.abspath(SPECPATH)

# av (PyAV) 自带 ffmpeg 动态库，必须整包带上，否则运行时报找不到 libavcodec
av_binaries = collect_dynamic_libs("av")
av_datas = collect_data_files("av")

# 前端资源
web_datas = [
    (os.path.join(BASE, "web", "index.html"), "web"),
    (os.path.join(BASE, "web", "style.css"), "web"),
    (os.path.join(BASE, "web", "app.js"), "web"),
]

# 内置演示音频（可选，缺失不影响运行）
sample_dir = os.path.join(BASE, "samples")
sample_datas = []
if os.path.isdir(sample_dir):
    for name in os.listdir(sample_dir):
        path = os.path.join(sample_dir, name)
        if os.path.isfile(path):
            sample_datas.append((path, "samples"))

hiddenimports = [
    "av", "av.audio", "av.audio.codeccontext", "av.audio.resampler",
    "av.audio.frame", "av.audio.stream", "av.codec", "av.container",
    "av.format", "av.packet", "av.error",
    "Crypto", "Crypto.Cipher", "Crypto.Cipher.AES", "Crypto.Util",
    "mutagen", "mutagen.id3", "mutagen.flac", "mutagen.mp4",
    "mutagen.oggvorbis", "mutagen.asf", "mutagen.easyid3",
    "numpy", "flask", "werkzeug", "jinja2", "click", "itsdangerous",
    "markupsafe", "blinker",
]

a = Analysis(
    ["launcher.py"],
    pathex=[BASE],
    binaries=av_binaries,
    datas=av_datas + web_datas + sample_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "scipy", "pandas", "IPython", "jupyter",
        "torch", "onnxruntime", "cv2", "PIL", "faster_whisper",
        "openai", "whisper", "librosa", "pytest", "setuptools",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AudioForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    version=None,
)
