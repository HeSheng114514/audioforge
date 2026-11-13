# -*- coding: utf-8 -*-
"""AudioForge 本地服务：Flask + 后台批处理队列 + 静态前端。"""
from __future__ import annotations

import io
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from flask import Flask, jsonify, request, send_file, send_from_directory, abort

def _resource_root() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


BASE = _resource_root()

# 打包后工作目录（上传/输出）要放在 exe 旁边，而不是临时解包目录
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = BASE
sys.path.insert(0, BASE)

from core.console import enable_utf8  # noqa: E402
from core import audio_io, chip, dsp, formats, pipeline, viz  # noqa: E402
from core import unlock as unlock_mod  # noqa: E402

enable_utf8()
from core.audio_io import AudioData, AudioError, decode, encode, probe  # noqa: E402
from core.pipeline import JobSpec, build_output_path, describe_chain  # noqa: E402
from core.chip import PRESETS, preset_params, render_chip, chip_summary  # noqa: E402

WEB_DIR = os.path.join(BASE, "web")
WORK_DIR = os.path.join(APP_DIR, "workspace")
UPLOAD_DIR = os.path.join(WORK_DIR, "uploads")
OUT_DIR = os.path.join(WORK_DIR, "outputs")
PREVIEW_DIR = os.path.join(WORK_DIR, "previews")
SAMPLE_DIR = os.path.join(BASE, "samples")
for d in (UPLOAD_DIR, OUT_DIR, PREVIEW_DIR):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB


# --------------------------------------------------------------------------- #
# 任务队列
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    spec: JobSpec
    status: str = "pending"      # pending / running / done / error / canceled
    progress: float = 0.0
    message: str = ""
    result: Dict = field(default_factory=dict)
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0


class JobManager:
    def __init__(self, workers: int = 2):
        self.q: "queue.Queue[str]" = queue.Queue()
        self.jobs: Dict[str, Job] = {}
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        for i in range(workers):
            threading.Thread(target=self._worker, name=f"af-worker-{i}", daemon=True).start()

    def submit(self, spec: JobSpec) -> str:
        jid = uuid.uuid4().hex[:12]
        job = Job(id=jid, spec=spec)
        with self.lock:
            self.jobs[jid] = job
        self.q.put(jid)
        return jid

    def get(self, jid: str) -> Optional[Job]:
        return self.jobs.get(jid)

    def snapshot(self, jid: str) -> Optional[dict]:
        job = self.jobs.get(jid)
        if not job:
            return None
        return {
            "id": job.id,
            "status": job.status,
            "progress": round(job.progress, 4),
            "message": job.message,
            "error": job.error,
            "result": job.result,
            "spec": job.spec.public(),
            "created": job.created,
            "elapsed": round((job.finished or time.time()) - (job.started or job.created), 2),
        }

    def all(self) -> List[dict]:
        with self.lock:
            order = sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
        return [self.snapshot(j.id) for j in order[:200]]

    def _worker(self):
        while not self.stop_flag.is_set():
            try:
                jid = self.q.get(timeout=0.4)
            except queue.Empty:
                continue
            job = self.jobs.get(jid)
            if not job or job.status == "canceled":
                continue
            job.status = "running"
            job.started = time.time()

            def prog(p: float, msg: str):
                job.progress = p
                job.message = msg

            try:
                result = pipeline.process(job.spec, progress=prog)
                job.result = {
                    "output": result["output"],
                    "chain": result["chain"],
                    "elapsed": result["elapsed"],
                    "in_info": result["in_info"],
                    "out_info": result["out_info"],
                    "ratio": result["ratio"],
                    "chip": result.get("chip"),
                }
                job.status = "done"
                job.progress = 1.0
                job.message = "完成"
            except Exception as exc:  # noqa: BLE001
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.message = "失败"
                traceback.print_exc()
            finally:
                job.finished = time.time()


