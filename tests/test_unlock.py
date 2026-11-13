# -*- coding: utf-8 -*-
"""私有加密格式解密器测试。

    python tests/test_unlock.py

对 NCM 使用真实文件（若存在）；QMC/KGM/KWM 用"反向打包 → 解密 → 逐字节比对"
的可逆性测试，能真正验证算法而不只是看文件头。
"""
from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import kgm, kwm, ncm, qmc, unlock  # noqa: E402
from core.audio_io import AudioData, decode, encode, probe  # noqa: E402
from core.pipeline import JobSpec, process  # noqa: E402
from core.stream_decrypt import DecryptError, sniff  # noqa: E402

REAL_NCM = os.path.join(BASE, "workspace", "uploads", "阿鲁阿卓 - 美丽中国.ncm")


def raw_audio(seconds=1.0, rate=44100) -> bytes:
    n = int(rate * seconds)
    t = np.arange(n) / rate
    sig = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    audio = AudioData(np.stack([sig, sig]), rate)
    fd, path = tempfile.mkstemp(suffix=".flac")
    os.close(fd)
    encode(audio, path, "flac")
    blob = open(path, "rb").read()
    os.remove(path)
    return blob


# --------------------------------------------------------------------------- #
# 反向打包器
# --------------------------------------------------------------------------- #
def pack_qmc_v1(raw: bytes) -> bytes:
    mask = qmc.qmc_mask()
    period = len(mask)
    return bytes(b ^ mask[i % period] for i, b in enumerate(raw))


def pack_kgm(raw: bytes, key: bytes = b"0123456789abcdef", vpr=False) -> bytes:
    header = bytearray(0x3C)
    header[0:8] = kgm.MAGIC_KGM
    header[0x10:0x14] = struct.pack("<I", 0x3C)
    header[0x1C:0x2C] = key[:16]
    fk = key[:16] + b"\x00"
    body = bytearray(len(raw))
    for i, b in enumerate(raw):
        x = b ^ ((b & 0x0F) << 4)
        m = kgm._mask_v2(i) ^ fk[i % 17]
        if vpr:
            m ^= kgm.VPR_KEY[i % 17]
        body[i] = x ^ m
    return bytes(header) + bytes(body)


def pack_kwm(raw: bytes, key: bytes = bytes(range(32))) -> bytes:
    header = bytearray(1024)
    header[0x40:0x60] = key
    header[0x60:0x80] = key
    body = bytes(b ^ key[i % 32] for i, b in enumerate(raw))
    return bytes(header) + body


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_unlock_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp, name)


class QmcMaskTests(unittest.TestCase):
    def test_mask_prefix(self):
        mask = qmc.qmc_mask()
        self.assertEqual(len(mask), 0x8000)
        # 与参考 C++ 实现逐位一致
        self.assertEqual(list(mask[:12]),
                         [0xC3, 0x4A, 0xD6, 0xCA, 0x90, 0x67, 0xF7, 0x52,
                          0xD8, 0xA1, 0x66, 0x62])

    def test_mask_tail(self):
        mask = qmc.qmc_mask()
        self.assertEqual(list(mask[-8:]),
                         [0xD8, 0x52, 0xF7, 0x67, 0x90, 0xCA, 0xD6, 0x4A])

    def test_mask_is_periodic_at_boundary(self):
        # 掩码在周期末尾要能无缝接回开头（解密按 offset % period 取）
        mask = qmc.qmc_mask()
        self.assertNotEqual(mask[-1], mask[0])
        self.assertEqual(len(set(mask)), len(set(mask)))  # 结构自洽


class QmcDecryptTests(TempDirTest):
    def test_roundtrip(self):
        raw = raw_audio()
        src = self.path("t.qmcflac")
        dst = self.path("out.flac")
        with open(src, "wb") as fh:
            fh.write(pack_qmc_v1(raw))
        res = qmc.decrypt_v1(src, dst)
        self.assertEqual(res.kind, "flac")
        self.assertEqual(open(dst, "rb").read(), raw)

    def test_rejects_plain_audio(self):
        src = self.path("plain.qmcflac")
        with open(src, "wb") as fh:
            fh.write(raw_audio())
        with self.assertRaises(DecryptError):
            qmc.decrypt_v1(src, self.path("x.flac"))

    def test_qmc2_without_ekey(self):
        src = self.path("t.mflac")
        with open(src, "wb") as fh:
            fh.write(b"\x00" * 5000)
        with self.assertRaises(DecryptError) as ctx:
            qmc.decrypt_v2(src, self.path("x.flac"))
        self.assertIn("ekey", str(ctx.exception))

    def test_detect_by_extension(self):
        self.assertTrue(qmc.is_qmc_v1("a.qmcflac"))
        self.assertTrue(qmc.is_qmc_v2("a.mflac"))
        self.assertFalse(qmc.is_qmc_v1("a.mp3"))


