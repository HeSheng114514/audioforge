# -*- coding: utf-8 -*-
"""为私有格式解密器生成"加密夹具"：用已确认的加密算法反向打包，
再用解密器还原，比对是否与原音频逐字节相同。

这比"能解出文件头"强得多 —— 它验证的是整条链路的可逆性。
"""
from __future__ import annotations

import io
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from core import kgm, kwm, qmc
from core.audio_io import AudioData, encode


def make_audio(seconds=1.5, rate=44100, freq=440.0) -> bytes:
    import tempfile
    n = int(rate * seconds)
    t = np.arange(n) / rate
    sig = (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    audio = AudioData(np.stack([sig, sig]), rate)
    fd, path = tempfile.mkstemp(suffix=".flac")
    os.close(fd)
    encode(audio, path, "flac")
    blob = open(path, "rb").read()
    os.remove(path)
    return blob


# --------------------------------------------------------------------------- #
# QMC v1：反向打包
# --------------------------------------------------------------------------- #
def pack_qmc_v1(raw: bytes) -> bytes:
    mask = qmc.qmc_mask()
    period = len(mask)
    out = bytearray(raw)
    for i in range(len(out)):
        out[i] ^= mask[i % period]
    return bytes(out)


# --------------------------------------------------------------------------- #
# KGM：反向打包
# --------------------------------------------------------------------------- #
def _encrypt_byte(plain: int, filekey: bytes, offset: int, vpr: bool) -> int:
    # 正向是 out = T(mask ^ enc ^ key)，T 是自逆的，所以
    # enc = T(out) ^ mask ^ key
    x = plain ^ ((plain & 0x0F) << 4)          # T^{-1} == T
    m = kgm._mask_v2(offset) ^ filekey[offset % 17]
    if vpr:
        m ^= kgm.VPR_KEY[offset % 17]
    return x ^ m


def pack_kgm(raw: bytes, filekey: bytes = b"0123456789abcdef", vpr: bool = False) -> bytes:
    header = bytearray(0x3C)
    header[0:8] = kgm.MAGIC_KGM
    header[0x10:0x14] = struct.pack("<I", 0x3C)
    header[0x1C:0x2C] = filekey[:16]
    body = bytearray(len(raw))
    for i, b in enumerate(raw):
        body[i] = _encrypt_byte(b, filekey + b"\x00", i, vpr)
    return bytes(header) + bytes(body)


# --------------------------------------------------------------------------- #
# KWM：反向打包
# --------------------------------------------------------------------------- #
def pack_kwm(raw: bytes, key: bytes = bytes(range(32))) -> bytes:
    header = bytearray(1024)
    # 让 0x400 之后连续出现两个相同的 32 字节块，解密器据此判定密钥
    header[0x40:0x60] = key
    header[0x60:0x80] = key
    body = bytearray(len(raw))
    for i, b in enumerate(raw):
        body[i] = b ^ key[i % 32]
    return bytes(header) + bytes(body)


# --------------------------------------------------------------------------- #
# 自测
# --------------------------------------------------------------------------- #
def run(tmp: str) -> int:
    os.makedirs(tmp, exist_ok=True)
    raw = make_audio()
    print(f"原始音频 {len(raw)} 字节，头 {raw[:4]!r}")

    cases = [
        ("qmcflac", pack_qmc_v1(raw), lambda p, d: qmc.decrypt_v1(p, d), "flac"),
        ("kgm", pack_kgm(raw), lambda p, d: kgm.decrypt(p, d), "flac"),
        ("vpr", pack_kgm(raw, vpr=True), lambda p, d: kgm.decrypt(p, d), "flac"),
        ("kwm", pack_kwm(raw), lambda p, d: kwm.decrypt(p, d), "flac"),
    ]

    fail = 0
    for ext, blob, fn, want in cases:
        src = os.path.join(tmp, "case." + ext)
        dst = os.path.join(tmp, "case_out.flac")
        with open(src, "wb") as fh:
            fh.write(blob)
        try:
            res = fn(src, dst)
            got = open(dst, "rb").read()
            same = got == raw
            print(f"  {ext:8s} kind={res.kind:5s} size={res.size:8d} "
                  f"逐字节一致={'是' if same else '否'}")
            if not same:
                fail += 1
                # 打印第一处差异，便于定位
                for i in range(min(len(got), len(raw))):
                    if got[i] != raw[i]:
                        print(f"    首处差异 @ {i}: got={got[i]:02x} want={raw[i]:02x}")
                        break
        except Exception as exc:  # noqa: BLE001
            print(f"  {ext:8s} FAIL {type(exc).__name__}: {exc}")
            fail += 1
        finally:
            for p in (src, dst):
                if os.path.exists(p):
                    os.remove(p)
    print(f"\n失败 {fail} 个")
    return fail


if __name__ == "__main__":
    import tempfile
    raise SystemExit(run(tempfile.mkdtemp(prefix="af_fx_")))
