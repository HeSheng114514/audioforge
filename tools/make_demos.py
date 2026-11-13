# -*- coding: utf-8 -*-
"""生成若干演示音频，方便第一次运行就有东西可试。

用法：python tools/make_demos.py [--out samples]
生成 4 首风格各异的短曲：
  demo_synthwave.mp3  合成器波（和弦 + 琶音 + 鼓）
  demo_chip_theme.wav 原创芯片主题（纯方波，适合对比 8bit 效果）
  demo_lofi.wav       低速 Lo-Fi 循环
  demo_sweep.flac     全频段扫频（用来验证频响与采样率）
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.audio_io import AudioData, encode  # noqa: E402
from core.console import enable_utf8  # noqa: E402

enable_utf8()


def adsr(n: int, a=0.01, d=0.1, s=0.7, r=0.2, rate=44100) -> np.ndarray:
    a_n, d_n, r_n = int(a * rate), int(d * rate), int(r * rate)
    env = np.ones(n, dtype=np.float32)
    a_n = min(a_n, n)
    d_n = min(d_n, max(0, n - a_n))
    r_n = min(r_n, max(0, n - a_n - d_n))
    s_n = max(0, n - a_n - d_n - r_n)
    parts = []
    if a_n:
        parts.append(np.linspace(0, 1, a_n, dtype=np.float32))
    if d_n:
        parts.append(np.linspace(1, s, d_n, dtype=np.float32))
    if s_n:
        parts.append(np.full(s_n, s, dtype=np.float32))
    if r_n:
        parts.append(np.linspace(s, 0, r_n, dtype=np.float32))
    env = np.concatenate(parts) if parts else env
    if env.size < n:
        env = np.pad(env, (0, n - env.size))
    return env[:n]


def square(freq: float, n: int, rate: int, duty=0.5) -> np.ndarray:
    t = np.arange(n) / rate
    ph = (t * freq) % 1.0
    return np.where(ph < duty, 1.0, -1.0).astype(np.float32)


def saw(freq: float, n: int, rate: int) -> np.ndarray:
    t = np.arange(n) / rate
    return (2.0 * ((t * freq) % 1.0) - 1.0).astype(np.float32)


def kick(rate: int, dur=0.22) -> np.ndarray:
    n = int(dur * rate)
    t = np.arange(n) / rate
    f = 110 * np.exp(-t * 26) + 42
    return (np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(-t * 14)).astype(np.float32)


def hat(rate: int, dur=0.05) -> np.ndarray:
    n = int(dur * rate)
    rng = np.random.default_rng(7)
    return (rng.standard_normal(n) * np.exp(-np.arange(n) / (n * 0.28)) * 0.5).astype(np.float32)


def place(buf: np.ndarray, sig: np.ndarray, at: int, gain=1.0):
    if at < 0:
        return
    end = min(buf.size, at + sig.size)
    if end <= at:
        return
    buf[at:end] += sig[: end - at] * gain


def synthwave(rate=44100, dur=24.0) -> np.ndarray:
    n = int(rate * dur)
    out = np.zeros(n, dtype=np.float32)
    chords = [(110.0, 164.81, 220.0), (146.83, 220.0, 293.66),
              (98.0, 146.83, 196.0), (130.81, 196.0, 261.63)]
    bar = dur / 8
    for i in range(8):
        root, third, fifth = chords[i % 4]
        s = int(rate * bar * i)
        length = int(rate * bar * 0.95)
        for f, g in ((root, 0.5), (third, 0.32), (fifth, 0.28)):
            place(out, saw(f, length, rate) * adsr(length, 0.02, 0.3, 0.55, 0.4, rate), s, 0.16 * g)
        # 琶音
        for k in range(8):
            note = [root * 2, third * 2, fifth * 2, third * 2][k % 4]
            ln = int(rate * bar / 8 * 0.9)
            place(out, square(note, ln, rate, 0.35) * adsr(ln, 0.005, 0.05, 0.4, 0.06, rate),
                  s + int(rate * bar / 8 * k), 0.08)
    # 鼓
    k = kick(rate)
    for i in range(int(dur / 0.5)):
        place(out, k, int(rate * 0.5 * i), 0.5)
    h = hat(rate)
    for i in range(int(dur / 0.25)):
        place(out, h, int(rate * 0.25 * i + rate * 0.125), 0.18)
    out = np.tanh(out * 1.3)
    return (out / max(1e-9, np.abs(out).max()) * 0.92).astype(np.float32)


def chip_theme(rate=44100, dur=18.0) -> np.ndarray:
    n = int(rate * dur)
    out = np.zeros(n, dtype=np.float32)
    melody = [659.25, 783.99, 880.0, 1046.5, 880.0, 783.99, 659.25, 523.25]
    step = dur / 32
    for i in range(32):
        f = melody[i % len(melody)]
        ln = int(rate * step * 0.85)
        place(out, square(f, ln, rate, 0.25) * adsr(ln, 0.004, 0.04, 0.55, 0.05, rate), int(rate * step * i), 0.28)
    bass = [164.81, 130.81, 196.0, 146.83]
    for i in range(8):
        f = bass[i % 4]
        ln = int(rate * step * 4 * 0.9)
        place(out, square(f, ln, rate, 0.5) * adsr(ln, 0.01, 0.1, 0.5, 0.2, rate), int(rate * step * 4 * i), 0.22)
    for i in range(int(dur / 0.5)):
        place(out, kick(rate, 0.1)[:int(rate * 0.08)], int(rate * 0.5 * i), 0.35)
    out = np.clip(out, -1, 1)
    return (out / max(1e-9, np.abs(out).max()) * 0.9).astype(np.float32)


def lofi(rate=44100, dur=16.0) -> np.ndarray:
    n = int(rate * dur)
    out = np.zeros(n, dtype=np.float32)
    prog = [(98.0, 123.47, 146.83, 196.0), (87.31, 110.0, 130.81, 174.61),
            (110.0, 138.59, 164.81, 220.0), (73.42, 92.5, 110.0, 146.83)]
    bar = dur / 8
    for i in range(8):
        s = int(rate * bar * i)
        ln = int(rate * bar * 1.02)
        for k, f in enumerate(prog[i % 4]):
            place(out, np.sin(2 * np.pi * f * np.arange(ln) / rate) *
                  adsr(ln, 0.05, 0.4, 0.5, 0.5, rate), s, 0.14 / (1 + k * 0.35))
    # 轻微的抖动（走音）与噪声底
    rng = np.random.default_rng(3)
    out += rng.standard_normal(n).astype(np.float32) * 0.006
    out = np.tanh(out * 1.1)
    return (out / max(1e-9, np.abs(out).max()) * 0.85).astype(np.float32)


def sweep(rate=48000, dur=10.0) -> np.ndarray:
    n = int(rate * dur)
    t = np.arange(n) / rate
    f0, f1 = 40.0, rate * 0.45
    k = (f1 / f0) ** (1.0 / dur)
    phase = 2 * np.pi * f0 * (k ** t - 1) / math.log(k)
    sig = 0.7 * np.sin(phase)
    return sig.astype(np.float32)


def stereo(x: np.ndarray, width=0.9) -> np.ndarray:
    delay = int(len(x) * 0.004)
    right = np.roll(x, delay) * width
    return np.stack([x, right]).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="samples")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    rate = 44100
    jobs = [
        ("demo_synthwave.mp3", stereo(synthwave(rate)), rate, "mp3",
         {"title": "霓虹跑道", "artist": "AudioForge", "album": "Demo", "genre": "Synthwave"}),
        ("demo_chip_theme.wav", np.stack([chip_theme(rate)] * 2), rate, "wav",
         {"title": "芯片主题", "artist": "AudioForge", "album": "Demo", "genre": "Chiptune"}),
        ("demo_lofi.wav", stereo(lofi(rate), 0.6), rate, "wav",
         {"title": "雨夜磁带", "artist": "AudioForge", "album": "Demo", "genre": "Lo-Fi"}),
        ("demo_sweep.flac", np.stack([sweep(48000)] * 2), 48000, "flac",
         {"title": "全频扫频", "artist": "AudioForge", "album": "Test", "genre": "Test"}),
    ]
    for name, data, r, fmt, meta in jobs:
        path = os.path.join(args.out, name)
        encode(AudioData(data, r), path, fmt, bitrate=320, metadata=meta)
        size = os.path.getsize(path) / 1024
        print(f"  ✓ {path:<40} {len(data[0]) / r:5.1f}s  {size:8.1f} KB")
    print("\n  演示音频生成完毕，可直接拖进 Web 界面或：")
    print("    python cli.py chip samples/demo_synthwave.mp3 --preset nes -d out\n")


if __name__ == "__main__":
    main()
