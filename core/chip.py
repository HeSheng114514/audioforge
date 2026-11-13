# -*- coding: utf-8 -*-
"""8bit / 芯片音乐（chiptune）引擎。

把任意音乐"压"成红白机 / GameBoy / C64 的味道，核心是四件事：
  1. 位深量化   —— 采样值只剩 2^N 个台阶（4bit 只有 16 级，典型 FC 音色）；
  2. 采样率骤降 —— 采样保持（sample & hold），产生镜像与颗粒；
  3. 波形方化   —— 向方波/pulse 波形靠拢，去掉细腻的泛音；
  4. 芯片特效   —— 琶音、颤音、门限颤音、环形调制、噪声床、减速收尾。

所有效果都在 (channels, samples) 的 float32 数组上完成，可任意串联。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from typing import Dict, Optional

import numpy as np

from .audio_io import AudioData, AudioError
from . import dsp


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
@dataclass
class ChipParams:
    bit_depth: int = 8            # 1..16
    sample_rate: int = 11025      # 目标等效采样率（Hz），越高越细
    squareness: float = 0.0       # 0..1 波形方化程度
    crush_mix: float = 1.0        # 干湿比
    gate: float = 0.0             # 0..1 门限颤音（步进音量）强度
    gate_hz: float = 12.0
    vibrato_semitones: float = 0.0
    vibrato_hz: float = 5.5
    arp_semitones: float = 0.0    # 琶音跳跃的半音数
    arp_hz: float = 14.0
    ring_hz: float = 0.0          # 环形调制频率
    ring_mix: float = 0.0
    noise: float = 0.0            # 噪声床电平 0..1
    lowpass: float = 0.0          # 0=自动（按目标采样率），>0 手动 Hz
    highpass: float = 20.0
    stereo: str = "keep"          # keep / mono / widen / pingpong
    drive: float = 0.0            # 前置过载 0..1
    tape_stop: float = 0.0        # 结尾减速秒数
    dither: str = "none"          # none / noise / noise-shape
    volume_db: float = -1.0
    auto_trim_bass: bool = True

    def public(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 基础单元
# --------------------------------------------------------------------------- #
def quantize(data: np.ndarray, bits: int, mode: str = "none") -> np.ndarray:
    """把 -1..1 量化到 2^bits 个台阶。"""
    bits = int(max(1, min(16, bits)))
    levels = (1 << bits) - 1
    x = np.clip(data, -1.0, 1.0)
    if mode == "noise":
        x = x + np.random.uniform(-0.5, 0.5, x.shape) / max(levels, 1)
    elif mode == "noise-shape":
        x = x + np.random.uniform(-1.0, 1.0, x.shape) / max(levels, 1) * 0.7
    q = np.round((x + 1.0) * 0.5 * levels) / levels * 2.0 - 1.0
    return q.astype(np.float32)


def downsample_hold(data: np.ndarray, rate: int, target_rate: int) -> np.ndarray:
    """采样保持：以 target_rate 抽样后零阶保持回原采样率。"""
    if target_rate <= 0 or rate <= 0 or target_rate >= rate:
        return data
    hold = max(1, int(round(rate / float(target_rate))))
    n = data.shape[1]
    idx = (np.arange(n) // hold) * hold
    idx = np.clip(idx, 0, n - 1)
    return data[:, idx].astype(np.float32)


def squareify(data: np.ndarray, amount: float) -> np.ndarray:
    if amount <= 0:
        return data
    amount = float(min(1.0, amount))
    # 软化方波：tanh 提升谐波，再与原信号混合
    driven = np.tanh(data * (1.0 + 24.0 * amount)) * (0.72 / max(0.4, 1.0 + 0.2 * amount))
    driven = np.clip(driven, -1.0, 1.0)
    return ((1 - amount) * data + amount * driven).astype(np.float32)


def _lfo(n: int, rate: int, hz: float, shape: str = "sine") -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / rate
    ph = 2 * math.pi * hz * t
    if shape == "square":
        return np.where(np.sin(ph) >= 0, 1.0, -1.0)
    if shape == "tri":
        return 2.0 / math.pi * np.arcsin(np.sin(ph))
    if shape == "step4":
        return np.floor((np.sin(ph) * 0.5 + 0.5) * 4) / 3.0 * 2 - 1.0
    return np.sin(ph)


def gate_mod(data: np.ndarray, rate: int, hz: float, amount: float) -> np.ndarray:
    """步进门限：模拟芯片的 volume envelope（每帧只有 0/1/2/3 级）。"""
    if amount <= 0:
        return data
    n = data.shape[1]
    lfo = _lfo(n, rate, hz, "step4") * 0.5 + 0.5     # 0..1
    lfo = np.floor(lfo * 3) / 3.0                       # 4 级台阶
    gain = (1 - amount) + amount * lfo
    return (data * gain[None, :]).astype(np.float32)


def vibrato(data: np.ndarray, rate: int, semitones: float, hz: float,
            shape: str = "sine") -> np.ndarray:
    """颤音：按 LFO 调制播放速度（重采样位置映射）。"""
    if abs(semitones) < 1e-3:
        return data
    n = data.shape[1]
    depth = 2.0 ** (semitones / 12.0) - 1.0
    lfo = _lfo(n, rate, hz, shape)
    ratio = 1.0 + depth * lfo
    pos = np.cumsum(ratio)
    pos = np.clip(pos, 0, n - 1)
    out = np.empty_like(data)
    src = np.arange(n)
    for ch in range(data.shape[0]):
        out[ch] = np.interp(pos, src, data[ch]).astype(np.float32)
    return out


def arpeggio(data: np.ndarray, rate: int, semitones: float, hz: float) -> np.ndarray:
    """琶音：以音阶跳进的方式快速在两个音高之间切换（FC 常见的 arpeggio 效果）。"""
    if semitones <= 0.05:
        return data
    n = data.shape[1]
    block = max(1, int(rate / max(1.0, hz)))
    ratio_hi = 2.0 ** (semitones / 12.0)
    pattern = [1.0, ratio_hi, 1.0 / ratio_hi] if semitones > 4 else [1.0, ratio_hi]
    pos = np.zeros(n, dtype=np.float64)
    p = 0.0
    src = np.arange(n)
    out = np.empty_like(data)
    for start in range(0, n, block):
        end = min(n, start + block)
        r = pattern[(start // block) % len(pattern)]
        seg = end - start
        pos[start:end] = p + np.arange(seg) * r
        p += seg * r
    pos = np.clip(pos, 0, n - 1)
    for ch in range(data.shape[0]):
        out[ch] = np.interp(pos, src, data[ch]).astype(np.float32)
    return out


def ring_mod(data: np.ndarray, rate: int, hz: float, mix: float) -> np.ndarray:
    if hz <= 0 or mix <= 0:
        return data
    n = data.shape[1]
    t = np.arange(n, dtype=np.float64) / rate
    carrier = np.sign(np.sin(2 * math.pi * hz * t))  # 方波载波，更有芯片味
    wet = data * carrier[None, :]
    return ((1 - mix) * data + mix * wet).astype(np.float32)


def noise_bed(data: np.ndarray, rate: int, level: float) -> np.ndarray:
    """噪声通道：按信号包络生成门控白噪声（FC 的 noise channel 质感）。"""
    if level <= 0:
        return data
    n = data.shape[1]
    noise = np.random.uniform(-1, 1, n).astype(np.float32)
    noise = np.repeat(noise[::8], 8)[:n]        # 粗糙的、带采样保持的噪声
    if noise.shape[0] < n:
        noise = np.pad(noise, (0, n - noise.shape[0]))
    mono = np.abs(data).mean(axis=0)
    win = max(1, int(rate * 0.01))
    nb = max(1, mono.size // win)
    env = np.max(np.abs(mono[: nb * win]).reshape(nb, win), axis=1)
    env = np.repeat(env, win)[:n]
    if env.shape[0] < n:
        env = np.pad(env, (0, n - env.shape[0]))
    bed = noise * env * float(level)
    return (data + bed[None, :]).astype(np.float32)


def pingpong_stereo(data: np.ndarray, rate: int, delay_ms: float = 90.0) -> np.ndarray:
    """左右交替延迟，做出芯片立体声的"左右声道分轨"感。"""
    if data.shape[0] < 2:
        return data
    d = max(1, int(rate * delay_ms / 1000.0))
    left = data[0].copy()
    right = data[1].copy()
    out = np.stack([left, right])
    out[0, d:] += right[:-d] * 0.35
    out[1, d:] += left[:-d] * 0.35
    return out.astype(np.float32)


# --------------------------------------------------------------------------- #
# 预设
# --------------------------------------------------------------------------- #
PRESETS: Dict[str, dict] = {
    "nes": {
        "label": "红白机 FC", "desc": "4bit 脉冲波 + 11kHz，最经典的 8bit 味",
        "params": dict(bit_depth=4, sample_rate=11025, squareness=0.55, crush_mix=1.0,
                       gate=0.35, gate_hz=13.0, vibrato_semitones=0.22, vibrato_hz=5.8,
                       arp_semitones=0.0, lowpass=0.0, highpass=60.0, stereo="mono",
                       drive=0.25, dither="noise", volume_db=-1.0),
    },
    "gameboy": {
        "label": "GameBoy", "desc": "4bit + 8kHz，比 FC 更闷更糙",
        "params": dict(bit_depth=4, sample_rate=8000, squareness=0.7, crush_mix=1.0,
                       gate=0.45, gate_hz=9.0, vibrato_semitones=0.3, vibrato_hz=4.6,
                       lowpass=3400.0, highpass=90.0, stereo="mono", drive=0.3,
                       dither="noise", volume_db=-1.0),
    },
    "c64": {
        "label": "C64 (SID)", "desc": "8bit + 环形调制，带 SID 的金属感",
        "params": dict(bit_depth=6, sample_rate=12000, squareness=0.3, crush_mix=1.0,
                       gate=0.12, gate_hz=18.0, vibrato_semitones=0.12, vibrato_hz=6.4,
                       ring_hz=220.0, ring_mix=0.22, lowpass=6000.0, highpass=45.0,
                       stereo="widen", drive=0.15, dither="noise", volume_db=-1.0),
    },
    "arcade": {
        "label": "街机", "desc": "8bit + 立体声乒乓，明亮有颗粒",
        "params": dict(bit_depth=6, sample_rate=16000, squareness=0.4, crush_mix=1.0,
                       gate=0.2, gate_hz=14.0, vibrato_semitones=0.15,
                       arp_semitones=0.0, lowpass=8000.0, highpass=50.0,
                       stereo="pingpong", drive=0.2, dither="noise", volume_db=-1.0),
    },
    "pixel": {
        "label": "8bit 轻量", "desc": "8bit 位深 + 22kHz，保留更多细节，适合整首歌",
        "params": dict(bit_depth=8, sample_rate=22050, squareness=0.12, crush_mix=1.0,
                       gate=0.0, lowpass=0.0, highpass=30.0, stereo="keep",
                       drive=0.05, dither="noise", volume_db=-1.0),
    },
    "tape": {
        "label": "磁带 Lo-Fi", "desc": "8bit + 失真 + 轻微抖动，复古卡带味",
        "params": dict(bit_depth=8, sample_rate=16000, squareness=0.18, crush_mix=1.0,
                       gate=0.0, vibrato_semitones=0.35, vibrato_hz=0.9,
                       lowpass=5200.0, highpass=70.0, stereo="widen",
                       drive=0.4, noise=0.02, dither="noise-shape", volume_db=-1.5),
    },
    "soft": {
        "label": "微 8bit", "desc": "16bit / 32kHz，只加一点点颗粒与方化",
        "params": dict(bit_depth=12, sample_rate=32000, squareness=0.06, crush_mix=1.0,
                       gate=0.0, lowpass=0.0, highpass=20.0, stereo="keep",
                       drive=0.0, dither="none", volume_db=-1.0),
    },
    "custom": {
        "label": "自定义", "desc": "全部参数手动调",
        "params": dict(),
    },
}


def preset_params(name: str, overrides: Optional[dict] = None) -> ChipParams:
    base = PRESETS.get(name, PRESETS["nes"])["params"]
    merged = dict(base)
    if overrides:
        merged.update({k: v for k, v in overrides.items() if v is not None})
    valid = {f.name for f in ChipParams.__dataclass_fields__.values()}
    merged = {k: v for k, v in merged.items() if k in valid}
    return ChipParams(**merged)


# --------------------------------------------------------------------------- #
# 主处理
# --------------------------------------------------------------------------- #
def render_chip(audio: AudioData, params: ChipParams) -> AudioData:
    rate = audio.rate
    data = audio.samples.astype(np.float32, copy=True)

    # 1) 声道布局
    if params.stereo == "mono":
        data = data.mean(axis=0, keepdims=True)
    elif params.stereo in ("widen", "pingpong") and data.shape[0] == 1:
        data = np.repeat(data, 2, axis=0)

    # 2) 前置过载
    if params.drive > 0:
        g = 1.0 + 6.0 * float(params.drive)
        data = np.tanh(data * g) / math.tanh(g) * 0.92

    # 3) 切除芯片发不出的超低频（否则量化后会变成噗噗声）
    if params.auto_trim_bass and params.highpass < 25:
        params = ChipParams(**{**params.public(), "highpass": 60.0})
    if params.highpass > 20:
        cut = min(params.highpass, rate * 0.4)
        data = dsp.highpass(AudioData(data, rate), cut).samples

    # 4) 芯片步进效果（顺序：琶音 → 颤音 → 门限）
    if params.arp_semitones > 0:
        data = arpeggio(data, rate, params.arp_semitones, params.arp_hz)
    if params.vibrato_semitones > 0:
        data = vibrato(data, rate, params.vibrato_semitones, params.vibrato_hz)
    if params.gate > 0:
        data = gate_mod(data, rate, params.gate_hz, params.gate)

    # 5) 方化 → 降采样 → 量化
    if params.squareness > 0:
        data = squareify(data, params.squareness)
    dry = AudioData(data.copy(), rate)
    data = downsample_hold(data, rate, params.sample_rate)
    data = quantize(data, params.bit_depth, params.dither)

    if params.crush_mix < 0.999:
        wet = AudioData(data, rate)
        mix = params.crush_mix
        # 干湿需要长度一致
        n = min(dry.frames, wet.frames)
        data = (dry.samples[:, :n] * (1 - mix) + wet.samples[:, :n] * mix).astype(np.float32)

    # 6) 环形调制 / 噪声床
    if params.ring_mix > 0 and params.ring_hz > 0:
        data = ring_mod(data, rate, params.ring_hz, params.ring_mix)
    if params.noise > 0:
        data = noise_bed(data, rate, params.noise)

    # 7) 抗混叠低通
    lp = params.lowpass if params.lowpass > 0 else params.sample_rate * 0.46
    lp = min(lp, rate * 0.47)
    if lp > 200:
        data = dsp.lowpass(AudioData(data, rate), lp, order=2).samples

    # 8) 立体声处理
    if params.stereo == "pingpong":
        data = pingpong_stereo(data, rate, 90.0)
    elif params.stereo == "widen" and data.shape[0] == 2:
        mid = (data[0] + data[1]) * 0.5
        side = (data[0] - data[1]) * 0.5 * 1.35
        data = np.stack([mid + side, mid - side])

    # 9) 减速收尾
    out = AudioData(np.clip(data, -1.0, 1.0), rate)
    if params.tape_stop > 0:
        out = dsp.tape_stop(out, params.tape_stop)

    # 10) 归一化
    out = dsp.normalize(out, target_db=params.volume_db, mode="peak", ceiling_db=-0.3)
    return out


def chip_summary(params: ChipParams) -> dict:
    """给 UI 用的科普式参数摘要。"""
    levels = (1 << int(params.bit_depth)) - 1
    nyquist = params.sample_rate / 2.0
    return {
        "levels": levels,
        "nyquist": nyquist,
        "clarity": int(round(min(100, (params.bit_depth / 16.0) * 50 + (params.sample_rate / 44100.0) * 50))),
        "text": f"{params.bit_depth}bit / {params.sample_rate}Hz 等效 / 带宽 {nyquist:.0f}Hz",
    }
