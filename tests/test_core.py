# -*- coding: utf-8 -*-
"""AudioForge 核心自测。

    python -m unittest discover -s tests -v
    或： python tests/test_core.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core import dsp, formats, viz  # noqa: E402
from core.audio_io import AudioData, AudioError, decode, encode, probe  # noqa: E402
from core.chip import ChipParams, PRESETS, preset_params, quantize, render_chip  # noqa: E402
from core.pipeline import JobSpec, build_output_path, describe_chain, process  # noqa: E402

RATE = 44100


def tone(freq=440.0, seconds=2.0, rate=RATE, amp=0.5, channels=2) -> AudioData:
    n = int(rate * seconds)
    t = np.arange(n) / rate
    sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    data = np.stack([sig] * channels) if channels > 1 else sig[None, :]
    return AudioData(data, rate)


class FormatTests(unittest.TestCase):
    def test_registry(self):
        keys = {f["key"] for f in formats.list_formats()}
        for k in ("mp3", "wav", "flac", "ogg", "opus", "m4a", "alac", "aiff"):
            self.assertIn(k, keys)

    def test_rate_alignment(self):
        spec = formats.get_format("mp3")
        self.assertEqual(formats.pick_rate(spec, 44100), 44100)
        # 44101 不在 MP3 支持表里，应回落到最近的合法值
        self.assertIn(formats.pick_rate(spec, 44101), spec.rates)
        opus = formats.get_format("opus")
        self.assertEqual(formats.pick_rate(opus, 44100), 48000)

    def test_unknown_format(self):
        with self.assertRaises(KeyError):
            formats.get_format("xyz")


class IoRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_io_")
        self.audio = tone(440.0, 2.0)

    def tearDown(self):
        for name in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def _roundtrip(self, fmt):
        path = os.path.join(self.tmp, "t_" + fmt + formats.get_format(fmt).ext)
        encode(self.audio, path, fmt, bitrate=192)
        self.assertTrue(os.path.exists(path))
        info = probe(path)
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.15)
        self.assertGreater(info["size"], 1000)
        back = decode(path)
        self.assertAlmostEqual(back.duration, 2.0, delta=0.15)
        self.assertEqual(back.channels, 2)
        return path

    def test_all_lossy_formats(self):
        for fmt in ("mp3", "ogg", "opus", "m4a", "wma", "mp2", "ac3"):
            with self.subTest(fmt=fmt):
                self._roundtrip(fmt)

    def test_all_lossless_formats(self):
        for fmt in ("wav", "flac", "alac", "aiff", "caf"):
            with self.subTest(fmt=fmt):
                self._roundtrip(fmt)

    def test_metadata_survives(self):
        path = os.path.join(self.tmp, "tag.mp3")
        encode(self.audio, path, "mp3", metadata={"title": "测试曲", "artist": "音匠"})
        tags = probe(path)["tags"]
        self.assertEqual(tags.get("title"), "测试曲")

    def test_missing_file(self):
        with self.assertRaises(AudioError):
            probe(os.path.join(self.tmp, "nope.mp3"))

    def test_partial_decode(self):
        path = self._roundtrip("wav")
        seg = decode(path, start=0.5, duration=1.0)
        self.assertAlmostEqual(seg.duration, 1.0, delta=0.05)

    def test_mono_downmix(self):
        path = self._roundtrip("flac")
        mono = decode(path, mono=True)
        self.assertEqual(mono.channels, 1)


class DspTests(unittest.TestCase):
    def test_gain(self):
        a = tone(amp=0.25)
        b = dsp.apply_gain(a, 6.0)
        self.assertAlmostEqual(float(np.abs(b.samples).max()) / float(np.abs(a.samples).max()),
                               1.995, places=2)

    def test_normalize_peak(self):
        a = tone(amp=0.05)
        b = dsp.normalize(a, target_db=-3.0, mode="peak")
        self.assertAlmostEqual(float(np.abs(b.samples).max()), 10 ** (-3 / 20), places=2)

    def test_fade(self):
        a = tone(seconds=2.0, amp=0.8)
        b = dsp.fade(a, fade_in=0.5, fade_out=0.5)
        self.assertLess(float(np.abs(b.samples[:, :100]).max()), 0.05)
        self.assertLess(float(np.abs(b.samples[:, -100:]).max()), 0.05)
        mid = b.samples[:, b.frames // 2 - 2000: b.frames // 2 + 2000]
        self.assertGreater(float(np.abs(mid).max()), 0.5)

    def test_speed_duration(self):
        a = tone(seconds=4.0)
        for speed in (0.5, 0.8, 1.5, 2.0):
            b = dsp.change_speed(a, speed)
            self.assertAlmostEqual(b.duration, 4.0 / speed, delta=0.15)

    def test_pitch_accuracy(self):
        a = tone(freq=1000.0, seconds=3.0, channels=1, amp=0.4)

        def peak_freq(x: AudioData) -> float:
            seg = x.samples[0][RATE:RATE * 2]
            win = seg * np.hanning(seg.size)
            spec = np.abs(np.fft.rfft(win, 1 << 18))
            freqs = np.fft.rfftfreq(1 << 18, 1 / x.rate)
            return float(freqs[np.argmax(spec)])

        for semis in (-12, -7, 5, 12):
            with self.subTest(semis=semis):
                b = dsp.change_pitch(a, semis)
                self.assertAlmostEqual(b.duration, a.duration, delta=0.05)
                expect = 1000.0 * 2 ** (semis / 12.0)
                self.assertAlmostEqual(peak_freq(b), expect, delta=expect * 0.02)

    def test_trim_silence(self):
        a = tone(seconds=1.0, amp=0.6)
        pad = AudioData(np.concatenate([np.zeros((2, RATE)), a.samples,
                                        np.zeros((2, RATE))], axis=1), RATE)
        b = dsp.trim_silence(pad, threshold_db=-40)
        self.assertLess(b.duration, 1.3)

    def test_eq_unity(self):
        a = tone(seconds=1.0)
        b = dsp.equalize(a, [0, 0, 0, 0, 0, 0, 0, 0, 0, 0])
        self.assertLess(float(np.abs(a.samples - b.samples).max()), 1e-6)

    def test_eq_low_boost(self):
        a = tone(freq=80.0, seconds=2.0, amp=0.2)
        b = dsp.equalize(a, [9, 6, 0, 0, 0, 0, 0, 0, 0, 0])
        base = float(np.sqrt(np.mean(a.samples[:, RATE:] ** 2)))
        lifted = float(np.sqrt(np.mean(b.samples[:, RATE:] ** 2)))
        self.assertGreater(lifted / base, 1.5)

    def test_limiter_ceiling(self):
        a = tone(seconds=1.0, amp=1.4)
        b = dsp.limiter(a, ceiling_db=-1.0)
        self.assertLessEqual(float(np.abs(b.samples).max()), 1.0)

    def test_resample(self):
        a = tone(seconds=1.0)
        b = dsp.resample(a, 22050)
        self.assertEqual(b.rate, 22050)
        self.assertAlmostEqual(b.duration, 1.0, delta=0.02)

    def test_channels(self):
        a = tone(channels=2)
        self.assertEqual(dsp.set_channels(a, "mono").channels, 1)
        self.assertEqual(dsp.set_channels(tone(channels=1), "stereo").channels, 2)
        s = dsp.set_channels(a, "swap")
        self.assertTrue(np.allclose(s.samples[0], a.samples[1]))


class ChipTests(unittest.TestCase):
    def test_quantize_levels(self):
        rng = np.random.default_rng(0)
        x = rng.uniform(-1, 1, (1, 20000)).astype(np.float32)
        q = quantize(x, 4, "none")
        levels = np.unique(np.round(q * 7.5 + 7.5))
        self.assertLessEqual(len(levels), 16)
        self.assertGreaterEqual(len(levels), 14)

    def test_presets_valid(self):
        for key in PRESETS:
            with self.subTest(preset=key):
                p = preset_params(key, None)
                self.assertIsInstance(p, ChipParams)
                self.assertTrue(1 <= p.bit_depth <= 16)

    def test_render_preserves_length(self):
        a = tone(seconds=2.0)
        for key in ("nes", "gameboy", "c64", "arcade", "pixel", "tape", "soft"):
            with self.subTest(preset=key):
                out = render_chip(a, preset_params(key))
                self.assertAlmostEqual(out.duration, a.duration, delta=0.05)
                self.assertLessEqual(float(np.abs(out.samples).max()), 1.05)

    def test_bit_depth_reduces_entropy(self):
        a = tone(seconds=1.0, amp=0.9, channels=1)
        deep = render_chip(a, ChipParams(bit_depth=12, sample_rate=44100, highpass=0,
                                         auto_trim_bass=False, lowpass=0))
        shallow = render_chip(a, ChipParams(bit_depth=3, sample_rate=8000, highpass=0,
                                            auto_trim_bass=False, lowpass=0))
        def uniq(x):
            return len(np.unique(np.round(x.samples[0] * 512).astype(int)))
        self.assertLess(uniq(shallow), uniq(deep))

    def test_zero_params_is_transparent(self):
        a = tone(seconds=1.0, amp=0.5, channels=1)
        p = ChipParams(bit_depth=16, sample_rate=44100, squareness=0.0, gate=0.0,
                       vibrato_semitones=0.0, arp_semitones=0.0, ring_mix=0.0,
                       noise=0.0, lowpass=20000, highpass=0, stereo="keep",
                       drive=0.0, tape_stop=0.0, dither="none", volume_db=-1.0,
                       auto_trim_bass=False)
        out = render_chip(a, p)
        corr = float(np.corrcoef(a.samples[0], out.samples[0])[0, 1])
        self.assertGreater(corr, 0.98)


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="af_pipe_")
        cls.src = os.path.join(cls.tmp, "src.wav")
        encode(tone(seconds=3.0), cls.src, "wav")

    @classmethod
    def tearDownClass(cls):
        for name in os.listdir(cls.tmp):
            try:
                os.remove(os.path.join(cls.tmp, name))
            except OSError:
                pass
        os.rmdir(cls.tmp)

    def test_convert(self):
        out = os.path.join(self.tmp, "out.mp3")
        r = process(JobSpec(input=self.src, output=out, fmt="mp3"))
        self.assertTrue(r["ok"])
        self.assertAlmostEqual(r["out_info"]["duration"], 3.0, delta=0.15)

    def test_chip_chain(self):
        out = os.path.join(self.tmp, "chip.wav")
        r = process(JobSpec(input=self.src, output=out, fmt="wav",
                            chip_enabled=True, chip_preset="nes"))
        self.assertIn("8bit", "".join(r["chain"]))
        self.assertLessEqual(r["out_info"]["channels"], 2)

    def test_crop_and_fade(self):
        out = os.path.join(self.tmp, "cut.wav")
        r = process(JobSpec(input=self.src, output=out, fmt="wav", start=0.5, end=2.0,
                            fade_in=0.2, fade_out=0.2))
        self.assertAlmostEqual(r["out_info"]["duration"], 1.5, delta=0.1)

    def test_speed(self):
        out = os.path.join(self.tmp, "fast.wav")
        r = process(JobSpec(input=self.src, output=out, fmt="wav", speed=2.0))
        self.assertAlmostEqual(r["out_info"]["duration"], 1.5, delta=0.2)

    def test_output_naming(self):
        spec = JobSpec(input=self.src, output="", fmt="mp3")
        p1 = build_output_path(self.src, self.tmp, spec, suffix="_x")
        open(p1, "wb").close()
        p2 = build_output_path(self.src, self.tmp, spec, suffix="_x")
        self.assertNotEqual(p1, p2)
        self.assertIn("(2)", os.path.basename(p2))
        os.remove(p1)

    def test_chain_description(self):
        chain = describe_chain(JobSpec(input=self.src, chip_enabled=True,
                                       chip_preset="gameboy", normalize="peak",
                                       speed=1.2, pitch=2, fmt="flac"))
        text = " | ".join(chain)
        self.assertIn("8bit", text)
        self.assertIn("FLAC", text)

    def test_missing_input(self):
        with self.assertRaises(AudioError):
            process(JobSpec(input=os.path.join(self.tmp, "no.wav"),
                            output=os.path.join(self.tmp, "x.mp3"), fmt="mp3"))


class VizTests(unittest.TestCase):
    def test_png_headers(self):
        a = tone(seconds=1.0)
        for blob in (viz.waveform_png(a.samples, a.rate),
                     viz.spectrum_png(a.samples, a.rate),
                     viz.compare_png(a.samples, a.samples)):
            self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
            self.assertGreater(len(blob), 500)

    def test_wave_data(self):
        a = tone(seconds=1.0)
        w = viz.wave_data(a.samples, columns=128)
        self.assertEqual(len(w["max"]), 128)
        self.assertEqual(len(w["min"]), 128)
        self.assertLessEqual(max(w["max"]), 1.0001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
