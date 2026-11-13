# -*- coding: utf-8 -*-
"""波形 / 频谱可视化：纯 numpy + zlib 手写 PNG，无需 Pillow 也可用（有 Pillow 会更好看）。

设计上刻意不用 matplotlib，保证打包后零额外依赖也能出图。
"""
from __future__ import annotations

import math
import struct
import zlib
from typing import List, Optional, Sequence, Tuple

import numpy as np

BG = (13, 15, 22)
GRID = (30, 35, 48)
WAVE = (96, 226, 197)
WAVE2 = (255, 110, 160)
PEAK = (120, 160, 255)
TEXT = (200, 210, 230)


def _png(width: int, height: int, rgb: np.ndarray) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(rgb[y].tobytes())

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


class Canvas:
    def __init__(self, w: int, h: int, bg=BG):
        self.w, self.h = int(w), int(h)
        self.buf = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        self.buf[:, :] = bg

    def hline(self, y: int, x0: int, x1: int, color):
        y = int(y)
        if 0 <= y < self.h:
            x0 = max(0, int(x0)); x1 = min(self.w - 1, int(x1))
            if x1 >= x0:
                self.buf[y, x0:x1 + 1] = color

    def vline(self, x: int, y0: int, y1: int, color):
        x = int(x)
        if 0 <= x < self.w:
            y0 = max(0, int(y0)); y1 = min(self.h - 1, int(y1))
            if y1 >= y0:
                self.buf[y0:y1 + 1, x] = color

    def rect(self, x0, y0, x1, y1, color):
        x0, x1 = sorted((max(0, int(x0)), min(self.w, int(x1))))
        y0, y1 = sorted((max(0, int(y0)), min(self.h, int(y1))))
        if x1 > x0 and y1 > y0:
            self.buf[y0:y1, x0:x1] = color

    def alpha_rect(self, x0, y0, x1, y1, color, alpha: float):
        x0, x1 = sorted((max(0, int(x0)), min(self.w, int(x1))))
        y0, y1 = sorted((max(0, int(y0)), min(self.h, int(y1))))
        if x1 > x0 and y1 > y0:
            region = self.buf[y0:y1, x0:x1].astype(np.float32)
            c = np.array(color, dtype=np.float32)
            self.buf[y0:y1, x0:x1] = np.clip(region * (1 - alpha) + c * alpha, 0, 255).astype(np.uint8)

    def tobytes(self) -> bytes:
        return _png(self.w, self.h, self.buf)


def _envelope(mono: np.ndarray, columns: int) -> Tuple[np.ndarray, np.ndarray]:
    n = mono.size
    if n == 0:
        return np.zeros(columns, np.float32), np.zeros(columns, np.float32)
    cols = max(1, columns)
    edges = np.linspace(0, n, cols + 1).astype(np.int64)
    lo = np.empty(cols, np.float32)
    hi = np.empty(cols, np.float32)
    for i in range(cols):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        seg = mono[a:b]
        lo[i] = float(np.min(seg)) if seg.size else 0.0
        hi[i] = float(np.max(seg)) if seg.size else 0.0
    m = max(1e-6, float(max(np.abs(lo).max(), np.abs(hi).max())))
    return lo / m, hi / m


def waveform_png(samples: np.ndarray, rate: int, *, width: int = 1200, height: int = 260,
                 progress: float = 0.0, title: str = "", accent=WAVE) -> bytes:
    cv = Canvas(width, height)
    # 网格
    for i in range(1, 8):
        x = int(width * i / 8)
        cv.vline(x, 0, height, GRID)
    for i in range(1, 4):
        y = int(height * i / 4)
        cv.hline(y, 0, width, GRID)
    mid = height // 2
    mono = samples.mean(axis=0) if samples.ndim > 1 else samples
    lo, hi = _envelope(mono.astype(np.float32), width)
    amp = height * 0.45
    for x in range(width):
        y0 = int(mid - hi[x] * amp)
        y1 = int(mid - lo[x] * amp)
        if y1 < y0:
            y0, y1 = y1, y0
        cv.vline(x, y0, max(y0 + 1, y1), accent)
    cv.hline(mid, 0, width, (60, 70, 90))
    if progress > 0:
        px = int(width * min(1.0, progress))
        cv.alpha_rect(px, 0, width, height, (0, 0, 0), 0.45)
        cv.vline(px, 0, height, WAVE2)
    return cv.tobytes()


