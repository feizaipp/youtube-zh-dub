# YouTube 英译中配音管线

本仓库同时是可安装的 Codex Skill 与独立命令行项目。将仓库克隆到
`~/.codex/skills/youtube-zh-dub` 后，即可通过 `$youtube-zh-dub` 调用；
`SKILL.md`、`agents/` 和 `scripts/` 构成 Skill 包，根目录的 Python 文件是
其自带的处理流水线。

## 环境要求

- Python 3.10 或更高版本；
- `ffmpeg`（同时提供 `ffprobe`）；
- 生成哔哩哔哩硬字幕版需要中文字体，Ubuntu/Debian 推荐安装 `fonts-noto-cjk`；
- 完整配音需要已安装并登录的 Codex CLI；
- 英文词级转录默认在本地运行 `faster-whisper medium.en`（CPU INT8），无需转录 API 费用；
- 默认中文配音使用本地 Fun-CosyVoice3-0.5B，并要求可用的 CosyVoice 源码、模型和独立 Python 环境；
- 仅在选择 `--tts-backend mai` 或远程 Whisper 时需要 `OPENROUTER_API_KEY`；
- 推荐安装 Node.js，供 `yt-dlp` 处理 YouTube JavaScript 校验。

纯下载模式只需要 Python、`yt-dlp`、`ffmpeg` 和 `ffprobe`，不需要
任何模型 Key 或 Codex CLI。

## 给其他 Agent 的快速部署

以下步骤从一台干净的 Debian/Ubuntu 主机开始，采用显式路径，不依赖 Skill
目录相邻的默认布局。这样在不同 agent、工作目录或容器之间迁移时不会误用
Python 环境或模型。

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg fonts-noto-cjk git python3-venv

git clone https://github.com/feizaipp/youtube-zh-dub.git \
  ~/.codex/skills/youtube-zh-dub
cd ~/.codex/skills/youtube-zh-dub
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -r requirements.txt

# 依照 CosyVoice 官方安装说明创建独立环境，并取得 Fun-CosyVoice3-0.5B。
git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git ~/CosyVoice
conda create -y -n cosyvoice python=3.10
conda run -n cosyvoice python -m pip install -r ~/CosyVoice/requirements.txt
modelscope download --model FunAudioLLM/Fun-CosyVoice3-0.5B \
  --local_dir ~/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B
```

`conda` 和 `modelscope` 不在基础系统中时，应先按 CosyVoice 官方文档安装；
不要把 CosyVoice 包安装进 Skill 的 `.venv`。运行完整工作流还需安装并登录
Codex CLI：`codex --version` 应能成功执行。YouTube 的 JavaScript 校验通常还
需要 Node.js；若下载报缺少 JS runtime，应先安装 Node.js，再重试同一命令。

先做不下载模型的静态检查：

```bash
cd ~/.codex/skills/youtube-zh-dub
.venv/bin/python -m py_compile youtube_dub.py scripts/run_youtube_zh_dub.py
.venv/bin/python scripts/run_youtube_zh_dub.py \
  'https://youtu.be/dQw4w9WgXcQ' --download-only --dry-run --video-title smoke-test
```

然后用明确的 CosyVoice 参数跑 45 秒验证片段。`--cosyvoice-threads 2` 与
`--fit-workers 2` 是 8 GiB 内存机器的保守起点；本地 TTS 始终只加载一个模型，
不要用 `--tts-workers` 增加 CosyVoice 并发。

```bash
PATH="$PWD/.venv/bin:$PATH" python3 scripts/run_youtube_zh_dub.py URL \
  --debug-seconds 45 --workdir /absolute/path/youtube-dub-smoke \
  --cosyvoice-root "$HOME/CosyVoice" \
  --cosyvoice-python "$(conda run -n cosyvoice which python)" \
  --cosyvoice-model "$HOME/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B" \
  --cosyvoice-threads 2 --fit-workers 2
```

确认验证目录中已有 `transcript.zh.srt`、`sync_report.json` 和最终视频后，再将
同一命令中的 `--debug-seconds 45` 删除以处理全片。任务中断、终端关闭或内存
回收后，使用**完全相同的 `--workdir` 和参数**重跑；管线会复用下载、转录、
翻译和已完成的句子音频。不要加 `--force`，除非明确需要丢弃缓存重做。

```bash
git clone https://github.com/feizaipp/youtube-zh-dub.git \
  ~/.codex/skills/youtube-zh-dub