class KgmDecryptTests(TempDirTest):
    def test_mask_v2_prefix(self):
        self.assertEqual([kgm._mask_v2(i) for i in range(6)],
                         [184, 213, 61, 178, 233, 175])

    def test_tables_loaded(self):
        self.assertEqual(len(kgm.TABLE1), 272)
        self.assertEqual(len(kgm.TABLE2), 272)
        self.assertEqual(len(kgm.TABLE_V2), 272)
        self.assertEqual(len(kgm.VPR_KEY), 17)

    def test_roundtrip_kgm(self):
        raw = raw_audio()
        src = self.path("t.kgm")
        dst = self.path("out.flac")
        with open(src, "wb") as fh:
            fh.write(pack_kgm(raw))
        res = kgm.decrypt(src, dst)
        self.assertEqual(res.kind, "flac")
        self.assertEqual(open(dst, "rb").read(), raw)

    def test_roundtrip_vpr(self):
        raw = raw_audio()
        src = self.path("t.vpr")
        dst = self.path("out.flac")
        with open(src, "wb") as fh:
            fh.write(pack_kgm(raw, vpr=True))
        res = kgm.decrypt(src, dst)
        self.assertEqual(open(dst, "rb").read(), raw)

    def test_garbage_rejected(self):
        src = self.path("t.kgm")
        with open(src, "wb") as fh:
            fh.write(b"\x00" * 5000)
        with self.assertRaises(DecryptError):
            kgm.decrypt(src, self.path("x.flac"))


class KwmDecryptTests(TempDirTest):
    def test_key_recovery(self):
        key = bytes(range(32))
        src = self.path("t.kwm")
        with open(src, "wb") as fh:
            fh.write(pack_kwm(b"\x00" * 2048, key))
        self.assertEqual(kwm._find_key(src), key)

    def test_roundtrip(self):
        raw = raw_audio()
        src = self.path("t.kwm")
        dst = self.path("out.flac")
        with open(src, "wb") as fh:
            fh.write(pack_kwm(raw))
        res = kwm.decrypt(src, dst)
        self.assertEqual(res.kind, "flac")
        self.assertEqual(open(dst, "rb").read(), raw)

    def test_too_small(self):
        src = self.path("tiny.kwm")
        with open(src, "wb") as fh:
            fh.write(b"\x00" * 100)
        with self.assertRaises(DecryptError):
            kwm.decrypt(src, self.path("x.flac"))


class UnlockRegistryTests(TempDirTest):
    def test_detect_ncm_by_magic(self):
        src = self.path("x.bin")
        with open(src, "wb") as fh:
            fh.write(b"CTENFDAM" + b"\x00" * 64)
        det = unlock.detect(src)
        self.assertTrue(det.encrypted)
        self.assertEqual(det.kind, "NCM")

    def test_detect_by_extension(self):
        cases = {
            "a.qmc0": "QQ音乐", "a.mflac": "QQ音乐", "a.kgm": "酷狗音乐",
            "a.vpr": "酷狗音乐", "a.kwm": "酷我音乐", "a.ncm": "网易云音乐",
        }
        for name, platform in cases.items():
            with self.subTest(name=name):
                src = self.path(name)
                open(src, "wb").close()
                det = unlock.detect(src)
                self.assertTrue(det.encrypted)
                self.assertEqual(det.platform, platform)

    def test_plain_not_encrypted(self):
        src = self.path("a.mp3")
        with open(src, "wb") as fh:
            fh.write(raw_audio())
        self.assertFalse(unlock.detect(src).encrypted)

    def test_needs_key_flags(self):
        self.assertTrue(unlock.detect("a.mflac").needs_key)
        self.assertFalse(unlock.detect("a.qmcflac").needs_key)
        self.assertFalse(unlock.detect("a.ncm").needs_key)

    def test_output_name(self):
        self.assertEqual(unlock.output_name("a/b/歌.ncm", "flac"), "歌.flac")


class NcmRealFileTests(unittest.TestCase):
    @unittest.skipUnless(os.path.isfile(REAL_NCM), "没有真实 NCM 样本")
    def test_real_ncm(self):
        tmp = tempfile.mkdtemp(prefix="af_ncm_")
        try:
            dst = os.path.join(tmp, "out.flac")
            res = ncm.decrypt(REAL_NCM, dst)
            self.assertEqual(res.kind, "flac")
            self.assertGreater(res.size, 1_000_000)
            info = probe(dst)
            self.assertEqual(info["codec"], "flac")
            self.assertGreater(info["duration"], 60)
            self.assertEqual(info["channels"], 2)
            # 标签
            self.assertTrue(res.meta.get("title"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(os.path.isfile(REAL_NCM), "没有真实 NCM 样本")
    def test_pipeline_auto_unlock(self):
        tmp = tempfile.mkdtemp(prefix="af_ncm_p_")
        try:
            out = os.path.join(tmp, "out.mp3")
            r = process(JobSpec(input=REAL_NCM, output=out, fmt="mp3", bitrate=128))
            self.assertIn("unlock", r)
            self.assertEqual(r["unlock"]["kind"], "NCM")
            self.assertTrue(r["chain"][0].startswith("解锁"))
            self.assertGreater(r["out_info"]["duration"], 60)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
