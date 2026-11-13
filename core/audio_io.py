# -*- coding: utf-8 -*-
"""音频读写层：PyAV 解码 / 编码 / 探测。

设计目标
--------
1. 任何输入格式统一解码成 float32 平面数组 (channels, samples)；
2. 编码时自动协商编码器支持的 sample_format / 采样率 / 声道布局，避免
   "avcodec_open2(...) returned 22" 这类参数不匹配；
3. 进度回调用于 Web / CLI 的实时进度条。
"""
from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import av
import numpy as np

from .formats import FormatSpec, get_format, pick_rate

Progress = Optional[Callable[[float, str], None]]


class AudioError(RuntimeError):
    pass


@dataclass
class AudioData:
    """统一的音频内存表示。samples: float32, shape=(channels, n)"""
    samples: np.ndarray
    rate: int
    channels: int = 0

    def __post_init__(self):
        if self.samples.ndim == 1:
            self.samples = self.samples[None, :]
        self.samples = np.ascontiguousarray(self.samples, dtype=np.float32)
        self.channels = int(self.samples.shape[0])

    @property
    def frames(self) -> int:
        return int(self.samples.shape[1])

    @property
    def duration(self) -> float:
        return self.frames / float(self.rate) if self.rate else 0.0

    def copy(self) -> "AudioData":
        return AudioData(self.samples.copy(), self.rate)

    def mono(self) -> np.ndarray:
        if self.channels == 1:
            return self.samples[0]
        return self.samples.mean(axis=0)

    def to_stereo(self) -> "AudioData":
        if self.channels == 2:
            return self
        if self.channels == 1:
            return AudioData(np.stack([self.samples[0], self.samples[0]]), self.rate)
        # 多于 2 声道：前 L/R + 中置折叠
        left = self.samples[0]
        right = self.samples[1] if self.channels > 1 else left
        rest = self.samples[2:].mean(axis=0) * 0.5 if self.channels > 2 else 0.0
        return AudioData(np.stack([left + rest, right + rest]), self.rate)

    def slice(self, start: float, end: Optional[float] = None) -> "AudioData":
        a = max(0, int(start * self.rate))
        b = self.frames if end is None else min(self.frames, int(end * self.rate))
        if b <= a:
            raise AudioError("裁剪区间为空")
        return AudioData(self.samples[:, a:b], self.rate)


# --------------------------------------------------------------------------- #
# Ogg 时长修正
# --------------------------------------------------------------------------- #
_OGG_CRC_TABLE = None


def _ogg_crc_table():
    global _OGG_CRC_TABLE
    if _OGG_CRC_TABLE is None:
        table = []
        for i in range(256):
            r = i << 24
            for _ in range(8):
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if (r & 0x80000000) else (r << 1) & 0xFFFFFFFF
            table.append(r)
        _OGG_CRC_TABLE = table
    return _OGG_CRC_TABLE


def _ogg_crc(buf):
    table = _ogg_crc_table()
    crc = 0
    for b in buf:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ b]
    return crc


