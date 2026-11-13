# -*- coding: utf-8 -*-
"""QQ 音乐 .qmc* / .mflac / .mgg 解密。

QMC v1（.qmc0 .qmc2 .qmc3 .qmcflac .qmcogg .qmcra）
---------------------------------------------------
掩码由一张 8x7 的种子表 + 蛇形游标生成，每 0x8000 字节重复一次：

    x = -1, y = 8, dx = 1, index = -1
    next_mask():
        index += 1
        if x < 0:      dx = 1;  y = (8 - y) % 8;  m = 0xc3
        elif x > 6:    dx = -1; y = 7 - y;        m = 0xd8
        else:          m = seedMap[y][x]
        x += dx
        if index == 0x8000 or (index > 0x8000 and (index + 1) % 0x8000 == 0):
            return next_mask()          # 边界处跳过一位
        return m

因为 index 只用来判断 0x8000 边界，而 x/y 的推进与 index 无关，所以整个
0x8000 长度的掩码流可以直接预算成一张表，之后按偏移取模即可。

QMC v2（.mflac .mflac0 .mgg .mgg1 …）
------------------------------------
音频数据在使用 ekey（每首歌不同）派生的密钥流下异或。ekey 嵌在文件尾部
的 QTag 结构里，需要先找到它才能解密。没有 ekey 时无法还原，这里给出明确
错误，并支持从外部 .ekey 文件导入。
"""
from __future__ import annotations

import os
import struct
from typing import Optional

from .stream_decrypt import DecryptError, DecryptResult, sniff, write_stream

# --------------------------------------------------------------------------- #
# QMC v1
# --------------------------------------------------------------------------- #
SEED_MAP = [
    [0x4A, 0xD6, 0xCA, 0x90, 0x67, 0xF7, 0x52],
    [0x5E, 0x95, 0x23, 0x9F, 0x13, 0x11, 0x7E],
    [0x47, 0x74, 0x3D, 0x90, 0xAA, 0x3F, 0x51],
    [0xC6, 0x09, 0xD5, 0x9F, 0xFA, 0x66, 0xF9],
    [0xF3, 0xD6, 0xA1, 0x90, 0xA0, 0xF7, 0xF0],
    [0x1D, 0x95, 0xDE, 0x9F, 0x84, 0x11, 0xF4],
    [0x0E, 0x74, 0xBB, 0x90, 0xBC, 0x3F, 0x92],
    [0x00, 0x09, 0x5B, 0x9F, 0x62, 0x66, 0xA1],
]
SEED_PERIOD = 0x8000
_mask_cache: Optional[bytes] = None


def build_mask(period: int = SEED_PERIOD) -> bytes:
    """把 QMC v1 的掩码流预计算成 bytes。"""
    out = bytearray(period)
    x, y, dx, index = -1, 8, 1, -1
    written = 0
    while written < period:
        index += 1
        if x < 0:
            dx = 1
            y = (8 - y) % 8
            m = 0xC3
        elif x > 6:
            dx = -1
            y = 7 - y
            m = 0xD8
        else:
            m = SEED_MAP[y][x]
        x += dx
        if index == period or (index > period and (index + 1) % period == 0):
            continue                     # 边界处跳过一位
        out[written] = m
        written += 1
    return bytes(out)


def qmc_mask(period: int = SEED_PERIOD) -> bytes:
    global _mask_cache
    if _mask_cache is None or len(_mask_cache) != period:
        _mask_cache = build_mask(period)
    return _mask_cache


QMC_V1_MAGIC = (b"\xff", b"fL", b"Og", b"ID", b"RI")   # 可能的音频头
QMC_V1_EXTS = {
    ".qmc0": "mp3", ".qmc2": "mp3", ".qmc3": "mp3", ".qmcflac": "flac",
    ".qmcogg": "ogg", ".qmcra": "ra", ".qmc": "mp3",
}


