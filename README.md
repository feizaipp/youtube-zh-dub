# YouTube 英译中配音管线

本仓库同时是可安装的 Codex Skill 与独立命令行项目。将仓库克隆到
`~/.codex/skills/youtube-zh-dub` 后，即可通过 `$youtube-zh-dub` 调用；
`SKILL.md`、`agents/` 和 `scripts/` 构成 Skill 包，根目录的 Python 文件是
其自带的处理流水线。

## 环境要求

- Python 3.10 或更高版本；
- `ffmpeg`（同时提供 `ffprobe`）；
- 完整配音需要已安装并登录的 Codex CLI；
- 完整配音需要通过环境变量提供 `OPENROUTER_API_KEY`；
- 推荐安装 Node.js，供 `yt-dlp` 处理 YouTube JavaScript 校验。

纯下载模式只需要 Python、`yt-dlp`、`ffmpeg` 和 `ffprobe`，不需要
OpenRouter Key 或 Codex CLI。

```bash
git clone https://github.com/feizaipp/youtube-zh-dub.git \
  ~/.codex/skills/youtube-zh-dub
cd ~/.codex/skills/youtube-zh-dub
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

安装后重新启动 Codex，使其重新扫描本地 Skills。可先执行不联网的检查：

```bash
.venv/bin/python scripts/run_youtube_zh_dub.py \
  'https://youtu.be/dQw4w9WgXcQ' --download-only --dry-run --video-title test
```

这个命令行工具完成以下流程：

1. 用一次 `yt-dlp` 任务同时下载最佳纯视频流和最佳纯音频流，并保留两个原始文件；
2. 在本地按静音点切分音频，用有界线程池并发调用 OpenRouter 的 MAI-Transcribe 1.5 转成英文；
3. 通过本机 Codex CLI 跨越原始切块检查英文语法和 ASR 重复词，再按完整句子重新分段并映射回原时间轴；
4. 通过本机 Codex CLI 识别主题，以领域专家口吻翻译为简体中文，并锁定校对后句子的时间戳；
5. 用有界线程池并发调用 MAI-Voice-2 生成逐句普通话配音，裁掉模型附带的首尾静音并测量真实讲话时长；
6. 超过舒适语速上限的句子会由本机 Codex CLI 自动精简并重新生成，最后用 `ffmpeg` 轻微调速、补静音、替换视频音轨并加入中文字幕。

## 安全设置

英文校对、主题识别、简体中文翻译和超时译文精简不调用 OpenRouter，而是使用本机已登录的 `codex` 命令。Codex 子进程在独立临时目录中以只读模式运行，并且不会继承 `OPENROUTER_API_KEY` 或 API Base URL 覆盖项。

OpenRouter 只用于 MAI 英文转录和 MAI 中文语音合成。不要把 API Key 写进源码或命令行，请通过环境变量提供：

```bash
export OPENROUTER_API_KEY='你的新 Key'
```

如果 Key 曾经出现在聊天记录、终端历史或普通文本文件中，应在 OpenRouter 控制台撤销并重新创建。

运行前确认 Codex CLI 已安装并登录：

```bash
codex --version
```

## 运行

建议在项目虚拟环境中安装最新版 `yt-dlp`（旧版本经常因 YouTube 接口变化而失效）。检测到 Node.js 时，程序还会启用 yt-dlp 官方的 `ejs:github` 远程组件来解决 YouTube JavaScript 挑战，避免因 `n challenge` 失败导致媒体请求返回 403。下载器使用 256 KiB HTTP 分段，并在速度持续低于 50 KiB/s 时主动重连，以缓解长视频单连接逐渐限速：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export PATH="$PWD/.venv/bin:$PATH"
```

默认处于安全调试模式，只处理开头 45 秒：

```bash
python3 youtube_dub.py 'https://www.youtube.com/watch?v=2cTDRKRQ5oc'
```

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

英文转录默认使用 3 个并发线程，中文 TTS 和逐句 ffmpeg 对齐默认各使用 4 个。超长视频可按 OpenRouter 限流和本机 CPU 情况调整，允许范围均为 `1`–`16`：

```bash
python3 youtube_dub.py URL --full --workdir output/full \
  --transcribe-workers 3 --tts-workers 4 --fit-workers 4
```

并发只发生在同一阶段的独立片段之间。转录结果和对齐报告仍按片段 ID 排序并由主线程原子保存；TTS 超时判断仍会等待本轮全部句子完成。遇到限流时降低网络线程数；本机负载过高时降低 `--fit-workers`，不要反复使用 `--force`。

如果 YouTube 要求登录，可使用本机浏览器 Cookie：

```bash
python3 youtube_dub.py URL --cookies-from-browser chrome
```

各阶段可恢复；超长英文稿会尽量在完整句边界分批校对，中文翻译使用较小批次，并把每个成功批次单独缓存。若中途失败，重跑时会按输入文本、完整请求、Codex 模型、文本后端及管线版本安全复用已经完成的批次，旧的 OpenRouter 文本结果不会被当成 Codex 结果继续复用。未完成的 yt-dlp `.part` 文件也会断点续传。只有显式使用 `--force` 才会覆盖下载断点和重跑产物；`--stop-after transcribe` 可只调试到原始英文转录，`--stop-after polish` 可停在语法校对和完整句重分段之后。

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
- `transcript.en.json` / `.srt`：MAI 返回的原始切块英文稿，保留用于审计；
- `transcript.en.polished.json` / `.srt`：跨切块纠错并按完整句子重分段后的英文稿，JSON 同时记录重要修订；
- `transcript.zh.json` / `.srt`：与校对后英文句子保持相同时间戳的中文稿及主题；JSON 还记录为控制语速进行的逐句精简历史；
- `chinese_voice.wav`：对齐后的中文音轨；
- `sync_report.json`：每段裁剪后自然语音时长、目标时长、最终调速倍率和本轮自动精简记录；
- `dubbed.zh.mp4`：最终中文配音视频。

## 断点续传最终视频

转录完成并取得最终视频的真实下载 URL 后，可使用下面的命令下载。
如果下载中断，重新运行同一条命令即可从已有文件继续；服务端需要支持 HTTP
Range 请求。

Linux：

```bash
curl -fL -C - --retry 5 --retry-delay 2 -o 'dubbed.zh.mp4' 'DOWNLOAD_URL'
```

macOS：

```bash
curl -fL -C - --retry 5 --retry-delay 2 -o 'dubbed.zh.mp4' 'DOWNLOAD_URL'
```

Windows PowerShell 或命令提示符：

```powershell
curl.exe -fL -C - --retry 5 --retry-delay 2 -o "dubbed.zh.mp4" "DOWNLOAD_URL"
```

将 `DOWNLOAD_URL` 替换成最终视频附件或文件服务提供的真实下载地址。本机文件
路径不是下载 URL；没有可远程访问的 URL 时，不应虚构地址。

MAI-Transcribe 的标准接口不返回词级时间戳。因此原始时间戳精度是“静音感知的片段级”，默认约 15 秒。校对模型只输出有序的完整句子，不生成时间戳；程序依据原始各块的词数密度将句子边界确定性地映射回完整时间轴，中文翻译和配音再严格复用这些边界。
