# -*- coding: utf-8 -*-
"""DSP 工具箱：重采样、增益、EQ、淡入淡出、变速变调、静音裁剪、立体声处理。

全部基于 numpy，不依赖 scipy（本机未安装 scipy）。
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

import numpy as np

from .audio_io import AudioData, AudioError

EPS = 1e-9


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def db_to_lin(db: float) -> float:
    return float(10.0 ** (db / 20.0))


def lin_to_db(x: float) -> float:
    return float(20.0 * math.log10(max(float(x), EPS)))


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def peak(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))


def apply_gain(audio: AudioData, db: float) -> AudioData:
    if abs(db) < 1e-6:
        return audio
    return AudioData(audio.samples * db_to_lin(db), audio.rate)


def normalize(audio: AudioData, target_db: float = -1.0, mode: str = "peak",
              ceiling_db: float = -0.3) -> AudioData:
    """mode: peak / rms。输出硬限幅到 ceiling。"""
    data = audio.samples.astype(np.float64, copy=False)
    if mode == "rms":
        cur = rms(data)
        target = db_to_lin(target_db - 12.0)  # RMS 目标相对峰值留 12dB
    else:
        cur = peak(data)
        target = db_to_lin(target_db)
    if cur < EPS:
        return audio
    gain = target / cur
    out = data * gain
    ceil = db_to_lin(ceiling_db)
    if peak(out) > ceil:
        out = np.tanh(out / ceil * 0.85) * ceil
    return AudioData(out.astype(np.float32), audio.rate)


def limiter(audio: AudioData, ceiling_db: float = -0.5, attack_ms: float = 3.0,
            release_ms: float = 120.0) -> AudioData:
    """简易前馈限幅器：平滑增益包络。"""
    data = audio.samples
    ceil = db_to_lin(ceiling_db)
    env = peak(data) if data.size else 0.0
    if env <= ceil:
        return audio
    # 逐块峰值包络
    block = max(1, int(audio.rate * 0.002))
    n = data.shape[1]
    nb = max(1, n // block)
    trimmed = data[:, : nb * block].reshape(data.shape[0], nb, block)
    envs = np.max(np.abs(trimmed), axis=(0, 2))
    target = np.minimum(1.0, ceil / np.maximum(envs, EPS))
    # 平滑（release 慢，attack 快）
    a_att = math.exp(-1.0 / max(1.0, attack_ms * 0.001 * audio.rate / block))
    a_rel = math.exp(-1.0 / max(1.0, release_ms * 0.001 * audio.rate / block))
    sm = np.empty_like(target)
    g = 1.0
    for i, t in enumerate(target):
        coef = a_att if t < g else a_rel
        g = coef * g + (1 - coef) * t
        sm[i] = g
    gain = np.repeat(sm, block)
    if gain.shape[0] < n:
        gain = np.concatenate([gain, np.full(n - gain.shape[0], gain[-1] if gain.size else 1.0)])
    out = data * gain[:n].astype(np.float32)
    out = np.clip(out, -ceil * 1.02, ceil * 1.02)
    return AudioData(out, audio.rate)


def fade(audio: AudioData, fade_in: float = 0.0, fade_out: float = 0.0,
         curve: str = "log") -> AudioData:
    data = audio.samples.copy()
    n = data.shape[1]
    rate = audio.rate

    def env(length: int, rising: bool) -> np.ndarray:
        if length <= 0:
            return np.ones(0, dtype=np.float32)
        t = np.linspace(0.0, 1.0, length, dtype=np.float32)
        if curve == "linear":
            e = t
        elif curve == "exp":
            e = t ** 2.5
        else:  # log 型（听感自然）
            e = np.sin(t * math.pi / 2) ** 1.6
        return e if rising else e[::-1]

    if fade_in > 0:
        k = min(n, int(fade_in * rate))
        data[:, :k] *= env(k, True)
    if fade_out > 0:
        k = min(n, int(fade_out * rate))
        data[:, n - k:] *= env(k, False)
    return AudioData(data, rate)


def trim_silence(audio: AudioData, threshold_db: float = -45.0,
                 keep_pad: float = 0.05) -> AudioData:
    thr = db_to_lin(threshold_db)
    mono = np.abs(audio.mono())
    if mono.size == 0:
        return audio
    # 用 10ms 窗求能量
    win = max(1, int(audio.rate * 0.01))
    nb = mono.size // win
    if nb == 0:
        return audio
    env = np.max(np.abs(mono[: nb * win].reshape(nb, win)), axis=1)
    idx = np.where(env > thr)[0]
    if idx.size == 0:
        raise AudioError("整段音频低于静音阈值，无法裁剪")
    pad = int(keep_pad * audio.rate)
    a = max(0, idx[0] * win - pad)
    b = min(audio.frames, (idx[-1] + 1) * win + pad)
    return AudioData(audio.samples[:, a:b], audio.rate)


# --------------------------------------------------------------------------- #
# 重采样
# --------------------------------------------------------------------------- #
def resample(audio: AudioData, new_rate: int) -> AudioData:
    new_rate = int(new_rate)
    if new_rate == audio.rate or new_rate <= 0:
        return audio
    src = audio.frames
    dst = max(1, int(round(src * new_rate / float(audio.rate))))
    x_old = np.arange(src, dtype=np.float64)
    x_new = np.linspace(0, src - 1, dst, dtype=np.float64)
    out = np.empty((audio.channels, dst), dtype=np.float32)
    for ch in range(audio.channels):
        out[ch] = np.interp(x_new, x_old, audio.samples[ch].astype(np.float64)).astype(np.float32)
    return AudioData(out, new_rate)


def _resample_array(data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return data
    src = data.shape[1]
    dst = max(1, int(round(src * dst_rate / float(src_rate))))
    x_old = np.arange(src, dtype=np.float64)
    x_new = np.linspace(0, src - 1, dst, dtype=np.float64)
    out = np.empty((data.shape[0], dst), dtype=np.float32)
    for ch in range(data.shape[0]):
        out[ch] = np.interp(x_new, x_old, data[ch].astype(np.float64)).astype(np.float32)
    return out


# --------------------------------------------------------------------------- #
# 变速 / 变调（WSOLA 时间伸缩，保持音高）
# --------------------------------------------------------------------------- #
def _resample_to(data: np.ndarray, target_len: int) -> np.ndarray:
    """把 (ch, n) 线性重采样到指定长度（端点保持）。"""
    src = data.shape[1]
    target_len = max(1, int(target_len))
    if src == target_len or src <= 1:
        return data
    x_old = np.arange(src, dtype=np.float64)
    x_new = np.linspace(0, src - 1, target_len, dtype=np.float64)
    out = np.empty((data.shape[0], target_len), dtype=np.float32)
    for ch in range(data.shape[0]):
        out[ch] = np.interp(x_new, x_old, data[ch].astype(np.float64)).astype(np.float32)
    return out


def _phase_vocoder_channel(x: np.ndarray, stretch: float, frame: int = 2048,
                           hop: int = 512) -> np.ndarray:
    """相位声码器时间伸缩（单声道）。stretch = 输出长度 / 输入长度。"""
    n = x.shape[0]
    if n < frame * 2:
        frame = max(256, 1 << int(math.floor(math.log2(max(2, n // 2)))))
        hop = max(64, frame // 4)
    n = x.shape[0]
    out_len = max(frame, int(round(n * stretch)))
    win = np.hanning(frame + 2)[1:-1].astype(np.float64)
    win_sum = win.sum()

    omega = 2.0 * math.pi * hop * np.arange(frame // 2 + 1) / frame
    prev_phase = np.zeros(frame // 2 + 1, dtype=np.float64)
    sum_phase = np.zeros(frame // 2 + 1, dtype=np.float64)
    first = True

    out = np.zeros(out_len + frame + hop, dtype=np.float64)
    norm = np.zeros_like(out)

    pos = 0.0
    out_pos = 0.0
    while pos + frame <= n and out_pos + frame <= out_len + frame:
        seg = x[int(round(pos)): int(round(pos)) + frame]
        if seg.shape[0] < frame:
            seg = np.pad(seg, (0, frame - seg.shape[0]))
        spec = np.fft.rfft(seg * win)
        mag = np.abs(spec)
        phase = np.angle(spec)

        dphi = phase - prev_phase - omega
        dphi = np.mod(dphi + math.pi, 2.0 * math.pi) - math.pi
        inst_freq = omega + dphi
        if not first:
            sum_phase += inst_freq * (stretch if stretch != 1.0 else 1.0)
        else:
            sum_phase = phase.copy()
            first = False
        prev_phase = phase

        y = np.fft.irfft(mag * np.exp(1j * sum_phase), frame) * win
        p = int(round(out_pos))
        if p + frame > out.shape[0]:
            break
        out[p:p + frame] += y
        norm[p:p + frame] += win ** 2

        pos += hop
        out_pos += hop * stretch

    valid = norm > 1e-8
    out[valid] /= norm[valid]
    return out[:out_len].astype(np.float32)


def change_speed(audio: AudioData, speed: float, keep_pitch: bool = True) -> AudioData:
    """speed>1 更快。keep_pitch=True 用相位声码器保持音高；False 直接改变速率（同时变调）。"""
    if speed <= 0:
        raise AudioError("速度必须大于 0")
    if abs(speed - 1.0) < 1e-4:
        return audio
    if not keep_pitch:
        target = max(1, int(audio.frames / speed))
        return AudioData(_resample_to(audio.samples, target), int(round(audio.rate * speed)))
    stretch = 1.0 / speed
    outs = []
    for ch in range(audio.channels):
        outs.append(_phase_vocoder_channel(audio.samples[ch], stretch))
    length = min(o.shape[0] for o in outs)
    data = np.stack([o[:length] for o in outs])
    return AudioData(data, audio.rate)


def change_pitch(audio: AudioData, semitones: float, formant: bool = False) -> AudioData:
    """移调（半音），保持时长：时间伸缩 1/ratio 倍后，用 ratio 倍速率重采样搬音高。"""
    if abs(semitones) < 1e-3:
        return audio
    ratio = 2.0 ** (semitones / 12.0)
    stretched = change_speed(audio, 1.0 / ratio, keep_pitch=True)
    exact = int(round(audio.frames * ratio))
    if stretched.frames != exact:
        stretched = AudioData(_resample_to(stretched.samples, exact), audio.rate)
    played = AudioData(stretched.samples, int(round(audio.rate * ratio)))
    out = _resample_to(played.samples, audio.frames)
    return AudioData(out, audio.rate)


# --------------------------------------------------------------------------- #
# EQ（频域增益，12 段）
# --------------------------------------------------------------------------- #
EQ_BANDS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def _design_lowpass(cutoff: float, rate: int, taps: int = 1025) -> np.ndarray:
    """窗函数法设计线性相位低通 FIR。"""
    taps = int(taps) | 1                      # 保证奇数，中心对称
    m = taps - 1
    fc = float(min(max(cutoff, 10.0), rate * 0.49)) / rate
    n = np.arange(taps) - m / 2.0
    taps_v = 2.0 * fc * np.sinc(2.0 * fc * n)
    window = np.blackman(taps)
    taps_v = taps_v * window
    s = taps_v.sum()
    if abs(s) > 1e-9:
        taps_v /= s                            # 直流增益归一
    return taps_v.astype(np.float32)


def _fir_conv(data: np.ndarray, taps: np.ndarray) -> np.ndarray:
    """FFT 线性卷积（因果、无循环卷绕），并补偿群延迟。"""
    n = data.shape[1]
    m = taps.shape[0]
    if n == 0 or m == 1:
        return data
    length = n + m - 1
    nfft = 1 << int(math.ceil(math.log2(max(2, length))))
    kernel = np.fft.rfft(taps.astype(np.float64), nfft)
    out = np.empty((data.shape[0], n), dtype=np.float32)
    delay = (m - 1) // 2
    for ch in range(data.shape[0]):
        spec = np.fft.rfft(data[ch].astype(np.float64), nfft) * kernel
        full = np.fft.irfft(spec, nfft)[:length]
        out[ch] = full[delay:delay + n].astype(np.float32)
    return out


def _taps_for(cutoff: float, rate: int) -> int:
    """截止频率越低，需要的阶数越高（保证过渡带足够窄）。"""
    if cutoff <= 0:
        return 1
    ratio = max(cutoff, 10.0) / float(rate)
    taps = int(round(8.0 / max(ratio, 1e-4)))
    return int(np.clip(taps, 129, 4097)) | 1


def lowpass(audio: AudioData, cutoff: float, order: int = 4) -> AudioData:
    """因果 FIR 低通。order 保留兼容，实际陡度由窗函数与阶数决定。"""
    cutoff = float(max(20.0, min(cutoff, audio.rate * 0.49)))
    if audio.frames == 0:
        return audio
    taps = _design_lowpass(cutoff, audio.rate, _taps_for(cutoff, audio.rate))
    return AudioData(_fir_conv(audio.samples, taps), audio.rate)


def highpass(audio: AudioData, cutoff: float, order: int = 4) -> AudioData:
    """高通 = 原信号 - 低通（同一组 FIR 抽头，相位一致）。"""
    cutoff = float(max(10.0, min(cutoff, audio.rate * 0.49)))
    if audio.frames == 0:
        return audio
    taps = _design_lowpass(cutoff, audio.rate, _taps_for(cutoff, audio.rate))
    low = _fir_conv(audio.samples, taps)
    return AudioData((audio.samples - low).astype(np.float32), audio.rate)


def equalize(audio: AudioData, gains_db: Sequence[float], bands: Sequence[float] = EQ_BANDS) -> AudioData:
    """10 段图形均衡。

    以原始信号为基准，各频段按 (增益 - 1) 做加权叠加：所有增益为 0dB 时
    输出与输入完全一致，避免整段被滤波器组削掉能量。
    """
    gains = np.asarray(list(gains_db), dtype=np.float64)
    if gains.size == 0 or np.allclose(gains, 0.0) or audio.frames == 0:
        return audio
    band_f = np.asarray(list(bands), dtype=np.float64)
    rate = audio.rate
    nyq = rate / 2.0

    edges = []
    for i, f in enumerate(band_f):
        lo = 20.0 if i == 0 else math.sqrt(band_f[i - 1] * f)
        hi = (nyq * 0.96 if i == len(band_f) - 1 else math.sqrt(f * band_f[i + 1]))
        hi = min(hi, nyq * 0.98)
        edges.append((min(lo, hi * 0.5), hi))

    lp_cache = {}

    def lowpassed(freq: float) -> np.ndarray:
        key = round(float(freq), 2)
        if key not in lp_cache:
            taps = _design_lowpass(min(freq, nyq * 0.985), rate, _taps_for(freq, rate))
            lp_cache[key] = _fir_conv(audio.samples, taps)
        return lp_cache[key]

    out = audio.samples.astype(np.float64).copy()
    for (lo, hi), g in zip(edges, gains):
        if abs(g) < 0.01:
            continue
        band = lowpassed(hi) - lowpassed(lo)
        out += band.astype(np.float64) * (db_to_lin(g) - 1.0)
    return AudioData(out.astype(np.float32), rate)


# --------------------------------------------------------------------------- #
# 立体声
# --------------------------------------------------------------------------- #
def set_channels(audio: AudioData, mode: str, width: float = 1.0) -> AudioData:
    """mode: keep / mono / stereo / swap / widen"""
    if mode == "mono":
        m = audio.mono()[None, :]
        return AudioData(m, audio.rate)
    if mode == "stereo":
        return audio.to_stereo()
    if audio.channels < 2:
        return audio.to_stereo() if mode in ("widen",) else audio
    left, right = audio.samples[0], audio.samples[1]
    if mode == "swap":
        return AudioData(np.stack([right, left]), audio.rate)
    if mode == "widen":
        mid = (left + right) * 0.5
        side = (left - right) * 0.5 * float(max(0.0, width))
        return AudioData(np.stack([mid + side, mid - side]), audio.rate)
    return audio


def reverse(audio: AudioData) -> AudioData:
    return AudioData(audio.samples[:, ::-1].copy(), audio.rate)


def tape_stop(audio: AudioData, length: float = 0.8) -> AudioData:
    """结尾磁带减速效果（八位游戏机常见的 down-pitch 收尾）。"""
    n = int(length * audio.rate)
    if n <= 0 or n > audio.frames:
        return audio
    head = audio.samples[:, :-n]
    tail = audio.samples[:, -n:]
    idx = np.linspace(0, n - 1, n) * np.linspace(1.0, 0.15, n)
    idx = np.clip(idx, 0, n - 1)
    slowed = np.stack([np.interp(idx, np.arange(n), tail[ch]) for ch in range(audio.channels)])
    return AudioData(np.concatenate([head, slowed.astype(np.float32)], axis=1), audio.rate)