cd ~/.codex/skills/youtube-zh-dub
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sudo apt-get install fonts-noto-cjk
```

安装后重新启动 Codex，使其重新扫描本地 Skills。可先执行不联网的检查：

```bash
.venv/bin/python scripts/run_youtube_zh_dub.py \
  'https://youtu.be/dQw4w9WgXcQ' --download-only --dry-run --video-title test
```

这个命令行工具完成以下流程：

1. 用一次 `yt-dlp` 任务同时下载最佳纯视频流和最佳纯音频流，并保留两个原始文件；
2. 在本地按静音点切分音频，使用共享的 `faster-whisper medium.en` 模型取得每个英文词的真实起止时间并换算成全片绝对时间；
3. 通过本机 Codex CLI 跨越原始切块检查英文语法和 ASR 重复词，再把完整句子按词序对齐回真实词级时间轴；句间原始停顿会被保留；
4. 通过本机 Codex CLI 识别主题，以领域专家口吻翻译为简体中文，并锁定校对后句子的时间戳；
5. 默认从每个英文句子的原始时间窗提取参考语音，用单实例 Fun-CosyVoice3 跨语言克隆当前说话人的音色并顺序生成中文；也可选择 MAI-Voice-2；
6. 超过舒适语速上限的句子会由本机 Codex CLI 自动精简并重新生成，最后用 `ffmpeg` 轻微调速、补静音、替换视频音轨；同时输出带可开关软字幕的通用版，以及把中文字幕烧录进画面的哔哩哔哩上传版。

## 安全设置

英文校对、主题识别、简体中文翻译和超时译文精简使用本机已登录的 `codex` 命令。Codex 子进程在独立临时目录中以只读模式运行，并且不会继承模型 API Key 或 API Base URL 覆盖项。

OpenRouter 只在显式选择 MAI 中文语音合成或 `openrouter-whisper1` 时使用。不要把 API Key 写进源码或命令行，请通过环境变量提供：

```bash
export OPENROUTER_API_KEY='你的新 Key'
```

如果 Key 曾经出现在聊天记录、终端历史或普通文本文件中，应在 OpenRouter 控制台撤销并重新创建。

运行前确认 Codex CLI 已安装并登录：

```bash
codex --version
```

## 运行

建议在项目虚拟环境中安装最新版 `yt-dlp`（旧版本经常因 YouTube 接口变化而失效）。检测到 Node.js 时，程序还会启用 yt-dlp 官方的 `ejs:github` 远程组件来解决 YouTube JavaScript 挑战，并默认使用 `web_embedded` 播放器客户端，降低匿名 VPS 下载 DASH 流时的 403 风险。可用 `--youtube-player-client` 覆盖。下载器使用 256 KiB HTTP 分段，并在速度持续低于 50 KiB/s 时主动重连，以缓解长视频单连接逐渐限速：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export PATH="$PWD/.venv/bin:$PATH"
```

默认处于安全调试模式，只处理开头 45 秒：

```bash
python3 youtube_dub.py 'https://www.youtube.com/watch?v=2cTDRKRQ5oc'
```

本地源音色配音默认查找相邻的 `cosyvoice/`、`miniconda-cosyvoice/bin/python` 和 `cosyvoice/pretrained_models/Fun-CosyVoice3-0.5B`。也可配置：

```bash
python3 youtube_dub.py URL --full \
  --tts-backend cosyvoice3-source \
  --cosyvoice-root /path/to/CosyVoice \
  --cosyvoice-python /path/to/cosyvoice-python \
  --cosyvoice-model /path/to/Fun-CosyVoice3-0.5B \
  --cosyvoice-threads 2
```

每个中文句子会使用同一时间窗的英文原声作为参考，因此多说话人视频无需预先标注角色。模型始终只加载一份并逐句生成，以控制内存。若要改回托管音色，使用 `--tts-backend mai`。

