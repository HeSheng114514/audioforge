# 音匠 AudioForge

> 本地音乐工具箱：**私有格式解密** + **格式转换** + **8bit 芯片化** + 批量处理 + 试听对比。
> 纯 Python（PyAV 内置 ffmpeg），Windows 双击 `start.bat` 即用，无需安装 ffmpeg。

![界面](workspace/ui_main.png)

---

## 一分钟上手

| 场景 | 操作 |
| --- | --- |
| 图形界面 | 双击 start.bat（macOS/Linux：./start.sh），浏览器自动打开；也可手动访问 http://127.0.0.1:8770 |
| 想先试效果 | 界面左上「载入示例」，拖好参数 → 「试听处理后」 |
| 批量转格式 | 拖入多个文件 → 格式选 MP3/FLAC… → 「开始转换」 |
| 转 8bit | 右侧切到 **8bit** 页 → 打开开关 → 选预设 → 开始转换 |
| 解密 ncm/mflac/kgm | 右侧切到 **解锁** 页 → 一键解密（也可直接转换，会自动解锁） |
| 命令行 | `python cli.py chip 歌曲.mp3 --preset nes -d out` |

---

## 双击 start.bat 没反应？

启动器 launcher.py 会自行处理这些情况：

| 现象 | 原因 | 现在的行为 |
| --- | --- | --- |
| 窗口一闪而过 | 缺依赖 / 脚本报错 | 出错会 pause 停住并打印完整 traceback |
| 中文乱码、命令不被识别 | cmd.exe 用 GBK 读 UTF-8 中文 | start.bat 已是纯 ASCII + CRLF，中文全在 Python 里输出 |
| 「'thon，请先安装' 不是内部或外部命令」 | 批处理是 LF 换行，if 块整块错位 | 同上，改成 CRLF |
| 浏览器白屏 | 服务没起来就开了 | 等端口真正可连之后才开浏览器 |
| 端口被占用 | 上次没关干净 / 别的程序占用 | 自动换下一个可用端口，并在控制台打印新地址 |
| 重复双击 | 已经跑着一个 | 识别到是 AudioForge 就只打开浏览器，不起第二份 |
| 没有 Python | 未安装 / 没加 PATH | 给下载地址，并探测 Windows 商店的假 python 别名 |

还是打不开，就在目录里手动跑一次，能看到完整报错：

    cd "D:\DeepSeek-Harness\D-音乐"
    python launcher.py

常用参数：

    python launcher.py --port 8800        # 换端口
    python launcher.py --no-browser       # 不自动开浏览器
    python launcher.py --host 0.0.0.0     # 局域网共享（手机也能用）

如果提示「已经在运行」，先关掉旧的：

    netstat -ano | findstr :8770
    taskkill /F /PID <PID>

---

## 功能

## 功能

### 0. 私有加密格式解密

网易云 / QQ音乐 / 酷狗 / 酷我下载的歌是加密容器，普通播放器和转码器都打不开。
这里内置了原生解密，**流式还原原始音频、不重编码、零音质损失**。

| 平台 | 格式 | 原理 |
| --- | --- | --- |
| 网易云音乐 | .ncm | AES-128-ECB 解出 RC4 密钥 → 256 字节 S 盒周期异或；元数据单独用第二把 AES 密钥解 |
| QQ音乐 | .qmc0 .qmc2 .qmc3 .qmcflac .qmcogg .qmcra | 8x7 种子表 + 蛇形游标生成 0x8000 周期掩码流，边界跳一位 |
| QQ音乐 | .mflac .mgg（v2） | 音频用每首歌独立的 ekey 派生密钥；文件尾有 QTag 结构，可外置 ekey |
| 酷狗音乐 | .kgm .kgma .vpr | 头部取 16 字节文件密钥 + 272 项双表 mask_v1/mask_v2 + 半字节互换；vpr 再叠一层 |
| 酷我音乐 | .kwm | 前 1024 字节为 32 字节密钥重复块，音频按 32 字节周期异或 |

实测（真实 NCM 文件，111 MB / 96 kHz FLAC）：

    111,698,478 字节 → 111,465,324 字节 FLAC，耗时 0.32 秒

比逐字节实现快 67 倍 —— 密钥流只有 256 字节周期，先算成查表再用 numpy 向量化。

用法：直接拖进软件，右侧「解锁」页点一下就行；也可以直接点「开始转换」，
会自动先解密再处理（勾选「转换时自动解锁」）。命令行：

    python cli.py unlock 歌曲.ncm -d out          # 只解密
    python cli.py chip 歌曲.ncm --preset nes       # 解密 + 8bit 一条龙

