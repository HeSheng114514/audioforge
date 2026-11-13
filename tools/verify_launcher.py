# -*- coding: utf-8 -*-
import os, socket, subprocess, sys, time, urllib.request

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

d = open('start.bat', 'rb').read()
print("start.bat:", len(d), "bytes | ascii:", all(b < 128 for b in d),
      "| CR:", d.count(b'\r'), "| LF:", d.count(b'\n'))

# 清理可能残留的实例
def kill_port(port):
    try:
        out = subprocess.check_output("netstat -ano -p TCP", shell=True, text=True, errors="replace")
        pids = set()
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line:
                pids.add(line.split()[-1])
        for pid in pids:
            subprocess.call(f"taskkill /F /PID {pid}", shell=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("kill err", e)

for p in (8894, 8895, 8896, 8897, 8898, 8899):
    kill_port(p)

print("\n--- 1) start.bat 通过 cmd.exe 启动 ---")
proc = subprocess.Popen('cmd.exe /c "start.bat --no-browser --port 8894"',
                        shell=True, cwd=os.getcwd(),
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
ok = False
for _ in range(40):
    time.sleep(0.5)
    try:
        with urllib.request.urlopen("http://127.0.0.1:8894/api/meta", timeout=1) as r:
            body = r.read(200)
            if b'"formats"' in body:
                ok = True
                break
    except Exception:
        pass
print("  服务可用:", ok)

if ok:
    with urllib.request.urlopen("http://127.0.0.1:8894/", timeout=2) as r:
        html = r.read(2000)
    print("  首页字节:", len(html), "| 含标题:", b'AudioForge' in html)

print("\n--- 2) 重复启动识别 ---")
try:
    out = subprocess.run(["python", "launcher.py", "--no-browser", "--port", "8894"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=20).stdout
    print("  " + "\n  ".join([l for l in out.splitlines() if l.strip()][:4]))
except subprocess.TimeoutExpired:
    print("  (超时，说明它去抢端口了 —— 不符合预期)")

kill_port(8894)
proc.terminate()
print("\n清理完成")
