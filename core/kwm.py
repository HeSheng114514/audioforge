# -*- coding: utf-8 -*-
"""酷我音乐 .kwm 解密。

KW(M) 文件结构：前 1024 字节是一个 32 字节密钥的重复 XOR 块，
音频数据从 1024 字节之后开始。

    密钥推导
    --------
    从 0x400 开始读 32 字节，与上一次读到的 32 字节比较；若连续两次相同，
    说明这一段就是密钥（文件里密钥会连续重复）。找不到重复时，把探测到的
    32 字节前后 16 字节对调作为密钥（某些版本如此）。

    解密
    ----
    out[i] = enc[1024 + i] ^ key[i % 32]
"""
from __future__ import annotations

import os
from typing import Optional

from .stream_decrypt import DecryptError, DecryptResult, sniff, write_stream

HEADER_SIZE = 1024
KEY_LEN = 32
MAX_SCAN = 468


def is_kwm(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in {".kwm", ".kwm2", ".kwmflac"}


def _find_key(src: str) -> bytes:
    size = os.path.getsize(src)
    if size <= HEADER_SIZE + KEY_LEN:
        raise DecryptError("文件太小，不像酷我加密文件")
    with open(src, "rb") as fh:
        fh.seek(HEADER_SIZE)
        prev = None
        found = None
        for _ in range(MAX_SCAN):
            block = fh.read(KEY_LEN)
            if len(block) < KEY_LEN:
                break
            if prev is not None and block == prev:
                found = block
                break
            prev = block
        if found is None:
            if prev is None:
                raise DecryptError("无法在文件头部定位密钥")
            # 某些版本把密钥前后 16 字节对调
            found = prev[16:] + prev[:16]
    return bytes(found)


def decrypt(src: str, dst: str, *, progress=None) -> DecryptResult:
    if not os.path.isfile(src):
        raise DecryptError(f"文件不存在: {src}")
    key = _find_key(src)

    def transform(chunk: bytes, offset: int) -> bytes:
        n = len(chunk)
        reps = (offset % KEY_LEN + n + KEY_LEN - 1) // KEY_LEN + 1
        stream = (key * reps)[offset % KEY_LEN: offset % KEY_LEN + n]
        try:
            import numpy as np
            return (np.frombuffer(chunk, dtype=np.uint8)
                    ^ np.frombuffer(stream, dtype=np.uint8)).tobytes()
        except Exception:  # noqa: BLE001
            return bytes(a ^ b for a, b in zip(chunk, stream))

    result = write_stream(src, dst, HEADER_SIZE, transform, progress=progress)
    kind = sniff(dst)
    if not kind:
        raise DecryptError("解密后不是可识别的音频，密钥可能不匹配")
    result.kind = kind
    result.source_format = "kwm"
    result.note = f"酷我 KWM → {kind.upper()}"
    return result
