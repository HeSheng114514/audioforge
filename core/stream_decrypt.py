# -*- coding: utf-8 -*-
"""流式解密公共设施：分块转换 + 落盘，避免整文件读进内存。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

CHUNK = 1 << 20  # 1 MiB


class DecryptError(RuntimeError):
    pass


@dataclass
class DecryptResult:
    path: str
    source: str
    source_format: str = ""
    kind: str = ""                     # 解密后的真实格式：flac / mp3 / ...
    size: int = 0
    meta: Dict[str, str] = field(default_factory=dict)
    title: str = ""
    note: str = ""
    cover: Optional[bytes] = None


def write_stream(src: str, dst: str,
                 offset: int,
                 transform: Callable[[bytes, int], bytes],
                 *, progress: Optional[Callable[[float, str], None]] = None,
                 chunk: int = CHUNK) -> DecryptResult:
    """从 src 的 offset 开始逐块 transform 后写入 dst。

    offset 同时作为异或密钥流的起点（很多私有格式要求按全局偏移取 key）。
    """
    total = max(1, os.path.getsize(src) - offset)
    done = 0
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    tmp = dst + ".part"
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            fi.seek(offset)
            while True:
                block = fi.read(chunk)
                if not block:
                    break
                fo.write(transform(block, done))
                done += len(block)
                if progress and total:
                    progress(min(0.99, done / total), "解密中")
        os.replace(tmp, dst)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    size = os.path.getsize(dst)
    if size == 0:
        raise DecryptError("解密结果为空")
    return DecryptResult(path=dst, source=src, size=size)


def copy_tail(src: str, dst: str, offset: int, *,
              progress: Optional[Callable[[float, str], None]] = None) -> DecryptResult:
    """不做变换、只截掉头部（用于极少数"仅剥壳"的格式）。"""
    return write_stream(src, dst, offset, lambda b, o: b, progress=progress)


def sniff(path: str) -> str:
    """识别音频容器，返回扩展名（不含点）。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return ""
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3":
        return "mp3"
    if head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3" or head[:2] == b"\xff\xf2":
        return "mp3"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"\x30\x26\xb2\x75":
        return "wma"
    if head[:4] == b"MAC ":
        return "ape"
    if head[:4] == b"wvpk":
        return "wv"
    return ""
