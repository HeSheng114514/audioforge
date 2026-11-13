# -*- coding: utf-8 -*-
"""处理管线：把"格式转换 + 8bit + 剪辑 + 调音"串成一次任务。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional

import numpy as np

from . import dsp
from . import unlock as unlock_mod
from .audio_io import AudioData, AudioError, decode, encode, probe, is_supported
from .chip import ChipParams, preset_params, render_chip, PRESETS, chip_summary
from .formats import get_format, pick_rate, QUALITY_PRESETS


@dataclass
class JobSpec:
    """一次转换任务的完整描述。"""
    input: str = ""
    output: str = ""
    fmt: str = "mp3"                     # 输出格式 key
    quality: str = "standard"            # 品质档
    bitrate: int = 0                     # >0 覆盖品质档
    rate: int = 0                        # >0 重采样
    bits: int = 16                       # PCM 位深
    volume_db: float = 0.0
    normalize: str = "none"              # none / peak / rms
    normalize_db: float = -1.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    start: float = 0.0
    end: float = 0.0                     # 0=到结尾
    speed: float = 1.0
    pitch: float = 0.0                   # 半音
    channels: str = "keep"               # keep / mono / stereo / swap
    trim_silence: bool = False
    reverse: bool = False
    eq: List[float] = field(default_factory=list)
    chip_enabled: bool = False
    chip_preset: str = "nes"
    chip: Dict = field(default_factory=dict)
    metadata: Dict[str, str] = field(default_factory=dict)
    keep_original_tags: bool = True
    auto_unlock: bool = True             # 输入是私有加密格式时自动解密
    ekey_file: str = ""                  # QMC v2 的外置 ekey

    def public(self) -> dict:
        return asdict(self)


def _out_ext(spec: JobSpec) -> str:
    return get_format(spec.fmt).ext


def build_output_path(src: str, out_dir: str, spec: JobSpec, suffix: str = "") -> str:
    base = os.path.splitext(os.path.basename(src))[0]
    if suffix:
        base = f"{base}{suffix}"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, base + _out_ext(spec))
    if not os.path.exists(path):
        return path
    i = 2
    while True:
        cand = os.path.join(out_dir, f"{base} ({i}){_out_ext(spec)}")
        if not os.path.exists(cand):
            return cand
        i += 1


def describe_chain(spec: JobSpec) -> List[str]:
    """人类可读的处理链，用于 UI 展示。"""
    steps: List[str] = []

    # 输入是私有加密格式时，第一步永远是解密
    if spec.input and os.path.isfile(spec.input):
        try:
            det = unlock_mod.detect(spec.input)
        except Exception:  # noqa: BLE001
            det = None
        if det is not None and det.encrypted:
            if spec.auto_unlock:
                steps.append(f"解锁 {det.kind}（{det.platform}）")
            else:
                steps.append(f"⚠ {det.kind} 加密未解锁")

    if spec.chip_enabled:
        p = preset_params(spec.chip_preset, spec.chip)
        label = PRESETS.get(spec.chip_preset, {}).get("label", spec.chip_preset)
        steps.append(f"8bit 芯片化 · {label}（{p.bit_depth}bit / {p.sample_rate}Hz）")
    if spec.trim_silence:
        steps.append("自动去除首尾静音")
    if spec.start or spec.end:
        steps.append(f"裁剪 {spec.start:.1f}s → {spec.end or '末尾':.1f}s" if spec.end else f"从 {spec.start:.1f}s 开始")
    if abs(spec.pitch) > 0.001:
        steps.append(f"移调 {spec.pitch:+.1f} 半音")
    if abs(spec.speed - 1.0) > 0.001:
        steps.append(f"变速 ×{spec.speed:.2f}")
    if spec.eq and any(abs(g) > 0.01 for g in spec.eq):
        steps.append("10 段均衡")
    if spec.channels != "keep":
        steps.append({"mono": "混合单声道", "stereo": "转立体声", "swap": "左右互换"}.get(spec.channels, spec.channels))
    if spec.normalize != "none":
        steps.append(f"响度归一化（{spec.normalize}）")
    if abs(spec.volume_db) > 0.01:
        steps.append(f"增益 {spec.volume_db:+.1f} dB")
    if spec.fade_in or spec.fade_out:
        steps.append(f"淡入 {spec.fade_in:.1f}s / 淡出 {spec.fade_out:.1f}s")
    if spec.reverse:
        steps.append("倒放")
    if spec.rate:
        steps.append(f"重采样 {spec.rate} Hz")
    steps.append(f"编码 {spec.fmt.upper()}")
    return steps


def process(spec: JobSpec, *, progress: Callable[[float, str], None] = None) -> Dict:
    """执行一次任务，返回结果信息。"""
    t0 = time.time()
    src = spec.input
    if not os.path.exists(src):
        raise AudioError(f"输入文件不存在: {src}")
    if not is_supported(src) and not unlock_mod.is_supported(src):
        raise AudioError(f"不支持的输入格式: {os.path.splitext(src)[1]}")

    def emit(p: float, msg: str):
        if progress:
            progress(max(0.0, min(1.0, p)), msg)

    emit(0.01, "检查格式")
    unlock_info = None
    working = src
    temp_files: List[str] = []
    det = unlock_mod.detect(src)
    if det.encrypted:
        if not spec.auto_unlock:
            raise AudioError(
                f"{det.platform} {det.kind} 是加密格式，请先在「解锁」页解密后再转换")
        emit(0.02, f"解密 {det.kind}")
        ekey = None
        if spec.ekey_file and os.path.isfile(spec.ekey_file):
            with open(spec.ekey_file, "rb") as fh:
                ekey = fh.read()
        tmp_dir = os.path.join(os.path.dirname(os.path.abspath(spec.output or ".")), ".unlock_tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_path = os.path.join(tmp_dir, f"{os.getpid()}_{abs(hash(src))}.bin")
        try:
            res = unlock_mod.unlock(src, tmp_path, ekey=ekey,
                                    progress=lambda p, m: emit(0.02 + p * 0.16, m))
        except Exception:
            raise
        working = res.path
        temp_files.append(working)
        unlock_info = {
            "platform": det.platform,
            "kind": det.kind,
            "audio_format": res.kind,
            "size": res.size,
            "note": res.note,
            "tags": res.meta,
        }
        emit(0.18, "解密完成")

    try:
        return _process_audio(spec, working, unlock_info, progress, emit, t0)
    finally:
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass


def _process_audio(spec: JobSpec, src: str, unlock_info: Optional[Dict],
                   progress, emit, t0: float) -> Dict:
    emit(0.02, "读取音频")
    info = probe(src)
    fmt_spec = get_format(spec.fmt)
    q = QUALITY_PRESETS.get(spec.quality, QUALITY_PRESETS["standard"])
    target_rate = int(spec.rate or q.get("rate") or info.get("rate") or 44100)
    if spec.chip_enabled:
        # 芯片化后按抽样率工作，保留完整谐波再看是否需要处理
        target_rate = int(max(22050, min(target_rate, 48000)))

    audio = decode(src, target_rate=target_rate, mono=False,
                   progress=lambda p, m: emit(0.02 + p * 0.4, m))

    if spec.trim_silence:
        emit(0.45, "去除静音")
        audio = dsp.trim_silence(audio)

    if spec.start or spec.end:
        emit(0.48, "裁剪")
        audio = audio.slice(spec.start, spec.end or None)

    if spec.reverse:
        audio = dsp.reverse(audio)

    if abs(spec.pitch) > 0.001:
        emit(0.5, "移调")
        audio = dsp.change_pitch(audio, spec.pitch)

    if abs(spec.speed - 1.0) > 0.001:
        emit(0.58, "变速")
        audio = dsp.change_speed(audio, spec.speed, keep_pitch=True)

    if spec.eq and any(abs(g) > 0.01 for g in spec.eq):
        emit(0.64, "均衡")
        audio = dsp.equalize(audio, spec.eq)

    if spec.channels != "keep":
        emit(0.66, "声道处理")
        audio = dsp.set_channels(audio, spec.channels)

    if spec.chip_enabled:
        emit(0.68, "8bit 芯片化")
        params = preset_params(spec.chip_preset, spec.chip)
        audio = render_chip(audio, params)

    if abs(spec.volume_db) > 0.01:
        audio = dsp.apply_gain(audio, spec.volume_db)

    if spec.normalize != "none":
        emit(0.84, "响度归一化")
        audio = dsp.normalize(audio, target_db=spec.normalize_db, mode=spec.normalize)

    if spec.fade_in or spec.fade_out:
        emit(0.88, "淡入淡出")
        audio = dsp.fade(audio, spec.fade_in, spec.fade_out)

    audio = dsp.limiter(audio, ceiling_db=-0.4)

    # 目标采样率最终对齐
    final_rate = pick_rate(fmt_spec, int(spec.rate or q.get("rate") or audio.rate))
    if final_rate != audio.rate:
        audio = dsp.resample(audio, final_rate)

    # 元数据
    meta: Dict[str, str] = {}
    if spec.keep_original_tags:
        meta.update({k: v for k, v in info.get("tags", {}).items()
                     if k in ("title", "artist", "album", "album_artist", "date", "genre", "comment")})
    if not meta.get("title"):
        meta["title"] = os.path.splitext(os.path.basename(src))[0]
    meta.update({k: v for k, v in (spec.metadata or {}).items() if v})

    out_path = spec.output or ""
    if not out_path:
        raise AudioError("缺少输出路径")
    emit(0.9, "编码输出")
    encode(audio, out_path, spec.fmt,
           bitrate=int(spec.bitrate or q["bitrate"]),
           bits=int(spec.bits or q["bits"]),
           metadata=meta,
           progress=lambda p, m: emit(0.9 + p * 0.09, m))

    out_info = probe(out_path)
    result = {
        "ok": True,
        "input": src,
        "output": out_path,
        "elapsed": round(time.time() - t0, 2),
        "in_info": info,
        "out_info": out_info,
        "chain": describe_chain(spec),
        "ratio": round(out_info["size"] / max(1, info["size"]), 3),
    }
    if spec.chip_enabled:
        result["chip"] = chip_summary(preset_params(spec.chip_preset, spec.chip))
    if unlock_info:
        result["unlock"] = unlock_info
        result["chain"] = [f"解锁 {unlock_info['kind']}（{unlock_info['platform']}）"] + result["chain"]
    emit(1.0, "完成")
    return result