.mflac / .mgg 如果文件里没有可用 ekey，会明确告诉你需要外置 ekey，而不是吐一个坏文件。

---

### 1. 格式转换（12 种输出）

| 格式 | 编码 | 类型 | 说明 |
| --- | --- | --- | --- |
| MP3 | libmp3lame | 有损 | 兼容性最好，64–512 kbps |
| WAV | PCM 16/24/32f | PCM | 无损，适合继续处理 |
| FLAC | flac | 无损 | 约 WAV 一半体积 |
| OGG | Vorbis | 有损 | 同码率优于 MP3 |
| OPUS | libopus | 有损 | 低码率高音质 |
| M4A | AAC | 有损 | 苹果生态 |
| ALAC | alac | 无损 | 苹果无损 |
| AIFF | PCM | PCM | 苹果 PCM |
| WMA | wmav2 | 有损 | Windows Media |
| MP2 / AC-3 / CAF | — | 兼容 | 广播、影音、CoreAudio |

输入侧支持 MP3 / WAV / FLAC / OGG / OPUS / M4A / AAC / WMA / AIFF / MP2 / AC3 / CAF / WV / APE / AMR / M4B / WEBM / MP4 / MKV 等（走 ffmpeg 解码器，实际能力取决于 PyAV 自带的那份 ffmpeg）。

品质档：**8bit 原生 / 省流量 / 标准 / 高品质 / 母带级**，也可单独指定码率、采样率、PCM 位深。

### 2. 8bit 芯片化（核心玩法）

把任意音乐「压」成红白机 / GameBoy / C64 的声音，处理链是：

```
芯片步进效果 → 波形方化 → 采样率骤降(零阶保持) → 位深量化 → 抗混叠低通 → 归一化
```

* **位深量化**：1–16 bit。4bit 只有 15 个台阶，就是 FC 那种「颗粒感」；可选噪声抖动（dither）避免台阶感过硬。
* **等效采样率**：1 kHz–44.1 kHz。11 kHz 是 FC 味，8 kHz 更闷更糙，22 kHz 保留更多细节。
* **波形方化**：把正弦味往方波/pulse 推，用量 0–1。
* **音量步进 / 门限颤音**：模拟芯片只有 0/1/2/3 级音量包络的断续感。
* **颤音、琶音、环形调制**：FC 的 vibrato / arpeggio、SID 的 ring mod。
* **噪声床**：按信号包络生成门控白噪声（FC noise channel）。
* **抗混叠低通 / 立体声（单声道·原样·加宽·乒乓）/ 结尾减速（tape stop）**。

内置 7 个预设：

| 预设 | 参数 | 味道 |
| --- | --- | --- |
| 红白机 FC | 4bit / 11 kHz / 方化 0.55 | 最经典的 8bit |
| GameBoy | 4bit / 8 kHz / 强方化 | 更闷更糙 |
| C64 (SID) | 6bit / 12 kHz / ring mod | 金属感 |
| 街机 | 6bit / 16 kHz / 乒乓立体声 | 明亮颗粒 |
| 8bit 轻量 | 8bit / 22 kHz | 整首歌都耐听 |
| 磁带 Lo-Fi | 8bit / 16 kHz / 抖动 + 驱动 | 复古卡带 |
| 微 8bit | 12bit / 32 kHz | 只加一点点颗粒 |

### 3. 调音

音量（dB）· 峰值/RMS 归一化 · 变速 0.5–2.0×（相位声码器，不变调）· 移调 ±12 半音（不变速，误差 <0.02%）· 10 段均衡（±12 dB）· 淡入淡出 · 裁剪起止 · 自动去首尾静音 · 单声道/立体声/左右互换 · 倒放。

### 4. 界面能力

* 拖拽多文件、批量队列、每文件独立进度
* 波形 + 频谱瀑布图（边播边扫）
* A/B 试听：原声 vs 处理后（默认截取前 12 秒即时渲染）
* **一键导出波形对比图 PNG**（原声 / 处理后 上下对照）
* 处理链实时预览（改任何参数，立刻告诉你这次会做什么）
* 标签保留与改写（ID3 / Vorbis / MP4 / FLAC）

---

## 命令行

```bash
python cli.py list                       # 列出格式与预设
python cli.py info 歌曲.mp3              # 查看音频信息
python cli.py convert 歌曲.mp3 -f flac -d out        # 转 FLAC
python cli.py convert "*.wav" -f mp3 --bitrate 320   # 批量转 MP3
python cli.py chip 歌曲.mp3 --preset nes -d out      # 转 8bit（默认加 _8bit 后缀）
python cli.py chip *.mp3 --preset gameboy --bit-depth 3 --chip-rate 8000 -f wav -d out
python cli.py wave 歌曲.mp3 --kind spectrum -d out   # 导出频谱图
python cli.py serve --port 8770          # 起本地 Web
```