JOBS = JobManager(workers=max(2, min(4, (os.cpu_count() or 4) // 2)))


# --------------------------------------------------------------------------- #
# 静态资源
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:fname>")
def static_files(fname: str):
    target = os.path.join(WEB_DIR, fname)
    if os.path.isfile(target):
        return send_from_directory(WEB_DIR, fname)
    abort(404)


@app.route("/media/<path:rel>")
def media(rel: str):
    """播放接口：支持 Range，供 <audio> 拖动。"""
    safe = os.path.normpath(rel).lstrip("\\/")
    for root in (UPLOAD_DIR, OUT_DIR, PREVIEW_DIR):
        candidate = os.path.join(root, os.path.basename(safe))
        if os.path.isfile(candidate):
            resp = send_file(candidate, conditional=True)
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Cache-Control"] = "no-cache"
            return resp
    abort(404)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/meta")
def api_meta():
    return jsonify({
        "formats": formats.list_formats(),
        "sample_rates": formats.SAMPLE_RATES,
        "qualities": [{"key": k, "label": v["label"], "bitrate": v["bitrate"], "rate": v["rate"]}
                      for k, v in formats.QUALITY_PRESETS.items()],
        "presets": [{"key": k, "label": v["label"], "desc": v["desc"], "params": v["params"]}
                    for k, v in PRESETS.items()],
        "eq_bands": dsp.EQ_BANDS,
        "encrypted_formats": [
            {"ext": ext, "kind": meta[0], "platform": meta[1]}
            for ext, meta in sorted(unlock_mod.KINDS.items())
        ],
        "encrypted_exts": list(unlock_mod.SUPPORTED_EXTS),
        "version": "1.0.0",
        "engine": f"PyAV {av_version()}",
    })


def av_version() -> str:
    try:
        import av
        return av.__version__
    except Exception:
        return "?"


@app.get("/api/samples")
def api_samples():
    """列出内置演示音频，供「载入示例」按钮使用。"""
    items = []
    if os.path.isdir(SAMPLE_DIR):
        for name in sorted(os.listdir(SAMPLE_DIR)):
            path = os.path.join(SAMPLE_DIR, name)
            if not os.path.isfile(path) or not audio_io.is_supported(name):
                continue
            try:
                info = probe(path)
            except Exception:  # noqa: BLE001
                continue
            info["ok"] = True
            info["id"] = name
            items.append(info)
    return jsonify({"ok": True, "files": items, "dir": SAMPLE_DIR})


@app.post("/api/use_sample")
def api_use_sample():
    """把内置示例登记为上传文件（复制到 uploads，便于后续处理与播放）。"""
    body = request.get_json(force=True, silent=True) or {}
    name = os.path.basename(body.get("name") or "")
    src = os.path.join(SAMPLE_DIR, name)
    if not name or not os.path.isfile(src):
        return jsonify({"ok": False, "error": "示例不存在"}), 404
    dst = os.path.join(UPLOAD_DIR, name)
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)
    try:
        info = probe(dst)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    info["ok"] = True
    info["path"] = dst
    info["id"] = os.path.basename(dst)
    return jsonify({"ok": True, "file": info})


@app.post("/api/upload")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400
    saved = []
    for f in files:
        name = os.path.basename(f.filename or "audio")
        stem, ext = os.path.splitext(name)
        if not ext:
            ext = ".bin"
        target = os.path.join(UPLOAD_DIR, name)
        i = 2
        while os.path.exists(target):
            target = os.path.join(UPLOAD_DIR, f"{stem} ({i}){ext}")
            i += 1
        f.save(target)
        det = unlock_mod.detect(target)
        if det.encrypted:
            info = {
                "ok": True, "encrypted": True, "platform": det.platform,
                "kind": det.kind, "needs_key": det.needs_key,
                "name": name, "path": target, "size": os.path.getsize(target),
                "duration": 0, "rate": 0, "channels": 0,
                "codec": f"{det.kind}（加密）",
            }
        else:
            try:
                info = probe(target)
                info["ok"] = True
            except Exception as exc:  # noqa: BLE001
                info = {"ok": False, "name": name, "path": target, "error": str(exc)}
        info["path"] = target
        info["id"] = os.path.basename(target)
        saved.append(info)
    return jsonify({"ok": True, "files": saved})


@app.get("/api/probe")
def api_probe():
    path = request.args.get("path", "")
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        return jsonify({"ok": True, "info": probe(path)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/waveform")
def api_waveform():
    """返回波形包络 + 频谱图（base64 PNG）。"""
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path", "")
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        limit = float(body.get("limit") or 0)
        audio = decode(path, target_rate=22050,
                       duration=limit if limit > 0 else None)
        data = viz.wave_data(audio.samples, columns=int(body.get("columns") or 640))
        import base64
        spec_png = viz.spectrum_png(audio.samples, audio.rate, width=900, height=260)
        return jsonify({
            "ok": True, "rate": audio.rate, "duration": audio.duration,
            "wave": data, "spectrum": "data:image/png;base64," + base64.b64encode(spec_png).decode(),
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/preview")
def api_preview():
    """就地渲染一段试听（默认前 12 秒），不写输出目录。"""
    body = request.get_json(force=True, silent=True) or {}
    spec = _spec_from_body(body, output="")
    path = body.get("path") or spec.input or ""
    if not path or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        start = float(body.get("preview_start") or 0.0)
        length = float(body.get("preview_length") or 12.0)
        spec.start = start
        spec.end = start + length
        spec.trim_silence = False
        target = os.path.join(PREVIEW_DIR, f"preview_{uuid.uuid4().hex[:10]}.wav")
        spec.output = target
        spec.fmt = "wav"
        spec.quality = "chip" if spec.chip_enabled else "standard"
        spec.rate = 0
        result = pipeline.process(spec)
        url = f"/media/{os.path.basename(target)}"
        return jsonify({
            "ok": True, "url": url, "file": target,
            "chain": result["chain"], "info": result["out_info"],
            "chip": result.get("chip"),
            "elapsed": result["elapsed"],
        })
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 400


def _spec_from_body(body: dict, output: str = "") -> JobSpec:
    s = body.get("spec") or body
    spec = JobSpec(
        input=s.get("input") or body.get("path") or "",
        output=output,
        fmt=s.get("fmt") or "mp3",
        quality=s.get("quality") or "standard",
        bitrate=int(s.get("bitrate") or 0),
        rate=int(s.get("rate") or 0),
        bits=int(s.get("bits") or 16),
        volume_db=float(s.get("volume_db") or 0.0),
        normalize=s.get("normalize") or "none",
        normalize_db=float(s.get("normalize_db") if s.get("normalize_db") is not None else -1.0),
        fade_in=float(s.get("fade_in") or 0.0),
        fade_out=float(s.get("fade_out") or 0.0),
        start=float(s.get("start") or 0.0),
        end=float(s.get("end") or 0.0),
        speed=float(s.get("speed") or 1.0),
        pitch=float(s.get("pitch") or 0.0),
        channels=s.get("channels") or "keep",
        trim_silence=bool(s.get("trim_silence")),
        reverse=bool(s.get("reverse")),
        eq=[float(x) for x in (s.get("eq") or [])],
        chip_enabled=bool(s.get("chip_enabled")),
        chip_preset=s.get("chip_preset") or "nes",
        chip=dict(s.get("chip") or {}),
        metadata=dict(s.get("metadata") or {}),
        keep_original_tags=bool(s.get("keep_original_tags", True)),
    )
    return spec


@app.post("/api/chain")
def api_chain():
    body = request.get_json(force=True, silent=True) or {}
    try:
        spec = _spec_from_body(body)
        return jsonify({"ok": True, "chain": describe_chain(spec)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/convert")
def api_convert():
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items") or []
    if not items:
        return jsonify({"ok": False, "error": "没有待转换的任务"}), 400
    out_dir = body.get("out_dir") or OUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    suffix = body.get("suffix") or ""
    ids = []
    try:
        for it in items:
            path = it.get("path") or it.get("input")
            if not os.path.isfile(path):
                continue
            spec = _spec_from_body(it)
            spec.input = path
            spec.output = build_output_path(path, out_dir, spec, suffix=suffix)
            ids.append(JOBS.submit(spec))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "ids": ids, "out_dir": out_dir})


@app.get("/api/jobs")
def api_jobs():
    ids = request.args.get("ids", "")
    if ids:
        out = []
        for jid in ids.split(","):
            snap = JOBS.snapshot(jid.strip())
            if snap:
                out.append(snap)
        return jsonify({"ok": True, "jobs": out})
    return jsonify({"ok": True, "jobs": JOBS.all()})


@app.post("/api/cancel")
def api_cancel():
    body = request.get_json(force=True, silent=True) or {}
    jid = body.get("id")
    job = JOBS.get(jid)
    if not job:
        return jsonify({"ok": False, "error": "任务不存在"}), 404
    if job.status == "pending":
        job.status = "canceled"
    return jsonify({"ok": True})


@app.post("/api/export_image")
def api_export_image():
    """导出波形 / 频谱 PNG 到输出目录。"""
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path", "")
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    kind = body.get("kind") or "wave"
    try:
        audio = decode(path, target_rate=22050, duration=float(body.get("limit") or 0) or None)
        if kind == "spectrum":
            blob = viz.spectrum_png(audio.samples, audio.rate)
        else:
            blob = viz.waveform_png(audio.samples, audio.rate)
        name = os.path.splitext(os.path.basename(path))[0] + f"_{kind}.png"
        target = os.path.join(OUT_DIR, name)
        with open(target, "wb") as fh:
            fh.write(blob)
        return jsonify({"ok": True, "file": target, "url": f"/media/{os.path.basename(target)}"})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/compare")
def api_compare():
    """生成「原声 vs 处理后」的波形对比图（PNG），并返回可访问地址。"""
    body = request.get_json(force=True, silent=True) or {}
    spec = _spec_from_body(body, output="")
    path = body.get("path") or spec.input or ""
    if not path or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        start = float(body.get("preview_start") or 0.0)
        length = float(body.get("preview_length") or 12.0)
        src = decode(path, target_rate=22050, start=start, duration=length)

        spec.start = start
        spec.end = start + length
        spec.trim_silence = False
        spec.fmt = "wav"
        spec.rate = 22050
        tmp = os.path.join(PREVIEW_DIR, f"cmp_{uuid.uuid4().hex[:8]}.wav")
        spec.output = tmp
        pipeline.process(spec)
        out = decode(tmp, target_rate=22050)

        blob = viz.compare_png(src.samples, out.samples, width=1200, height=300, rate=src.rate)
        name = f"compare_{uuid.uuid4().hex[:8]}.png"
        target = os.path.join(OUT_DIR, name)
        with open(target, "wb") as fh:
            fh.write(blob)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return jsonify({"ok": True, "url": f"/media/{name}", "file": target})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(exc)}), 400



# --------------------------------------------------------------------------- #
# 私有加密格式解锁
# --------------------------------------------------------------------------- #
@app.post("/api/detect")
def api_detect():
    """判断文件是不是受支持的加密格式。"""
    body = request.get_json(force=True, silent=True) or {}
    path = body.get("path") or ""
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    det = unlock_mod.detect(path)
    return jsonify({
        "ok": True,
        "encrypted": det.encrypted,
        "platform": det.platform,
        "kind": det.kind,
        "note": det.note,
        "needs_key": det.needs_key,
    })


@app.post("/api/unlock")
def api_unlock():
    """解密一个或多个私有加密文件，输出到 outputs 目录。"""
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items") or []
    ekey = body.get("ekey") or ""
    ekey_blob = ekey.encode("utf-8", "ignore") if ekey else None
    results = []
    for it in items:
        path = it.get("path") if isinstance(it, dict) else it
        if not path or not os.path.isfile(path):
            results.append({"ok": False, "input": path, "error": "文件不存在"})
            continue
        det = unlock_mod.detect(path)
        if not det.encrypted:
            results.append({"ok": False, "input": path,
                            "error": "不是受支持的加密格式"})
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        # 先解到临时名（还不知道真实格式），再按嗅探结果改名
        tmp = os.path.join(OUT_DIR, f".{uuid.uuid4().hex[:8]}.unlock")
        try:
            res = unlock_mod.unlock(path, tmp, ekey=ekey_blob)
            kind = res.kind or "audio"
            target = os.path.join(OUT_DIR, f"{stem}.{kind}")
            i = 2
            while os.path.exists(target):
                target = os.path.join(OUT_DIR, f"{stem} ({i}).{kind}")
                i += 1
            os.replace(tmp, target)
            info = probe(target)
            results.append({
                "ok": True,
                "input": path,
                "output": target,
                "name": os.path.basename(target),
                "kind": kind,
                "platform": det.platform,
                "size": info["size"],
                "duration": info["duration"],
                "rate": info["rate"],
                "channels": info["channels"],
                "codec": info["codec"],
                "tags": res.meta,
                "note": res.note,
            })
        except Exception as exc:  # noqa: BLE001
            try:
                os.remove(tmp)
            except OSError:
                pass
            results.append({"ok": False, "input": path,
                            "error": str(exc), "kind": det.kind,
                            "platform": det.platform})
    ok = sum(1 for r in results if r.get("ok"))
    return jsonify({"ok": True, "results": results, "succeeded": ok,
                    "failed": len(results) - ok})


@app.post("/api/open_folder")
def api_open_folder():
    body = request.get_json(force=True, silent=True) or {}
    target = body.get("path") or OUT_DIR
    try:
        os.startfile(os.path.abspath(target))  # type: ignore[attr-defined]
        return jsonify({"ok": True})
    except Exception:  # noqa: BLE001
        return jsonify({"ok": False, "error": "无法打开文件夹"})


@app.post("/api/delete")
def api_delete():
    body = request.get_json(force=True, silent=True) or {}
    target = body.get("path") or ""
    roots = (UPLOAD_DIR, OUT_DIR, PREVIEW_DIR)
    try:
        real = os.path.abspath(target)
        if any(real.startswith(os.path.abspath(r) + os.sep) for r in roots) and os.path.isfile(real):
            os.remove(real)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "路径不在可删除范围"}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"ok": False, "error": "文件超过 2GB 上限"}), 413


def main():
    import argparse
    p = argparse.ArgumentParser(description="AudioForge 本地服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    print("=" * 62)
    print("  音匠 AudioForge · 音乐转格式 / 8bit 芯片化")
    print(f"  打开浏览器访问： http://{args.host}:{args.port}")
    print(f"  输出目录： {OUT_DIR}")
    print("=" * 62)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