def fix_ogg_granule(path, total_samples):
    """修正 Ogg 末页 granulepos。

    本机 ffmpeg 的 vorbis 编码器不回收末页 granule，导致容器头里时长显示为
    真实时长的 1/16 左右。这里重写末页 granule 并重算 CRC，播放器即可读到
    正确时长（音频数据本身没有变化）。
    """
    try:
        with open(path, "rb") as fh:
            data = bytearray(fh.read())
    except Exception:
        return False
    if len(data) < 27 or data[:4] != b"OggS":
        return False
    off = 0
    last = None
    while off + 27 <= len(data) and data[off:off + 4] == b"OggS":
        nseg = data[off + 26]
        if off + 27 + nseg > len(data):
            break
        body = sum(data[off + 27: off + 27 + nseg])
        page_len = 27 + nseg + body
        granule = int.from_bytes(data[off + 6: off + 14], "little", signed=True)
        if granule >= 0:
            last = (off, page_len)
        off += page_len
    if not last:
        return False
    start, plen = last
    cur = int.from_bytes(data[start + 6: start + 14], "little", signed=True)
    if cur == total_samples:
        return True
    data[start + 6: start + 14] = int(total_samples).to_bytes(8, "little", signed=True)
    data[start + 22: start + 26] = b"\x00\x00\x00\x00"
    crc = _ogg_crc(bytes(data[start: start + plen]))
    data[start + 22: start + 26] = crc.to_bytes(4, "little")
    tmp = path + ".oggfix"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return True


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def probe(path: str) -> Dict:
    if not os.path.exists(path):
        raise AudioError(f"文件不存在: {path}")
    try:
        with av.open(path) as container:
            if not container.streams.audio:
                raise AudioError("该文件不包含音频流")
            stream = container.streams.audio[0]
            ctx = stream.codec_context
            duration = 0.0
            if stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            elif container.duration:
                duration = float(container.duration) / 1_000_000.0
            tags = {}
            for src in (container.metadata or {}, stream.metadata or {}):
                for k, v in src.items():
                    if v and k.lower() not in ("encoder", "handler_name", "vendor_id"):
                        tags.setdefault(k.lower(), str(v))
            bitrate = int(ctx.bit_rate or 0) or int(getattr(stream, "bit_rate", 0) or 0)
            info = {
                "path": path,
                "name": os.path.basename(path),
                "format": (container.format.name or "").split(",")[0],
                "codec": ctx.name,
                "rate": int(ctx.sample_rate or 0),
                "channels": int(ctx.channels or 0),
                "layout": str(ctx.layout.name) if ctx.layout else "",
                "bitrate": bitrate,
                "duration": duration,
                "size": os.path.getsize(path),
                "tags": tags,
            }
            if ctx.format is not None:
                info["sample_format"] = ctx.format.name
                depth = {"u8": 8, "s16": 16, "s16p": 16, "s32": 24, "s32p": 24, "flt": 32, "fltp": 32}.get(
                    ctx.format.name, 0)
                info["bit_depth"] = depth
            return info
    except AudioError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AudioError(f"无法解析音频文件: {exc}") from exc


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
def decode(path: str, *, target_rate: Optional[int] = None, mono: bool = False,
           start: float = 0.0, duration: Optional[float] = None,
           progress: Progress = None) -> AudioData:
    """把任意音频解码为 float32 (channels, samples)。

    注意：部分容器（Vorbis/OGG）解码出的 frame.time 带偏移甚至为负，因此
    这里不依赖时间戳裁剪，而是按样本计数跳过与截断。
    """
    if not os.path.exists(path):
        raise AudioError(f"文件不存在: {path}")

    with av.open(path) as container:
        if not container.streams.audio:
            raise AudioError("该文件不包含音频流")
        stream = container.streams.audio[0]
        src_rate = int(stream.codec_context.sample_rate or 44100)
        out_rate = int(target_rate or src_rate)
        layout = "mono" if mono else "stereo"
        resampler = av.AudioResampler(format="fltp", layout=layout, rate=out_rate)

        total = 0.0
        if container.duration:
            total = float(container.duration) / 1_000_000.0
        elif stream.duration and stream.time_base:
            total = float(stream.duration * stream.time_base)

        skip = 0
        if start > 0.02:
            skip = int(start * out_rate)
            if start > 0.5:
                try:
                    container.seek(int((start - 0.2) / (stream.time_base or 1.0)), stream=stream)
                    skip = int(0.2 * out_rate)
                except Exception:
                    pass

        limit = int(duration * out_rate) if duration else None
        chunks: List[np.ndarray] = []
        got = 0
        emitted = 0
        for frame in container.decode(stream):
            if frame.samples == 0:
                continue
            for out in resampler.resample(frame):
                arr = out.to_ndarray()
                got += arr.shape[1]
                if skip > 0:
                    if arr.shape[1] <= skip:
                        skip -= arr.shape[1]
                        continue
                    arr = arr[:, skip:]
                    skip = 0
                if limit is not None:
                    room = limit - emitted
                    if room <= 0:
                        break
                    if arr.shape[1] > room:
                        arr = arr[:, :room]
                chunks.append(arr)
                emitted += arr.shape[1]
            if limit is not None and emitted >= limit:
                break
            if progress and total > 0:
                progress(min(1.0, max(0.0, got / out_rate / total)), "解码中")
        if limit is None or emitted < limit:
            for out in resampler.resample(None):
                arr = out.to_ndarray()
                if skip > 0:
                    if arr.shape[1] <= skip:
                        skip -= arr.shape[1]
                        continue
                    arr = arr[:, skip:]
                    skip = 0
                if limit is not None:
                    room = limit - emitted
                    if room <= 0:
                        break
                    arr = arr[:, :room]
                chunks.append(arr)
                emitted += arr.shape[1]

    if not chunks:
        raise AudioError("解码结果为空，文件可能损坏")

    data = np.concatenate(chunks, axis=1)
    if progress:
        progress(1.0, "解码完成")
    return AudioData(data, out_rate)


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #
def _encoder_stream(out, spec: FormatSpec, rate: int, channels: int, bitrate: int,
                    bits: int):
    """按编码器能力协商有效参数。返回 (stream, sample_format, layout, rate)。"""
    codec_name = spec.codec
    if spec.key == "wav" or spec.key == "caf" or spec.key == "aiff":
        if spec.key == "wav":
            codec_name = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_f32le"}.get(bits, "pcm_s16le")
        elif spec.key == "aiff":
            codec_name = {16: "pcm_s16be", 24: "pcm_s24be", 32: "pcm_f32be"}.get(bits, "pcm_s16be")
        else:
            codec_name = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_f32le"}.get(bits, "pcm_s16le")
    if spec.key == "flac":
        codec_name = "flac"
    if spec.key == "alac":
        codec_name = "alac"

    rate = pick_rate(spec, rate)
    layout = "mono" if channels == 1 else "stereo"

    try:
        codec = av.codec.Codec(codec_name, "w")
    except Exception as exc:  # noqa: BLE001
        raise AudioError(f"本机 ffmpeg 不支持编码器 {codec_name}: {exc}") from exc

    stream = out.add_stream(codec_name, rate=rate)
    ctx = stream.codec_context

    # 采样率对齐编码器支持列表
    supported_rates = list(getattr(codec, "audio_rates", []) or [])
    if supported_rates and rate not in supported_rates:
        rate = min(supported_rates, key=lambda r: (abs(r - rate), -r))
        stream.rate = rate

    # 采样格式
    fmts = [f.name for f in (getattr(codec, "audio_formats", None) or [])]
    if fmts:
        for want in ("fltp", "s16p", "s32p", "s16", "flt", "s32", "u8", "dblpd", "dblp"):
            if want in fmts:
                ctx.format = want
                break
        else:
            ctx.format = fmts[0]

    ctx.layout = layout

    if spec.key == "ogg" or codec_name == "vorbis":
        try:
            ctx.options = {"strict": "-2"}
        except Exception:
            pass
    if spec.bitrate and bitrate:
        if spec.key == "opus":
            ctx.bit_rate = int(bitrate * 1000)
            try:
                ctx.options = {"vbr": "on", "compression_level": "10"}
            except Exception:
                pass
        else:
            ctx.bit_rate = int(bitrate * 1000)

    return stream, ctx.format.name if ctx.format is not None else "fltp", layout, int(rate)


