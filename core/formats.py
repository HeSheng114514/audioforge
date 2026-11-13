# -*- coding: utf-8 -*-
"""格式注册表：输出容器 / 编解码器 / 品质预设。

后端完全基于 PyAV（自带 ffmpeg 动态库），本机无需另外安装 ffmpeg.exe。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


@dataclass(frozen=True)
class FormatSpec:
    key: str                      # 内部标识
    label: str                    # 显示名
    codec: str                    # ffmpeg 编码器名
    muxer: str                    # 容器封装器
    ext: str                      # 扩展名（含点）
    kind: str                     # lossy / lossless / pcm
    bitrate: bool = False         # 是否支持码率
    default_bitrate: int = 192    # kbps
    vbr: bool = True              # 是否支持质量档
    rates: tuple = ()             # 允许采样率（空=不限）
    bits: tuple = (16, 24)        # PCM 位深可选
    locked_rate: Optional[int] = None
    desc: str = ""
    tag: str = ""

    def public(self) -> dict:
        d = asdict(self)
        d["rates"] = list(self.rates)
        d["bits"] = list(self.bits)
        return d


MP3_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000)

FORMATS: Dict[str, FormatSpec] = {
    "mp3": FormatSpec(
        "mp3", "MP3", "libmp3lame", "mp3", ".mp3", "lossy",
        bitrate=True, default_bitrate=320, vbr=True, rates=MP3_RATES,
        desc="兼容性最好，有损压缩", tag="通用",
    ),
    "wav": FormatSpec(
        "wav", "WAV", "pcm_s16le", "wav", ".wav", "pcm",
        bitrate=False, vbr=False, rates=(), bits=(16, 24, 32),
        desc="无损 PCM，体积大，适合继续处理", tag="无损",
    ),
    "flac": FormatSpec(
        "flac", "FLAC", "flac", "flac", ".flac", "lossless",
        bitrate=False, vbr=False, rates=(), bits=(16, 24),
        desc="无损压缩，约 WAV 的一半体积", tag="无损",
    ),
    "ogg": FormatSpec(
        "ogg", "OGG Vorbis", "vorbis", "ogg", ".ogg", "lossy",
        bitrate=True, default_bitrate=224, vbr=True, rates=(8000, 11025, 16000, 22050, 32000, 44100, 48000),
        desc="开源有损格式，同码率优于 MP3", tag="通用",
    ),
    "opus": FormatSpec(
        "opus", "Opus", "libopus", "opus", ".opus", "lossy",
        bitrate=True, default_bitrate=160, vbr=True,
        rates=(48000, 24000, 16000, 12000, 8000), locked_rate=48000,
        desc="低码率高音质，网络/语音首选", tag="通用",
    ),
    "m4a": FormatSpec(
        "m4a", "M4A (AAC)", "aac", "ipod", ".m4a", "lossy",
        bitrate=True, default_bitrate=256, vbr=False, rates=(),
        desc="苹果生态常用，AAC 编码", tag="通用",
    ),
    "alac": FormatSpec(
        "alac", "ALAC", "alac", "ipod", ".m4a", "lossless",
        bitrate=False, vbr=False, rates=(), bits=(16, 24),
        desc="苹果无损，存于 m4a 容器", tag="无损",
    ),
    "aiff": FormatSpec(
        "aiff", "AIFF", "pcm_s16be", "aiff", ".aiff", "pcm",
        bitrate=False, vbr=False, rates=(), bits=(16, 24),
        desc="苹果无损 PCM", tag="无损",
    ),
    "wma": FormatSpec(
        "wma", "WMA", "wmav2", "asf", ".wma", "lossy",
        bitrate=True, default_bitrate=192, vbr=False, rates=(8000, 11025, 16000, 22050, 32000, 44100, 48000),
        desc="Windows Media 格式", tag="兼容",
    ),
    "mp2": FormatSpec(
        "mp2", "MP2", "mp2", "mp2", ".mp2", "lossy",
        bitrate=True, default_bitrate=192, vbr=False, rates=(32000, 44100, 48000),
        desc="广播/老设备兼容", tag="兼容",
    ),
    "ac3": FormatSpec(
        "ac3", "AC-3", "ac3", "ac3", ".ac3", "lossy",
        bitrate=True, default_bitrate=192, vbr=False, rates=(32000, 44100, 48000),
        desc="杜比数字，影音容器用", tag="兼容",
    ),
    "caf": FormatSpec(
        "caf", "CAF", "pcm_s16le", "caf", ".caf", "pcm",
        bitrate=False, vbr=False, rates=(), bits=(16, 24),
        desc="Core Audio 容器", tag="兼容",
    ),
}

# 采样率预设（输入侧也用于重采样）
SAMPLE_RATES = [8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000]

# 品质档：影响有损编码的码率/质量与无损的位深
QUALITY_PRESETS = {
    "chip": {"label": "8bit 原生", "bitrate": 128, "rate": 22050, "bits": 16},
    "low": {"label": "省流量", "bitrate": 128, "rate": 44100, "bits": 16},
    "standard": {"label": "标准", "bitrate": 192, "rate": 44100, "bits": 16},
    "high": {"label": "高品质", "bitrate": 320, "rate": 44100, "bits": 24},
    "studio": {"label": "母带级", "bitrate": 512, "rate": 48000, "bits": 24},
}


def get_format(key: str) -> FormatSpec:
    k = (key or "").lower().lstrip(".")
    if k not in FORMATS:
        raise KeyError(f"不支持的输出格式: {key}")
    return FORMATS[k]


def format_of_extension(path: str) -> str:
    ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    for spec in FORMATS.values():
        if spec.ext == ext:
            return spec.key
    if ext == ".m4a":
        return "m4a"
    return "mp3"


def pick_rate(spec: FormatSpec, rate: int) -> int:
    """把目标采样率对齐到编码器允许的档位。"""
    if spec.locked_rate:
        return spec.locked_rate
    if not spec.rates:
        return int(rate)
    if rate in spec.rates:
        return int(rate)
    return int(min(spec.rates, key=lambda r: (abs(r - rate), -r)))


def list_formats() -> List[dict]:
    order = ["mp3", "wav", "flac", "ogg", "opus", "m4a", "alac", "aiff", "wma", "mp2", "ac3", "caf"]
    return [FORMATS[k].public() for k in order if k in FORMATS]


def quality_to_bitrate(spec: FormatSpec, quality: str) -> int:
    preset = QUALITY_PRESETS.get(quality or "standard", QUALITY_PRESETS["standard"])
    return int(preset["bitrate"]) if spec.bitrate else 0