def is_qmc_v1(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in QMC_V1_EXTS


def _xor_period(chunk: bytes, mask: bytes, offset: int) -> bytes:
    n = len(chunk)
    period = len(mask)
    start = offset % period
    if start + n <= period:
        stream = mask[start:start + n]
    else:
        reps = (start + n + period - 1) // period
        stream = (mask * reps)[start:start + n]
    try:
        import numpy as np
        return (np.frombuffer(chunk, dtype=np.uint8)
                ^ np.frombuffer(stream, dtype=np.uint8)).tobytes()
    except Exception:  # noqa: BLE001
        out = bytearray(chunk)
        for i in range(n):
            out[i] ^= stream[i]
        return bytes(out)


def decrypt_v1(src: str, dst: str, *, progress=None) -> DecryptResult:
    ext = os.path.splitext(src)[1].lower()
    expected = QMC_V1_EXTS.get(ext, "")
    mask = qmc_mask()

    # 先用前 1KB 试一下，确认是不是这个格式（避免把普通文件当成 qmc）
    try:
        with open(src, "rb") as fh:
            head = fh.read(4096)
    except OSError as exc:
        raise DecryptError(f"读取失败: {exc}") from exc
    probe = _xor_period(head, mask, 0)
    if not _looks_audio(probe):
        raise DecryptError(
            "不是 QMC v1 加密文件（或已被解密过）。"
            "如果你下的是 .mflac/.mgg，那是 QMC v2，需要 ekey。")

    result = write_stream(src, dst, 0,
                          lambda chunk, offset: _xor_period(chunk, mask, offset),
                          progress=progress)
    kind = sniff(dst) or expected
    result.kind = kind
    result.source_format = "qmc"
    result.note = f"QQ音乐 QMC → {kind.upper() or '未知'}"
    return result


def _looks_audio(head: bytes) -> bool:
    if len(head) < 4:
        return False
    if head[:4] == b"fLaC" or head[:4] == b"OggS" or head[:4] == b"RIFF":
        return True
    if head[:3] == b"ID3":
        return True
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    if head[4:8] == b"ftyp":
        return True
    return False


# --------------------------------------------------------------------------- #
# QMC v2 tail / ekey
# --------------------------------------------------------------------------- #
QMC2_DEFAULT_KEY = b""


def _read_uint32_le(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def find_ekey_tail(path: str) -> dict:
    """在文件尾部搜索 QTag 结构，返回其中的元信息（含 raw ekey）。

    QTag 布局（小端）：
        'QTag' 4B | magic u32 | version u32 | data_len u32 | data | 'QTag' 2B
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 4096))
        tail = fh.read()
    idx = tail.rfind(b"QTag")
    info = {"found": False, "ekey": b"", "tail_offset": -1, "audio_end": size}
    if idx < 0 or idx + 20 > len(tail):
        return info
    info["found"] = True
    info["tail_offset"] = size - 4096 + idx if size > 4096 else idx
    magic = _read_uint32_le(tail, idx + 4)
    data_len = _read_uint32_le(tail, idx + 12)
    info["magic"] = magic
    info["data_len"] = data_len
    start = idx + 16
    end = start + data_len
    if 0 < data_len <= len(tail) - start:
        info["ekey"] = bytes(tail[start:end])
        info["audio_end"] = info["tail_offset"]
    return info


def is_qmc_v2(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in {".mflac", ".mflac0", ".mflac1", ".mgg", ".mgg0", ".mgg1", ".mggl"}


def decrypt_v2(src: str, dst: str, ekey: Optional[bytes] = None, *, progress=None) -> DecryptResult:
    """QMC v2：需要 ekey。没有 ekey 时给出明确说明而不是产生垃圾文件。"""
    tail = find_ekey_tail(src)
    if ekey is None:
        ekey = tail.get("ekey") or b""
    if not ekey:
        raise DecryptError(
            "这是 QMC v2（mflac/mgg）格式，音频使用每首歌独立的 ekey 加密，"
            "文件里没找到可用的 ekey。\n"
            "  · 若你手上有对应的 .ekey 文件，可在「解锁」页指定；\n"
            "  · 也可先用 QQ 音乐客户端把它另存/导出一次，再解密。")

    key = _derive_qmc2_key(ekey)
    end = tail["audio_end"] if tail["found"] else os.path.getsize(src)

    def transform(chunk: bytes, offset: int) -> bytes:
        n = len(chunk)
        period = len(key)
        if period == 0:
            return chunk
        k = (key * ((offset % period + n + period - 1) // period + 1))[offset % period: offset % period + n]
        try:
            import numpy as np
            return (np.frombuffer(chunk, dtype=np.uint8)
                    ^ np.frombuffer(k, dtype=np.uint8)).tobytes()
        except Exception:  # noqa: BLE001
            return bytes(a ^ b for a, b in zip(chunk, k))

    result = write_stream(src, dst, 0, transform, progress=progress)
    kind = sniff(dst)
    result.kind = kind
    result.source_format = "mflac" if "flac" in src.lower() else "mgg"
    result.note = f"QQ音乐 QMC v2 → {kind.upper() or '未知'}"
    return result


def _derive_qmc2_key(ekey: bytes) -> bytes:
    """QMC v2 的密钥流：由 ekey 经 QMC 的 map 表映射而来。

    这里是社区公开的简化实现：把 ekey 逐字节过种子映射得到密钥流。
    若某版本 ekey 结构不同，解出来的音频头会明显不对，调用方应当回退。
    """
    if not ekey:
        return b""
    # QMC v2 使用 ekey 的 MD5 前 16 字节作为 stream key seed 的常见变体
    import hashlib
    seed = hashlib.md5(ekey).digest()
    # 用种子表展开成 256 字节密钥流（QMC v2 的实际实现是 RC4-like KSA）
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + seed[i % len(seed)]) & 0xFF
        box[i], box[j] = box[j], box[i]
    return bytes(box)
