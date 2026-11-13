# -*- coding: utf-8 -*-
"""AudioForge HTTP 接口自测（Flask test client，不需要起服务）。

    python tests/test_api.py
"""
from __future__ import annotations

import io
import os
import sys
import time
import unittest

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import server  # noqa: E402
from core.audio_io import AudioData, encode  # noqa: E402


def wav_bytes(seconds=2.0, freq=440.0, rate=44100) -> bytes:
    import tempfile
    n = int(rate * seconds)
    t = np.arange(n) / rate
    sig = (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    audio = AudioData(np.stack([sig, sig]), rate)
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    encode(audio, path, "wav")
    with open(path, "rb") as fh:
        blob = fh.read()
    os.remove(path)
    return blob


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.app.config["TESTING"] = True
        cls.client = server.app.test_client()
        cls.blob = wav_bytes()
        r = cls.client.post("/api/upload", data={
            "files": (io.BytesIO(cls.blob), "api_test.wav"),
        }, content_type="multipart/form-data")
        cls.upload = r.get_json()
        cls.path = cls.upload["files"][0]["path"] if cls.upload.get("ok") else None

    @classmethod
    def tearDownClass(cls):
        if cls.path and os.path.exists(cls.path):
            os.remove(cls.path)

    def test_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"AudioForge", r.data)

    def test_static(self):
        for name in ("style.css", "app.js"):
            r = self.client.get("/" + name)
            self.assertEqual(r.status_code, 200)
            self.assertGreater(len(r.data), 500)

    def test_meta(self):
        data = self.client.get("/api/meta").get_json()
        self.assertIn("formats", data)
        self.assertGreaterEqual(len(data["formats"]), 10)
        self.assertIn("presets", data)
        self.assertIn("eq_bands", data)

    def test_upload(self):
        self.assertTrue(self.upload["ok"])
        info = self.upload["files"][0]
        self.assertEqual(info["name"], "api_test.wav")
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.1)

    def test_upload_empty(self):
        r = self.client.post("/api/upload", data={}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)

    def test_probe(self):
        data = self.client.get("/api/probe?path=" + self.path).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["info"]["channels"], 2)

    def test_probe_missing(self):
        r = self.client.get("/api/probe?path=/nope/none.wav")
        self.assertEqual(r.status_code, 404)

    def test_waveform(self):
        data = self.client.post("/api/waveform", json={"path": self.path}).get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["spectrum"].startswith("data:image/png;base64,"))
        self.assertEqual(len(data["wave"]["max"]), len(data["wave"]["min"]))

    def test_chain(self):
        data = self.client.post("/api/chain", json={"spec": {
            "input": self.path, "fmt": "flac", "chip_enabled": True,
            "chip_preset": "gameboy", "normalize": "peak",
        }}).get_json()
        self.assertTrue(data["ok"])
        text = " ".join(data["chain"])
        self.assertIn("8bit", text)
        self.assertIn("FLAC", text)

    def test_preview(self):
        data = self.client.post("/api/preview", json={
            "path": self.path,
            "spec": {"input": self.path, "fmt": "wav", "chip_enabled": True, "chip_preset": "nes"},
            "preview_length": 1.0,
        }).get_json()
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["url"].startswith("/media/"))
        media = self.client.get(data["url"])
        self.assertEqual(media.status_code, 200)
        self.assertTrue(media.headers["Content-Type"].startswith("audio/"))
        media.close()
        target = os.path.join(server.PREVIEW_DIR, os.path.basename(data["url"]))
        if os.path.exists(target):
            os.remove(target)

    def test_compare_image(self):
        data = self.client.post("/api/compare", json={
            "path": self.path,
            "spec": {"input": self.path, "fmt": "wav", "chip_enabled": True},
            "preview_length": 1.0,
        }).get_json()
        self.assertTrue(data["ok"], data)
        self.assertGreater(os.path.getsize(data["file"]), 500)
        os.remove(data["file"])

    def test_convert_flow(self):
        data = self.client.post("/api/convert", json={
            "items": [{"path": self.path, "spec": {
                "input": self.path, "fmt": "mp3", "chip_enabled": True, "chip_preset": "c64",
            }}],
            "suffix": "_api",
        }).get_json()
        self.assertTrue(data["ok"])
        ids = data["ids"]
        self.assertEqual(len(ids), 1)

        status = None
        for _ in range(120):
            jobs = self.client.get("/api/jobs?ids=" + ids[0]).get_json()["jobs"]
            status = jobs[0]["status"]
            if status in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(status, "done", jobs[0].get("error"))
        out = jobs[0]["result"]["output"]
        self.assertTrue(os.path.exists(out))
        self.assertIn("_api", os.path.basename(out))
        os.remove(out)

    def test_convert_no_items(self):
        r = self.client.post("/api/convert", json={"items": []})
        self.assertEqual(r.status_code, 400)

    def test_delete_outside_scope(self):
        r = self.client.post("/api/delete", json={"path": "C:/Windows/system32/drivers/etc/hosts"})
        self.assertEqual(r.status_code, 400)

    def test_media_traversal_blocked(self):
        r = self.client.get("/media/../../server.py")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
