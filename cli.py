# -*- coding: utf-8 -*-
"""音匠 AudioForge 命令行工具。

示例
----
  python cli.py list
  python cli.py info samples/demo_song.mp3
  python cli.py convert samples/demo_song.mp3 -f flac -o out/
  python cli.py chip samples/demo_song.mp3 --preset nes --fmt mp3 -o out/
  python cli.py chip samples/*.mp3 --preset gameboy --fmt wav --bit-depth 3 --rate 8000 -o out/
  python cli.py wave samples/demo_song.mp3 -o out/wave.png
  python cli.py serve
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.console import enable_utf8  # noqa: E402

enable_utf8()

from core import audio_io, chip as chip_mod, dsp, formats, pipeline, viz  # noqa: E402
from core import unlock as unlock_mod  # noqa: E402
from core.audio_io import AudioError, decode  # noqa: E402
from core.pipeline import JobSpec, build_output_path  # noqa: E402

BAR_W = 34


_last = {"p": -1.0, "t": 0.0, "msg": ""}


def _bar(p: float, msg: str = "") -> None:
    import time as _t
    now = _t.time()
    same_msg = msg == _last["msg"]
    if p < 1.0:
        if same_msg and abs(p - _last["p"]) < 0.02 and now - _last["t"] < 0.25:
            return
        if now - _last["t"] < 0.06:
            return
    _last["p"], _last["t"], _last["msg"] = p, now, msg
    n = int(max(0.0, min(1.0, p)) * BAR_W)
    bar = "█" * n + "░" * (BAR_W - n)
    sys.stdout.write(f"\r  [{bar}] {p * 100:5.1f}%  {msg[:24]:<24}")
    sys.stdout.flush()


def _expand(patterns) -> list:
    out = []
    for p in patterns:
        if os.path.isfile(p):
            out.append(p)
        else:
            hits = sorted(glob.glob(p))
            out.extend([h for h in hits if os.path.isfile(h)])
    return out


def _fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def cmd_list(_args) -> int:
    print("\n  支持的输出格式\n  " + "-" * 58)
    for f in formats.list_formats():
        kind = {"lossy": "有损", "lossless": "无损", "pcm": "PCM"}[f["kind"]]
        print(f"  {f['key']:<6} {f['label']:<12} {kind:<4} {f['desc']}")
    print("\n  可解密的私有格式\n  " + "-" * 58)
    seen = set()
    for ext, (kind, platform) in sorted(unlock_mod.KINDS.items()):
        if kind in seen:
            continue
        seen.add(kind)
        print(f"  {kind:<9} {platform:<12} {ext} 等")
    print("\n  8bit 预设\n  " + "-" * 58)
    for k, v in chip_mod.PRESETS.items():
        print(f"  {k:<9} {v['label']:<12} {v['desc']}")
    print()
    return 0


def cmd_info(args) -> int:
    files = _expand(args.input)
    if not files:
        print("没有匹配的文件")
        return 1
    for path in files:
        try:
            info = audio_io.probe(path)
        except AudioError as exc:
            print(f"  ✕ {path}: {exc}")
            continue
        print(f"\n  {info['name']}")
        print("  " + "-" * 58)
        rows = [
            ("容器", info["format"]), ("编码", info["codec"]),
            ("采样率", f"{info['rate']} Hz"), ("声道", str(info["channels"])),
            ("时长", f"{info['duration']:.2f} s"),
            ("码率", f"{info['bitrate'] // 1000} kbps" if info["bitrate"] else "—"),
            ("大小", _fmt_size(info["size"])),
            ("位深", f"{info.get('bit_depth', '?')} bit"),
            ("采样格式", info.get("sample_format", "?")),
        ]
        for k, v in rows:
            print(f"  {k:<8}{v}")
        tags = info.get("tags") or {}
        if tags:
            print("  标签")
            for k in ("title", "artist", "album", "date", "genre"):
                if k in tags:
                    print(f"    {k:<8}{tags[k]}")
    print()
    return 0


def _build_spec(args, src: str) -> JobSpec:
    out_dir = args.out or "out"
    spec = JobSpec(
        input=src,
        fmt=args.fmt,
        quality=args.quality,
        bitrate=args.bitrate or 0,
        rate=args.rate or 0,
        bits=args.bits,
        volume_db=args.volume,
        normalize=args.normalize,
        fade_in=args.fade_in,
        fade_out=args.fade_out,
        start=args.start,
        end=args.end,
        speed=args.speed,
        pitch=args.pitch,
        channels=args.channels,
        trim_silence=args.trim_silence,
        reverse=args.reverse,
        chip_enabled=args.chip,
        chip_preset=args.preset,
        chip=dict(
            bit_depth=args.bit_depth,
            sample_rate=args.chip_rate,
            squareness=args.squareness,
            gate=args.gate,
            vibrato_semitones=args.vibrato,
            arp_semitones=args.arp,
            noise=args.noise,
            lowpass=args.lowpass,
            stereo=args.chip_stereo,
            tape_stop=args.tape_stop,
            dither=args.dither,
        ) if args.chip else {},
    )
    if args.output and len(_expand(args.input)) == 1:
        spec.output = args.output
    else:
        spec.output = build_output_path(src, out_dir, spec, suffix=args.suffix)
    return spec


def _run_batch(args, force_chip: bool) -> int:
    files = _expand(args.input)
    if not files:
        print("没有匹配的输入文件")
        return 2
    if force_chip:
        args.chip = True
    ok = fail = 0
    for idx, src in enumerate(files, 1):
        print(f"\n  [{idx}/{len(files)}] {os.path.basename(src)}")
        try:
            spec = _build_spec(args, src)
            result = pipeline.process(spec, progress=_bar)
            print()
            out = result["out_info"]
            print(f"  ✓ {result['output']}")
            print(f"    {out['codec']} · {out['rate']} Hz · {out['channels']}ch · "
                  f"{out['duration']:.2f}s · {_fmt_size(out['size'])} · 用时 {result['elapsed']}s")
            if result.get("chip"):
                print(f"    芯片参数：{result['chip']['text']}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"\n  ✕ 失败：{type(exc).__name__}: {exc}")
            fail += 1
    print(f"\n  完成 {ok} 个，失败 {fail} 个\n")
    return 0 if fail == 0 else 1


def cmd_convert(args) -> int:
    return _run_batch(args, force_chip=False)


def cmd_chip(args) -> int:
    if not args.suffix:
        args.suffix = "_8bit"
    return _run_batch(args, force_chip=True)


def cmd_unlock(args) -> int:
    files = _expand(args.input)
    if not files:
        print("没有匹配的输入文件")
        return 2
    out_dir = args.out or "out"
    os.makedirs(out_dir, exist_ok=True)
    ekey = None
    if args.ekey:
        if os.path.isfile(args.ekey):
            with open(args.ekey, "rb") as fh:
                ekey = fh.read()
        else:
            ekey = args.ekey.encode("utf-8", "ignore")
    ok = fail = 0
    for idx, src in enumerate(files, 1):
        det = unlock_mod.detect(src)
        name = os.path.basename(src)
        if not det.encrypted:
            print(f"  [{idx}/{len(files)}] {name}")
            print("    - 跳过：不是加密格式")
            continue
        print(f"  [{idx}/{len(files)}] {name}")
        print(f"    {det.platform} · {det.kind}")
        stem = os.path.splitext(name)[0]
        tmp = os.path.join(out_dir, f".{stem}.unlockpart")
        try:
            res = unlock_mod.unlock(src, tmp, ekey=ekey,
                                    progress=lambda p, m: _bar(p, m))
            kind = res.kind or "audio"
            target = os.path.join(out_dir, f"{stem}.{kind}")
            i = 2
            while os.path.exists(target):
                target = os.path.join(out_dir, f"{stem} ({i}).{kind}")
                i += 1
            os.replace(tmp, target)
            info = audio_io.probe(target)
            print()
            print(f"    - 输出 {target}")
            print(f"      {info['codec']} · {info['rate']} Hz · {info['channels']}ch · "
                  f"{info['duration']:.2f}s · {_fmt_size(info['size'])}")
            if res.meta:
                print(f"      标签 {res.meta}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"\n    x 失败：{type(exc).__name__}: {exc}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            fail += 1
    print(f"\n  成功 {ok} 个，失败 {fail} 个\n")
    return 0 if fail == 0 else 1


def cmd_wave(args) -> int:
    files = _expand(args.input)
    if not files:
        print("没有匹配的输入文件")
        return 2
    for src in files:
        audio = decode(src, target_rate=22050)
        blob = (viz.spectrum_png(audio.samples, audio.rate) if args.kind == "spectrum"
                else viz.waveform_png(audio.samples, audio.rate))
        name = os.path.splitext(os.path.basename(src))[0] + f"_{args.kind}.png"
        target = args.output if (args.output and len(files) == 1) else os.path.join(args.out or "out", name)
        os.makedirs(os.path.dirname(os.path.abspath(target)) or ".", exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(blob)
        print(f"  ✓ {target}")
    return 0


def cmd_serve(args) -> int:
    import server
    sys.argv = ["server.py", "--host", args.host, "--port", str(args.port)]
    server.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="audioforge", description="音匠 AudioForge · 音乐转格式 / 8bit 芯片化")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出支持的格式与 8bit 预设")

    pi = sub.add_parser("info", help="查看音频信息")
    pi.add_argument("input", nargs="+")

    def add_common(sp):
        sp.add_argument("input", nargs="+", help="输入文件或通配符")
        sp.add_argument("-o", "--output", help="输出路径（单文件时生效）")
        sp.add_argument("--out", "-d", help="输出目录（默认 out）")
        sp.add_argument("-f", "--fmt", default="mp3", help="输出格式，见 list")
        sp.add_argument("-q", "--quality", default="standard",
                        choices=list(formats.QUALITY_PRESETS), help="品质档")
        sp.add_argument("--bitrate", type=int, default=0, help="码率 kbps（覆盖品质档）")
        sp.add_argument("--rate", type=int, default=0, help="目标采样率 Hz（0=保持）")
        sp.add_argument("--bits", type=int, default=16, choices=[16, 24, 32], help="PCM 位深")
        sp.add_argument("--volume", type=float, default=0.0, help="增益 dB")
        sp.add_argument("--normalize", default="none", choices=["none", "peak", "rms"])
        sp.add_argument("--fade-in", type=float, default=0.0, dest="fade_in")
        sp.add_argument("--fade-out", type=float, default=0.0, dest="fade_out")
        sp.add_argument("--start", type=float, default=0.0, help="裁剪起点 秒")
        sp.add_argument("--end", type=float, default=0.0, help="裁剪终点 秒（0=末尾）")
        sp.add_argument("--speed", type=float, default=1.0, help="变速 0.5~2.0")
        sp.add_argument("--pitch", type=float, default=0.0, help="移调半音")
        sp.add_argument("--channels", default="keep", choices=["keep", "mono", "stereo", "swap"])
        sp.add_argument("--trim-silence", action="store_true", dest="trim_silence")
        sp.add_argument("--reverse", action="store_true")
        sp.add_argument("--suffix", default="", help="输出文件名后缀")
        sp.add_argument("--chip", action="store_true", help="启用 8bit 芯片化")
        sp.add_argument("--preset", default="nes", choices=list(chip_mod.PRESETS), help="8bit 预设")
        sp.add_argument("--bit-depth", type=int, default=4, dest="bit_depth")
        sp.add_argument("--chip-rate", type=int, default=11025, dest="chip_rate")
        sp.add_argument("--squareness", type=float, default=0.55)
        sp.add_argument("--gate", type=float, default=0.35)
        sp.add_argument("--vibrato", type=float, default=0.22)
        sp.add_argument("--arp", type=float, default=0.0)
        sp.add_argument("--noise", type=float, default=0.0)
        sp.add_argument("--lowpass", type=float, default=0.0)
        sp.add_argument("--chip-stereo", default="mono", dest="chip_stereo",
                        choices=["mono", "keep", "widen", "pingpong"])
        sp.add_argument("--tape-stop", type=float, default=0.0, dest="tape_stop")
        sp.add_argument("--dither", default="noise", choices=["none", "noise", "noise-shape"])

    add_common(sub.add_parser("convert", help="格式转换（原声）"))
    pc = sub.add_parser("chip", help="转 8bit 芯片音乐")
    add_common(pc)

    pu = sub.add_parser("unlock", help="解密网易云/QQ音乐/酷狗/酷我加密格式")
    pu.add_argument("input", nargs="+")
    pu.add_argument("-d", "--out", default="out", help="输出目录")
    pu.add_argument("--ekey", default="", help="QMC v2 的 ekey（字符串或 .ekey 文件）")

    pw = sub.add_parser("wave", help="导出波形/频谱 PNG")
    pw.add_argument("input", nargs="+")
    pw.add_argument("-o", "--output")
    pw.add_argument("--out", "-d", default="out")
    pw.add_argument("--kind", default="wave", choices=["wave", "spectrum"])

    ps = sub.add_parser("serve", help="启动本地 Web 界面")
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--port", type=int, default=8770)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 0
    if not hasattr(args, "input"):
        args.input = []
    handlers = {"list": cmd_list, "info": cmd_info, "convert": cmd_convert,
                "chip": cmd_chip, "unlock": cmd_unlock, "wave": cmd_wave,
                "serve": cmd_serve}
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        print("\n  已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
