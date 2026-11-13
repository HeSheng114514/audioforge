# -*- coding: utf-8 -*-
"""网易云音乐 .ncm 解密。

NCM 结构
--------
    "CTENFDAM"            8B   魔数
    0x01 0x70             2B   版本与固定填充
    key_len (LE u32)      4B
    key_blob              key_len 字节
    meta_len (LE u32)     4B
    meta_blob             meta_len 字节
    crc32                 4B
    0x01 + 5B 填充
    img_len (LE u32)      4B
    cover                 img_len 字节
    audio payload         ...

密钥推导
--------
    key_blob ^ 0x64  →  AES-128-ECB 解密（key = "hzHRAmso5kInbaxW"）
    PKCS#7 去填充 → 去掉前缀 "neteasecloudmusic" → 得到 RC4 密钥
    用该密钥做 KSA 得到 256 字节 S 盒，音频按
        out[i] = in[i] ^ S[(S[i+1] + S[(S[i+1] + (i+1)) & 0xff]) & 0xff]
    逐字节异或还原
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
from dataclasses import dataclass, field
from typing import Dict, Optional

from .stream_decrypt import DecryptError, DecryptResult, write_stream

MAGIC = b"CTENFDAM"
CORE_KEY = bytes([0x68, 0x7A, 0x48, 0x52, 0x41, 0x6D, 0x73, 0x6F,
                  0x35, 0x6B, 0x49, 0x6E, 0x62, 0x61, 0x78, 0x57])
META_KEY = bytes([0x23, 0x31, 0x34, 0x6C, 0x6A, 0x6B, 0x5F, 0x21,
                  0x5C, 0x5D, 0x26, 0x30, 0x55, 0x3C, 0x27, 0x28])
KEY_PREFIX = b"neteasecloudmusic"
META_PREFIX = b"163 key(Don't modify):"


def _aes_ecb_decrypt(blob: bytes, key: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:  # noqa: BLE001
        raise DecryptError("缺少 pycryptodome，无法解密 NCM") from exc
    if len(blob) % 16:
        raise DecryptError("NCM 密钥块长度不是 16 的倍数，文件可能损坏")
    return AES.new(key, AES.MODE_ECB).decrypt(blob)


def _unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    return data[:-pad] if 0 < pad <= 16 else data


def _derive_rc4_key(key_blob: bytes) -> bytes:
    raw = _unpad(_aes_ecb_decrypt(bytes(b ^ 0x64 for b in key_blob), CORE_KEY))
    if raw.startswith(KEY_PREFIX):
        raw = raw[len(KEY_PREFIX):]
    if not raw:
        raise DecryptError("RC4 密钥为空")
    return raw


def _ksa(key: bytes) -> bytes:
    box = bytearray(range(256))
    j = 0
    n = len(key)
    for i in range(256):
        j = (j + box[i] + key[i % n]) & 0xFF
        box[i], box[j] = box[j], box[i]
    return bytes(box)


def _xor_table(sbox: bytes) -> bytes:
    """NCM 的密钥流以 256 字节为周期（索引全部 & 0xff），先算成查表。"""
    table = bytearray(256)
    for i in range(256):
        j = (i + 1) & 0xFF
        table[i] = sbox[(sbox[j] + sbox[(sbox[j] + j) & 0xFF]) & 0xFF]
    return bytes(table)


def xor_with_table(chunk: bytes, table: bytes, offset: int) -> bytes:
    """按全局偏移套用 256 字节周期密钥表。优先走 numpy，纯 Python 兜底。"""
    n = len(chunk)
    if n == 0:
        return b""
    start = offset & 0xFF
    need = start + n
    reps = (need + 255) // 256
    stream = (table * reps)[start:start + n]
    try:
        import numpy as np
        data = np.frombuffer(chunk, dtype=np.uint8)
        key = np.frombuffer(stream, dtype=np.uint8)
        return (data ^ key).tobytes()
    except Exception:  # noqa: BLE001
        out = bytearray(chunk)
        for i in range(n):
            out[i] ^= stream[i]
        return bytes(out)


@dataclass
class NcmMeta:
    format: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0
    bitrate: int = 0
    raw: Dict = field(default_factory=dict)

    def tags(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if self.title:
            out["title"] = self.title
        if self.artist:
            out["artist"] = self.artist
        if self.album:
            out["album"] = self.album
        return out


def parse_meta(meta_blob: bytes) -> NcmMeta:
    meta = NcmMeta()
    if not meta_blob:
        return meta
    blob = bytes(b ^ 0x63 for b in meta_blob)
    if blob.startswith(META_PREFIX):
        blob = blob[len(META_PREFIX):]
    try:
        dec = _unpad(_aes_ecb_decrypt(base64.b64decode(blob), META_KEY))
        if dec.startswith(b"music:"):
            dec = dec[6:]
        info = json.loads(dec.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return meta
    meta.raw = info
    meta.format = str(info.get("format") or "").lower()
    meta.title = str(info.get("musicName") or "")
    artists = info.get("artist") or []
    names = []
    for a in artists:
        if isinstance(a, (list, tuple)) and a:
            names.append(str(a[0]))
        elif isinstance(a, str):
            names.append(a)
    meta.artist = " / ".join(names)
    meta.album = str(info.get("album") or "")
    try:
        meta.duration = float(info.get("duration") or 0) / 1000.0
        meta.bitrate = int(info.get("bitrate") or 0)
    except (TypeError, ValueError):
        pass
    return meta


def is_ncm(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == MAGIC
    except OSError:
        return False


def decrypt(src: str, dst: str, *, progress=None) -> DecryptResult:
    """解密 NCM，音频流原样落盘（真无损，不重编码）。"""
    if not os.path.isfile(src):
        raise DecryptError(f"文件不存在: {src}")

    with open(src, "rb") as fh:
        if fh.read(8) != MAGIC:
            raise DecryptError("不是 NCM 文件（魔数不匹配）")
        fh.seek(8)
        fh.read(2)
        key_len = struct.unpack("<I", fh.read(4))[0]
        if key_len <= 0 or key_len > 1 << 20:
            raise DecryptError("NCM 密钥段长度异常")
        rc4_key = _derive_rc4_key(fh.read(key_len))

        meta_len_raw = fh.read(4)
        if len(meta_len_raw) < 4:
            raise DecryptError("NCM 头部不完整")
        meta_len = struct.unpack("<I", meta_len_raw)[0]
        meta = parse_meta(fh.read(meta_len) if 0 < meta_len < (1 << 24) else b"")

        fh.read(4)      # crc32
        fh.read(5)      # 版本填充：0x01 + 4B
        img_raw = fh.read(4)
        if len(img_raw) < 4:
            raise DecryptError("NCM 封面段不完整")
        img_len = struct.unpack("<I", img_raw)[0]
        if img_len:
            fh.seek(img_len, 1)
        audio_start = fh.tell()

    table = _xor_table(_ksa(rc4_key))

    def transform(chunk: bytes, offset: int) -> bytes:
        return xor_with_table(chunk, table, offset)

    result = write_stream(src, dst, audio_start, transform, progress=progress)

    fmt = meta.format
    if not fmt:
        fmt = guess_audio_ext(result.path)
    result.meta = meta.tags()
    result.kind = fmt
    result.source_format = "ncm"
    result.title = meta.title
    result.note = f"网易云 NCM → {fmt.upper()}" if fmt else "网易云 NCM"
    if meta.bitrate:
        result.note += f" · {meta.bitrate // 1000} kbps"
    return result


def guess_audio_ext(path: str) -> str:
    """从解密后文件头判断真实格式。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return ""
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3":
        return "mp3"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[4:8] == b"ftyp":
        return "m4a"
    return ""