`chip` 可调参数：`--bit-depth --chip-rate --squareness --gate --vibrato --arp --noise --lowpass --chip-stereo --tape-stop --dither`。

---

## 目录结构

```
.
├── start.bat / start.sh      # 双击启动（纯 ASCII + CRLF）
├── launcher.py                # 启动器：依赖自检 / 端口探测 / 开浏览器
├── cli.py                     # 命令行入口（convert / chip / wave / serve / info / list）
├── server.py                  # Flask 本地服务 + 任务队列
├── core/
│   ├── formats.py             # 格式注册表、品质档、采样率对齐
│   ├── audio_io.py            # PyAV 解码/编码/探测 + Ogg granule 修正
│   ├── dsp.py                 # FIR 滤波、EQ、相位声码器变速变调、限幅、淡入淡出
│   ├── chip.py                # 8bit 引擎（量化/采样保持/方化/芯片特效/预设）
│   ├── unlock.py              # 加密格式统一入口（按扩展名 + 魔数分流）
│   ├── ncm.py / qmc.py        # 网易云 / QQ音乐解密
│   ├── kgm.py / kwm.py        # 酷狗 / 酷我解密
│   ├── stream_decrypt.py      # 流式解密基座（分块 + 落盘 + 格式嗅探）
│   ├── pipeline.py            # JobSpec + 处理管线
│   ├── viz.py                 # 手写 PNG 波形/频谱/对比图
│   ├── tags.py                # mutagen 标签读写
│   └── console.py             # Windows 控制台 UTF-8
├── web/                       # 前端（原生 HTML/CSS/JS，无构建）
├── tools/make_demos.py        # 生成演示音频
├── tests/                     # 34 + 15 + 22 个自动化测试
├── samples/                   # 演示曲
└── workspace/                 # 上传 / 输出 / 试听缓存
```

---

## 技术要点（踩过的坑）

1. **不依赖 ffmpeg.exe**：PyAV 自带 ffmpeg 动态库，只调 `av` 的编解码 API。各编码器支持的采样格式/采样率不同（vorbis 只吃 `fltp`、opus 只吃 48k），编码前统一协商并重采样。
2. **Ogg 时长**：本机 ffmpeg 的 vorbis 编码器不回填末页 granulepos，容器头时长会显示成实际的 1/16。`audio_io.fix_ogg_granule()` 直接重写末页 granule 并重算 CRC。
3. **Vorbis 是实验性编码器**：需要给编码器传 `-strict -2`，否则 `avcodec_open2` 直接报 ExperimentalError。
4. **滤波不能用整段 FFT**：循环卷积会把结尾能量折到开头，听起来像整首歌被「错位」。改用窗函数法设计的**线性相位 FIR**（级联低通实现带通），并按截止频率自适应阶数。
5. **变速变调用相位声码器**：WSOLA 会有 1–3% 的音高漂移，改成 STFT 相位声码器后 ±12 半音的误差都在 0.02% 以内。
6. **解密要向量化**：NCM 逐字节 Python 解 111 MB 要 21.5 秒，看出密钥流只有 256 字节周期后改成查表 + numpy 异或，降到 0.32 秒。
7. **QMC 掩码有边界跳位**：每 0x8000 个样本要跳过一位、且游标在 x<0/x>6 时反向，直接照抄参考 C++ 才能逐位对齐。
8. **解密必须做可逆性验证**：手上只有 NCM 真文件，其余三个用「反向打包 → 解密 → 逐字节比对」自证，比只看文件头可靠得多。
9. **解码不依赖 frame.time**：Ogg/Vorbis 解码出的时间戳会带偏移甚至为负，按样本计数做跳过/截断才稳。

---

## 依赖

```
av        音频解码/编码（内含 ffmpeg）
numpy     全部 DSP
flask     本地 Web
mutagen   标签
```

`pip install -r requirements.txt`（`start.bat` 会自动检测安装）。

## 测试

```bash
python tests/test_core.py     # 34 项：格式往返、DSP、8bit、管线、可视化
python tests/test_api.py      # 15 项：HTTP 接口、上传、任务、越权防护
python tests/test_unlock.py   # 22 项：四个解密器的可逆性 + 真实 NCM 端到端
```

## 说明

* 服务默认只监听 `127.0.0.1`，不对外网暴露。
* 上传/输出都在 `workspace/` 下，`/api/delete` 只允许删这三个子目录里的文件。
* 音源与输出都留在本地，不做任何上传。