只需一键下载最高质量的独立视频流和独立音频流时，使用 Skill 启动器。它会自动创建 `output/<视频标题>/`，保留原始视频、原始音频和合并文件，支持 `.part` 断点续传，并且不需要 OpenRouter Key 或 Codex CLI：

```bash
python3 scripts/run_youtube_zh_dub.py \
  'https://www.youtube.com/watch?v=2cTDRKRQ5oc' --download-only
```

默认格式选择器为 `bestvideo` 和 `bestaudio`，不限制分辨率、帧率或源编码，因此会选择 yt-dlp 当前可取得的最清晰视频与最高质量音频。

可用 `--quality` 将视频清晰度限制为 720p 或 1080p；脚本会选择不超过该高度的最高质量视频流，音频始终选择最高质量：

```bash
python3 scripts/run_youtube_zh_dub.py URL --download-only --quality 720p
python3 scripts/run_youtube_zh_dub.py URL --download-only --quality 1080p
```

`--quality best` 保持不限制清晰度的默认行为。若同一输出目录已经完成另一种清晰度的下载，请使用新的 `--workdir`；只有明确要覆盖旧资源时才使用 `--force`。

调试模式只把指定片段发送给模型。下载阶段通过所选视频清晰度与 `bestaudio` 在同一次 yt-dlp 任务中取得两条流，`--keep-video` 会阻止合并后删除原始流。随后由本地 ffmpeg 从已下载的原始音频生成 WAV，并截取调试片段；不会再向 YouTube 发起第二次音频下载请求。可用 `--video-format` 和 `--audio-format` 分别覆盖两个 yt-dlp 格式选择器。程序会自动读取系统 HTTP(S) 代理，也可显式传入 `--proxy URL`。

调整调试长度或起点：

```bash
python3 youtube_dub.py URL --debug-seconds 30 --start-seconds 60 --workdir output/sample
```

确认短片结果后，必须显式传入 `--full` 才会处理全片：

```bash
python3 youtube_dub.py URL --full --workdir output/full
```

为避免中文配音出现“快进感”，后期加速默认严格限制为 `1.15x`。自然语音超过这个上限时，程序只把超时句子交给 Codex CLI 精简，并重新生成这些句子的 TTS；不会重新转录视频。可按需要调整：

```bash
python3 youtube_dub.py URL --full --workdir output/full \
  --max-tempo 1.12 --timing-rewrite-attempts 5
```

`--max-tempo` 可设为 `1.0`–`1.5`。值越低越自然，但可能需要更多精简轮次；建议保持在 `1.10`–`1.15`。

默认使用 Codex CLI 当前模型；需要显式指定时可传：

```bash
python3 youtube_dub.py URL --text-model MODEL_NAME
```

英文转录默认单任务，本地 CosyVoice3 默认 2 个 CPU 计算线程且逐句生成，逐句 ffmpeg 对齐默认 4 个线程。`--tts-workers` 只控制 MAI 后端：

```bash
python3 youtube_dub.py URL --full --workdir output/full \
  --transcribe-workers 1 --tts-workers 4 --fit-workers 4
```

并发只发生在同一阶段的独立片段之间。转录结果和对齐报告仍按片段 ID 排序并由主线程原子保存；TTS 超时判断仍会等待本轮全部句子完成。遇到限流时降低网络线程数；本机负载过高时降低 `--fit-workers`，不要反复使用 `--force`。

如果 YouTube 要求登录，可使用本机浏览器 Cookie：

```bash
python3 youtube_dub.py URL --cookies-from-browser chrome
```

各阶段可恢复；Whisper 的词级结果按后端、模型、计算类型和时间轴版本校验后缓存，旧的片段级缓存不会被误用。超长英文稿会尽量在完整句边界分批校对，中文翻译使用较小批次，并把每个成功批次单独缓存。若中途失败，重跑会安全复用兼容的已完成批次。未完成的 yt-dlp `.part` 文件也会断点续传。只有显式使用 `--force` 才会覆盖下载断点和重跑产物；`--stop-after transcribe` 可只调试到原始英文转录，`--stop-after polish` 可停在语法校对和完整句重分段之后。

若只需重建最终视频（例如修复播放器兼容性），无需重新调用模型：