def spectrum_png(samples: np.ndarray, rate: int, *, width: int = 1200, height: int = 300,
                 fft: int = 2048, accent=WAVE) -> bytes:
    """频谱瀑布图（时间 × 频率，颜色 = 能量）。"""
    mono = (samples.mean(axis=0) if samples.ndim > 1 else samples).astype(np.float32)
    if mono.size < fft:
        mono = np.pad(mono, (0, fft - mono.size))
    hop = max(1, mono.size // width)
    cols = max(1, min(width, mono.size // hop))
    win = np.hanning(fft).astype(np.float32)
    spec = np.zeros((height, cols), dtype=np.float32)
    for c in range(cols):
        a = c * hop
        seg = mono[a:a + fft]
        if seg.size < fft:
            seg = np.pad(seg, (0, fft - seg.size))
        mag = np.abs(np.fft.rfft(seg * win))
        # 对数频率轴
        bins = np.logspace(0, math.log10(len(mag) - 1), height)
        vals = np.interp(bins, np.arange(len(mag)), mag)
        spec[:, c] = vals
    spec = np.log1p(spec * 12.0)
    m = float(spec.max()) or 1.0
    spec = np.clip(spec / m, 0, 1)
    cv = Canvas(cols, height)
    # 伪彩：低频暖 / 高频冷
    for y in range(height):
        t = y / max(1, height - 1)
        base = np.array([30 + 225 * t, 60 + 150 * (1 - abs(t - 0.55) * 1.6), 200 - 120 * t])
        row = spec[y][:, None] * base[None, :]
        cv.buf[height - 1 - y, :, :] = np.clip(row + 8, 0, 255).astype(np.uint8)
    for i in range(1, 8):
        cv.vline(int(cols * i / 8), 0, height, (255, 255, 255))
        cv.alpha_rect(int(cols * i / 8), 0, int(cols * i / 8) + 1, height, (0, 0, 0), 0.7)
    cv.hline(height // 2, 0, cols, (0, 0, 0))
    cv.alpha_rect(0, height // 2, cols, height // 2 + 1, (0, 0, 0), 0.6)
    return cv.tobytes()


def compare_png(before: np.ndarray, after: np.ndarray, *, width: int = 1200,
                height: int = 300, rate: int = 44100) -> bytes:
    """上下对比：原始波形 vs 芯片化波形（用于 UI 的 A/B 展示）。"""
    cv = Canvas(width, height)
    half = height // 2
    for idx, (data, accent, y0, y1) in enumerate((
            (before, WAVE, 0, half - 2), (after, WAVE2, half + 2, height))):
        mid = (y0 + y1) // 2
        mono = (data.mean(axis=0) if data.ndim > 1 else data).astype(np.float32)
        lo, hi = _envelope(mono, width)
        amp = (y1 - y0) * 0.45
        for x in range(width):
            a = int(mid - hi[x] * amp)
            b = int(mid - lo[x] * amp)
            if b < a:
                a, b = b, a
            cv.vline(x, a, max(a + 1, b), accent)
        cv.hline(mid, 0, width, (60, 70, 90))
    cv.hline(half, 0, width, (0, 0, 0))
    return cv.tobytes()


def wave_data(samples: np.ndarray, columns: int = 480) -> dict:
    """给前端画 canvas 用的降采样包络数据。"""
    mono = (samples.mean(axis=0) if samples.ndim > 1 else samples).astype(np.float32)
    lo, hi = _envelope(mono, columns)
    return {"min": [round(float(v), 4) for v in lo], "max": [round(float(v), 4) for v in hi]}