def encode(audio: AudioData, path: str, fmt: str = "mp3", *, bitrate: int = 0,
           bits: int = 16, metadata: Optional[Dict[str, str]] = None,
           progress: Progress = None, quality: str = "standard") -> str:
    """把 AudioData 编码写入 path。返回实际输出路径。"""
    spec = get_format(fmt)
    rate = pick_rate(spec, audio.rate)
    channels = 1 if audio.channels == 1 else 2
    data = audio.samples
    if channels == 1 and audio.channels > 1:
        data = data.mean(axis=0, keepdims=True)
    elif channels == 2 and audio.channels == 1:
        data = np.repeat(data, 2, axis=0)
    elif channels == 2 and audio.channels > 2:
        data = audio.to_stereo().samples

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".part"
    br = int(bitrate or spec.default_bitrate)

    out = av.open(tmp, "w", format=spec.muxer)
    try:
        stream, sample_fmt, layout, rate = _encoder_stream(out, spec, rate, channels, br, bits)
        resampler = av.AudioResampler(format=sample_fmt, layout=layout, rate=rate)

        frame_size = 4096 if channels == 2 else 4096
        total = data.shape[1]
        pos = 0
        while pos < total:
            block = np.ascontiguousarray(data[:, pos:pos + frame_size])
            pos += frame_size
            if block.shape[1] == 0:
                continue
            frame = av.AudioFrame.from_ndarray(block, format="fltp", layout=layout)
            frame.sample_rate = audio.rate
            for out_frame in resampler.resample(frame):
                for packet in stream.encode(out_frame):
                    out.mux(packet)
            if progress and total:
                progress(min(1.0, pos / total) * 0.95, "编码中")
        for out_frame in resampler.resample(None):
            for packet in stream.encode(out_frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)

        if metadata:
            clean = {str(k): str(v) for k, v in metadata.items() if v}
            try:
                out.metadata.update(clean)
            except Exception:
                pass
    finally:
        out.close()

    # 用 mutagen 补写标准标签（ffmpeg 对 mp3/m4a 的标签写入不完整）
    if metadata:
        try:
            from .tags import write_tags
            write_tags(tmp, metadata)
        except Exception:
            pass

    if spec.codec == "vorbis":
        try:
            total_out = int(round(data.shape[1] * rate / float(audio.rate)))
            fix_ogg_granule(tmp, total_out)
        except Exception:
            pass

    os.replace(tmp, path)
    if progress:
        progress(1.0, "编码完成")
    return path


def convert_file(src: str, dst: str, fmt: str = "mp3", *, bitrate: int = 0,
                 rate: Optional[int] = None, bits: int = 16,
                 progress: Progress = None) -> str:
    """纯转码（不改变内容），用于格式转换。"""
    info = probe(src)
    audio = decode(src, target_rate=rate or info.get("rate") or 44100, progress=progress)
    meta = {k: v for k, v in info.get("tags", {}).items() if k in
            ("title", "artist", "album", "album_artist", "date", "genre", "track", "comment")}
    return encode(audio, dst, fmt, bitrate=bitrate, bits=bits, metadata=meta, progress=progress)


SUPPORTED_INPUT = ("mp3", "wav", "flac", "ogg", "oga", "opus", "m4a", "aac", "wma", "aiff", "aif",
                   "alac", "mp2", "ac3", "caf", "mka", "wv", "ape", "amr", "m4b", "webm", "mp4",
                   "mkv", "ts", "au", "snd")


def is_supported(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return ext in SUPPORTED_INPUT