```bash
python3 youtube_dub.py --workdir output/full --remux-only
```

`--remux-only` 会忽略 URL 和 `--full`，也不会读取或改写该目录的 `manifest.json`。
程序会检测源视频编码；H.264 会直接复制，AV1、VP9 等编码会转为 QuickTime 兼容的 H.264/AVC（`avc1`、`yuv420p`）。1080p 全片转码需要一定时间。

最终视频会自动包含 `transcript.zh.srt`，字幕编码为 QuickTime 支持的 `mov_text/tx3g`，使用 `chi` 语言码、“中文字幕”handler 名称，并标记为默认/强制字幕。为规避 QuickTime 在 0.000 秒不触发首条字幕渲染的问题，内嵌版本只将第一条字幕延后 100 毫秒，其余时间戳不变。若只需给已有视频加入或更新中文字幕，可直接复制音视频轨：

```bash
python3 youtube_dub.py --workdir output/full --subtitles-only
```

## 产物

默认位于 `output/`：

- `source.mp4`：下载的视频；
- `source_video_original.*`：yt-dlp 下载并保留的纯视频流；
- `source_audio_original.*`：yt-dlp 下载并保留的纯音频流；
- `source_audio.wav`：由上述原始音频转换得到的单声道 16 kHz 转录音频；
- `transcript.en.json` / `.srt`：Whisper 返回的原始切块英文稿；JSON 同时保留带绝对起止时间的词数组，供审计和恢复；
- `transcript.en.polished.json` / `.srt`：跨切块纠错并按完整句子重分段后的英文稿，JSON 同时记录重要修订；
- `transcript.zh.json` / `.srt`：与校对后英文句子保持相同时间戳的中文稿及主题；JSON 还记录为控制语速进行的逐句精简历史；
- `chinese_voice.wav`：对齐后的中文音轨；
- `sync_report.json`：每段裁剪后自然语音时长、目标时长、最终调速倍率和本轮自动精简记录；
- `dubbed.zh.mp4`：最终中文配音视频。
- `dubbed.zh.bilibili.mp4`：中文字幕已烧录进画面的 H.264/AAC 成片，适合直接上传哔哩哔哩；字幕始终可见，不依赖平台识别 MP4 内挂字幕轨。

## 断点续传最终视频

转录完成后，可以通过 SSH 和 `rsync` 下载最终视频。`SSH_USER`、`SSH_HOST`
和远程绝对路径必须替换成真实连接信息；客户端与服务器都需要安装 `rsync`，
并且用户需要具备 SSH 登录权限。如果下载中断，重新运行同一条命令即可续传并
校验已有内容。

Linux：

```bash
rsync --partial --append-verify --progress -e ssh \
  'SSH_USER@SSH_HOST:/absolute/path/dubbed.zh.mp4' './dubbed.zh.mp4'
```

macOS：

```bash
rsync --partial --append-verify --progress -e ssh \
  'SSH_USER@SSH_HOST:/absolute/path/dubbed.zh.mp4' './dubbed.zh.mp4'
```

Windows PowerShell（需要 WSL，并在 WSL 中安装 `rsync`）：

```powershell
wsl rsync --partial --append-verify --progress -e ssh `
  "SSH_USER@SSH_HOST:/absolute/path/dubbed.zh.mp4" `
  "/mnt/c/Users/WINDOWS_USER/Downloads/dubbed.zh.mp4"
```

通常的 rsync-over-SSH 下载源是 `USER@HOST:/path`，它不是网页链接。只有服务器
实际配置了 rsync daemon 和共享模块时，才会存在类似下面的 URL：

```text
rsync://HOST/MODULE/path/dubbed.zh.mp4
```

没有真实 SSH 地址或 rsync daemon 配置时，不应虚构连接信息。

时间轴以 Whisper 返回的真实词级起止时间为依据。每个切块的相对词时间会加上切块起点，换算为全片绝对时间；校对模型只修改文本，不生成时间戳，程序通过词序对齐把校对后的完整句子映射回这些真实边界，并保留句间静音。若后端没有返回词级时间戳，管线会明确失败，不会退回按字数或片段时长估算，以免再次产生音画错位。
