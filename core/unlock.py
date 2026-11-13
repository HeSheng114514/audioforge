# -*- coding: utf-8 -*-
"""私有加密格式统一入口。

按扩展名 + 文件头嗅探分流到具体解密器，对外只暴露 detect / unlock 两个函数。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import kgm, kwm, ncm, qmc
from .stream_decrypt import DecryptError, DecryptResult, sniff

# 扩展名 → (显示名, 平台)
KINDS: Dict[str, tuple] = {
    ".ncm": ("NCM", "网易云音乐"),
    ".qmc0": ("QMC", "QQ音乐"),
    ".qmc2": ("QMC", "QQ音乐"),
    ".qmc3": ("QMC", "QQ音乐"),
    ".qmcflac": ("QMC", "QQ音乐"),
    ".qmcogg": ("QMC", "QQ音乐"),
    ".qmcra": ("QMC", "QQ音乐"),
    ".qmc": ("QMC", "QQ音乐"),
    ".mflac": ("QMC v2", "QQ音乐"),
    ".mflac0": ("QMC v2", "QQ音乐"),
    ".mflac1": ("QMC v2", "QQ音乐"),
    ".mgg": ("QMC v2", "QQ音乐"),
    ".mgg0": ("QMC v2", "QQ音乐"),
    ".mgg1": ("QMC v2", "QQ音乐"),
    ".kgm": ("KGM", "酷狗音乐"),
    ".kgma": ("KGM", "酷狗音乐"),
    ".vpr": ("VPR", "酷狗音乐"),
    ".kgg": ("KGG", "酷狗音乐"),
    ".kwm": ("KWM", "酷我音乐"),
    ".kwm2": ("KWM", "酷我音乐"),
}

SUPPORTED_EXTS = tuple(KINDS)


@dataclass
class Detection:
    encrypted: bool
    platform: str = ""
    kind: str = ""
    note: str = ""
    needs_key: bool = False


def detect(path: str) -> Detection:
    """判断是不是受支持的私有加密格式。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in KINDS:
        label, platform = KINDS[ext]
        needs_key = ext in {".mflac", ".mgg", ".mflac0", ".mgg0", ".kgg"}
        return Detection(True, platform, label,
                         f"{platform} {label} 加密文件", needs_key=needs_key)
    # 扩展名不认识时看文件头
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return Detection(False)
    if head == b"CTENFDAM":
        return Detection(True, "网易云音乐", "NCM", "网易云 NCM（按文件头识别）")
    return Detection(False)


def is_supported(path: str) -> bool:
    return detect(path).encrypted


def unlock(src: str, dst: str, *, ekey: Optional[bytes] = None,
           progress: Optional[Callable[[float, str], None]] = None) -> DecryptResult:
    """解密任意受支持的私有格式，输出原始音频（不重编码）。"""
    if not os.path.isfile(src):
        raise DecryptError(f"文件不存在: {src}")
    det = detect(src)
    if not det.encrypted:
        raise DecryptError("不是受支持的加密格式")

    ext = os.path.splitext(src)[1].lower()
    if ext == ".ncm" or ncm.is_ncm(src):
        return ncm.decrypt(src, dst, progress=progress)
    if qmc.is_qmc_v1(src):
        return qmc.decrypt_v1(src, dst, progress=progress)
    if qmc.is_qmc_v2(src):
        return qmc.decrypt_v2(src, dst, ekey=ekey, progress=progress)
    if kgm.is_kgm(src):
        return kgm.decrypt(src, dst, progress=progress)
    if kwm.is_kwm(src):
        return kwm.decrypt(src, dst, progress=progress)
    raise DecryptError(f"暂不支持 {det.platform} {det.kind} 的密钥获取，无法解密")


def output_name(src: str, kind: str) -> str:
    """把 xxx.ncm 换成 xxx.flac 这样的目标文件名。"""
    stem = os.path.splitext(os.path.basename(src))[0]
    ext = kind or "audio"
    return f"{stem}.{ext}"
