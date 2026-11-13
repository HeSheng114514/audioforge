# -*- coding: utf-8 -*-
"""音匠 AudioForge 启动器（双击 start.bat / start.sh 实际跑的是这个）。

为什么要有这一层：
  * 批处理文件在中文 Windows 下极易被 cmd.exe 用 GBK 误读（UTF-8 中文 +
    LF 换行会让 if 块整个错位），所以 .bat 保持纯 ASCII，中文全部交给 Python；
  * 端口被占用时自动换一个，不会一闪而过；
  * 启动前检查依赖，缺了自动装；
  * 服务真正起来之后再开浏览器，避免白屏；
  * 任何异常都打印出来并停住，双击窗口不会瞬间消失。

用法：
    python launcher.py              # 默认 127.0.0.1:8770，自动开浏览器
    python launcher.py --port 9000
    python launcher.py --no-browser
    python launcher.py --host 0.0.0.0     # 局域网共享（注意防火墙）
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

def _resource_root() -> str:
    """打包成单文件 exe 后，资源在 sys._MEIPASS 下；源码运行时用脚本目录。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


BASE = _resource_root()
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from core.console import enable_utf8  # noqa: E402

enable_utf8()

LINE = "=" * 64
REQUIRED = [("av", "av"), ("numpy", "numpy"), ("flask", "flask"), ("mutagen", "mutagen")]


# --------------------------------------------------------------------------- #
# 环境自检
# --------------------------------------------------------------------------- #
def check_python() -> None:
    if sys.version_info < (3, 10):
        print(f"[x] 需要 Python 3.10 或更高版本，当前是 {sys.version.split()[0]}")
        print("    下载： https://www.python.org/downloads/")
        raise SystemExit(1)


def missing_modules() -> list:
    import importlib.util
    missing = []
    for mod, pkg in REQUIRED:
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(pkg)
        except (ImportError, ValueError):
            missing.append(pkg)
    return missing


def ensure_deps() -> None:
    missing = missing_modules()
    if not missing:
        return
    print(f"[*] 缺少依赖：{', '.join(missing)}，正在安装…")
    req = os.path.join(BASE, "requirements.txt")
    cmds = []
    if os.path.isfile(req):
        cmds.append([sys.executable, "-m", "pip", "install", "-r", req])
    cmds.append([sys.executable, "-m", "pip", "install", *missing])
    for cmd in cmds:
        print("    >", " ".join(cmd))
        try:
            import subprocess
            rc = subprocess.call(cmd)
        except Exception as exc:  # noqa: BLE001
            print(f"[!] 安装命令执行失败：{exc}")
            rc = 1
        if rc == 0 and not missing_modules():
            print("[✓] 依赖安装完成")
            return
    if missing_modules():
        print("[x] 依赖安装失败。请手动执行：")
        print(f"    {sys.executable} -m pip install " + " ".join(missing))
        print("    若在国内网络，可加镜像： -i https://pypi.tuna.tsinghua.edu.cn/simple")
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# 端口
# --------------------------------------------------------------------------- #
def port_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_our_server(host: str, port: int) -> bool:
    """判断该端口上跑的是不是 AudioForge（而不是别的程序）。

    只看 /api/meta 的响应前缀：JSON 里 "formats" 与 "eq_bands" 都在开头，
    末尾的 "version" 可能落在读取窗口之外，不能当判据。
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/meta", timeout=1.5) as resp:
            body = resp.read(2048)
            return b'"formats"' in body and b'"eq_bands"' in body
    except Exception:  # noqa: BLE001
        return False


def pick_port(host: str, preferred: int, tries: int = 30) -> int:
    for offset in range(tries):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if not port_open(host, candidate):
            return candidate
    return preferred


# --------------------------------------------------------------------------- #
# 浏览器
# --------------------------------------------------------------------------- #
def open_browser_later(host: str, port: int, delay: float = 0.8) -> None:
    url = f"http://{('127.0.0.1' if host in ('0.0.0.0', '::') else host)}:{port}/"

    def worker() -> None:
        deadline = time.time() + 25
        while time.time() < deadline:
            if port_open(host if host not in ("0.0.0.0", "::") else "127.0.0.1", port):
                time.sleep(delay)
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    print(f"[!] 无法自动打开浏览器，请手动访问 {url}")
                return
            time.sleep(0.2)

    threading.Thread(target=worker, daemon=True, name="af-open-browser").start()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def banner(host: str, port: int, out_dir: str) -> None:
    show = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print()
    print(LINE)
    print("   音匠 AudioForge  ·  音乐转格式 / 8bit 芯片化")
    print(LINE)
    print(f"   访问地址   http://{show}:{port}")
    if host in ("0.0.0.0", "::"):
        print("   局域网共享 已开启（同一 WiFi 下用本机 IP 访问）")
    print(f"   输出目录   {out_dir}")
    print(f"   Python     {sys.version.split()[0]}  ({sys.executable})")
    print(LINE)
    print("   关闭窗口或按 Ctrl+C 即可停止服务")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="音匠 AudioForge 启动器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--force", action="store_true", help="端口被占用时仍然强行启动（会失败）")
    args = ap.parse_args()

    if os.name == "nt":
        try:
            os.system("title AudioForge")
        except Exception:  # noqa: BLE001
            pass

    check_python()
    if not getattr(sys, "frozen", False):
        ensure_deps()

    host, port = args.host, args.port
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

    if port_open(probe_host, port) and not args.force:
        if is_our_server(probe_host, port):
            url = f"http://{probe_host}:{port}/"
            print(LINE)
            print("   音匠 AudioForge 已经在运行")
            print(f"   访问地址   {url}")
            print(LINE)
            if not args.no_browser:
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001
                    pass
            print("\n   已为你打开浏览器，本窗口可以关掉。")
            time.sleep(2.5)
            return 0
        new_port = pick_port(probe_host, port + 1)
        print(f"[!] 端口 {port} 已被其他程序占用，改用 {new_port}")
        port = new_port

    try:
        import server
    except Exception as exc:  # noqa: BLE001
        print(f"[x] 加载服务失败：{type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    banner(host, port, server.OUT_DIR)
    if not args.no_browser:
        open_browser_later(probe_host, port)

    try:
        server.app.run(host=host, port=port, debug=False, threaded=True,
                       use_reloader=False)
    except OSError as exc:
        print(f"\n[x] 启动失败：{exc}")
        print("    端口可能被占用，试试： python launcher.py --port 8800")
        return 1
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已停止。")
        raise SystemExit(0)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"\n[x] 未预期的错误：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise SystemExit(1)
