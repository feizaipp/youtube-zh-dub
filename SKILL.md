---
name: youtube-zh-dub
description: Download selectable-quality YouTube streams, or turn an English YouTube video into a timestamped Simplified Chinese dub using Alibaba Cloud CosyVoice or local source-voice cloning, with embedded subtitles. Use for YouTube 中文配音/中文翻译视频, complete dubbing workflows, or clear/best-quality YouTube downloads.
---

# YouTube 中文配音

Run the verified pipeline through `scripts/run_youtube_zh_dub.py`. Treat the command “转录 <YouTube Link>” as a request for the complete Chinese-dubbed video, not only a text transcript.
Set `SKILL_DIR` to this Skill package's root before using the examples below.

## Execute

For a fresh machine or a different agent environment, read the **“给其他 Agent 的快速部署”**
section in `README.md` before running the workflow. It defines the Alibaba Cloud
credentials, optional local CosyVoice environment, smoke test, and exact resume rule.

1. Extract exactly one `youtube.com` or `youtu.be` URL from the request. Reject a missing or non-YouTube URL instead of guessing.
2. The defaults are local `faster-whisper medium.en` transcription (CPU INT8) and Alibaba Cloud `cosyvoice-v3.5-flash` fixed-voice TTS. Require a Beijing-region `DASHSCOPE_API_KEY` plus a matching cloned/designed voice through `ALIYUN_COSYVOICE_VOICE` or `--voice`. The launcher safely loads those variables from `.secrets/dashscope.env` in the Skill directory or its parent when they are absent from the process environment; only DashScope key, workspace, and voice variables are accepted from that file. Use `DASHSCOPE_WORKSPACE_ID` for the preferred workspace endpoint; the compatible shared Beijing endpoint is used when it is absent. Require `OPENROUTER_API_KEY` only when `--tts-backend mai` or `--transcriber-backend openrouter-whisper1` is selected. Never expose, recover, log, or place API keys on the command line. English polishing, topic detection, Chinese translation, and timing rewrites are completed by the Agent currently running this Skill, using its own configured backend model; they do not invoke a separate model CLI.
3. Run from any working directory:

   ```bash
   python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" 'YOUTUBE_URL'
   ```

4. Monitor the long-running command and report meaningful stage changes. Do not leave the user without an update for more than 60 seconds.
5. On a transient failure, inspect the actual error and rerun the same command so completed stages resume. Do not add `--force` unless the user explicitly requests regeneration or a corrupted cached stage is proven.

When the pipeline reports an Agent text request, follow [the agent text-exchange protocol](references/agent-text-backend.md): use the current backend model to produce the required JSON response, save it at the requested path, and rerun the unchanged command. This is the portable text backend for Codex, OpenClaw, Hermes, and other Skill hosts.

For download-only requests, run the same launcher with `--download-only`. This mode preserves both original streams, creates the title-based output directory, supports `.part` resume, and does not require or call either model API. Select a video cap with `--quality 720p` or `--quality 1080p`; omit it or use `--quality best` for unrestricted quality. Always keep `bestaudio`:

```bash
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --download-only --quality 1080p
```

For a cover-only request, run `--download-cover`. It downloads no audio/video, starts no transcription or dubbing stage, and saves the selected YouTube thumbnail as `cover.jpg` in the title-based output directory (or an explicit `--workdir`):

```bash
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --download-cover
```

Every non-dry-run normal workflow—including full dubbing, debug clips, and `--download-only`—must also download or reuse `cover.jpg` in its selected output directory before media processing. A cover retrieval failure is a run failure; do not silently omit it. `--dry-run` does not download a cover.

The launcher defaults to:

- Full-video processing with `--full`.
- One output directory named after the YouTube title: `<skill-directory>/output/<video-title>`.
- A sanitized title that preserves Unicode letters and numbers, removes punctuation and symbols, and converts each run of title whitespace to one `-`. Append the video ID only if another video already occupies the same title.
- Maximum post-processing speed-up of `1.15x` and up to five translation-shortening attempts.
- Fixed hosted voice synthesis: synthesize each Chinese sentence with `cosyvoice-v3.5-flash`, download its expiring result URL immediately, and cache the local 24 kHz WAV by text, model, voice, speed, and instruction.
- Bounded hosted concurrency: use four `--tts-workers` by default. Successful sentences remain cached after partial API failures, so rerun unchanged and do not add `--force`.
- CPU tuning for the verified 10-vCPU/8GB VPS: cap local Whisper at six threads to avoid memory-bandwidth contention, run Demucs with two jobs, and use x264 `fast` for required H.264 encodes. Override with `--whisper-cpu-threads`, `--demucs-jobs`, or `--video-preset` after benchmarking a different host.
- Background preservation: use local Demucs `htdemucs` with two jobs to remove the original English vocal stem, then sidechain-duck and mix the remaining stereo background under the Chinese dub. Cache both separation and mixing by content hash. Use `--background-mode none` only when the user explicitly wants the legacy voice-only output or Demucs is unavailable.
- The bundled verified source pipeline at `<skill-directory>/youtube_dub.py`.
- The project `.venv/bin` prepended to `PATH` when present.

Pass supported pipeline options after the URL. Examples:

```bash
# Use Chrome cookies only when the user authorizes it or YouTube requires login.
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --cookies-from-browser chrome

# Or use an explicitly supplied Netscape-format cookie file.
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --cookies /absolute/path/cookies.txt

# Explicit alternate output directory.
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --workdir /absolute/output/path
```

Use `--tts-backend cosyvoice3-source` to opt back into per-sentence local source-voice cloning. That backend discovers `COSYVOICE_ROOT`, `COSYVOICE_PYTHON`, and `COSYVOICE_MODEL`, or accepts the matching command options. Its default layout is a sibling `cosyvoice/` checkout, `miniconda-cosyvoice/bin/python`, and `cosyvoice/pretrained_models/Fun-CosyVoice3-0.5B`.

When Alibaba Cloud voice enrollment needs a public sample URL and the user has authorized this host's Nginx endpoint, publish only the final 16-bit WAV with `scripts/nginx_voice_sample.py publish`. Use the returned URL immediately, revoke it after enrollment reaches `OK`, and retain only the resulting `voice_id` in the run registry. Never publish samples under the general `/home/hermes/data` download root. The IP-only endpoint is unencrypted HTTP; disclose that limitation and prefer private OSS or trusted HTTPS when available.

Background preservation requires the project environment to have `demucs` installed (via `requirements.txt`); its model weights download on first use. If that dependency cannot be installed or the user requests a voice-only result, pass `--background-mode none` explicitly. Do not silently drop background audio after a Demucs failure.

## Debug economically

The production pipeline has already been validated. Run the full workflow directly for an ordinary “转录 URL” request.

When diagnosing code, model, download, or environment changes, process only 30–45 seconds first:

```bash
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --debug-seconds 45 --workdir /absolute/debug/path
```

Keep debug and full-run directories separate. Stop at the earliest relevant stage with `--stop-after` when possible. Never spend model tokens on a full-video diagnostic run.
When `--workdir` is omitted, put debug output under `output/<video-title>/_debug/start-<seconds>s_duration-<seconds>s`. Keep every artifact for the same video inside its title directory without conflicting with the production manifest.

## Preserve quality and timing

Use the pipeline defaults unless the user asks otherwise. They enforce these invariants:

- Download video and audio together in one `yt-dlp` task and preserve both original streams.
- Default yt-dlp to the `web_embedded` YouTube player client; override with `--youtube-player-client` only when a demonstrated video-specific restriction requires it.
- Retain raw English ASR and absolute word timestamps for audit, then correct repeated/broken cross-chunk grammar and align complete sentences back to real word boundaries.
- Run independent ASR chunks, TTS sentences, and per-window ffmpeg fitting concurrently while preserving ID order, atomic checkpoints, resumability, and per-segment caches.
- Load the local Whisper model once per run and retain its real word timestamps. The first run may download `medium.en`; subsequent runs reuse the local model cache.
- Complete English polishing, topic detection, Chinese translation, and overlong-line shortening through the calling Agent's current backend model, using the structured request/response files when the pipeline reaches a text stage.
- Preserve the polished English timestamps exactly in the Chinese transcript.
- Preserve real inter-sentence silence and fail explicitly if word timestamps are absent; never fall back to word-count/duration interpolation.
- Trim only leading/trailing TTS silence; preserve internal pauses.
- Include the hosted voice ID and synthesis controls in Alibaba Cloud cache keys; include the exact source-reference WAV hash for the optional local backend. Changing Chinese text, backend, model, voice, or relevant reference audio must regenerate the affected sentence.
- Shorten only Chinese sentences whose natural speech would exceed the maximum tempo, regenerate only those lines, and never silently fast-forward beyond the cap.
- Produce H.264/AAC video with an embedded default/forced `mov_text` Chinese subtitle stream compatible with QuickTime.
- Build the final AAC track from the high-quality retained source audio, never the 16 kHz mono transcription WAV. Preserve the Demucs background stem at stereo quality and duck it only while Chinese speech is active.
- Also produce `dubbed.zh.bilibili.mp4` with Chinese subtitles burned into the H.264 picture so video-platform transcoding cannot discard them; copy the already encoded AAC dub audio without regenerating speech.
- Use `Noto Sans CJK SC` for burned Chinese subtitles and visually inspect a subtitle-bearing frame; install `fonts-noto-cjk` on Debian/Ubuntu if glyphs render as boxes.

Do not manually edit timestamps or invoke independent download/transcription commands unless repairing a demonstrated pipeline defect.

## Verify before completion

After the command succeeds:

1. Confirm `video_metadata.json`, `dubbed.zh.mp4`, `dubbed.zh.bilibili.mp4`, `transcript.en.polished.json`, `transcript.zh.json`, `transcript.zh.srt`, and `sync_report.json` exist inside `output/<video-title>`.
2. Read `sync_report.json`; require zero warnings and no segment tempo above the configured cap.
3. Use `ffprobe` to require video, audio, and subtitle streams. Confirm H.264 video, AAC audio, and `mov_text` subtitle codecs.
4. Decode the full final file with `ffmpeg -v error -i dubbed.zh.mp4 -f null -`; require exit code 0.
5. Return clickable absolute paths for the final video, Chinese SRT, Chinese JSON audit, and sync report. State the maximum tempo and whether warnings were found.
6. After successful full-video transcription, also provide resumable `rsync` download commands for `dubbed.zh.mp4` on all three platforms below. Prefer a real SSH source in the form `SSH_USER@SSH_HOST:/absolute/path/dubbed.zh.mp4`. Only provide an `rsync://HOST/MODULE/path/dubbed.zh.mp4` URL when an rsync daemon and module are actually configured and reachable. Never invent a hostname, account, module, or public address. If SSH connection details are unavailable, keep the placeholders and state exactly which values the user must replace.

   Linux:

   ```bash
   rsync --partial --append-verify --progress -e ssh \
     'SSH_USER@SSH_HOST:/absolute/path/dubbed.zh.mp4' './dubbed.zh.mp4'
   ```

   macOS:

   ```bash
   rsync --partial --append-verify --progress -e ssh \
     'SSH_USER@SSH_HOST:/absolute/path/dubbed.zh.mp4' './dubbed.zh.mp4'
   ```

   Windows PowerShell with WSL and `rsync` installed inside WSL:

   ```powershell
   wsl rsync --partial --append-verify --progress -e ssh `
     "SSH_USER@SSH_HOST:/absolute/path/dubbed.zh.mp4" `
     "/mnt/c/Users/WINDOWS_USER/Downloads/dubbed.zh.mp4"
   ```

   Explain briefly that `--partial` keeps an interrupted local file and `--append-verify` resumes and verifies it when the same command is rerun. State that rsync-over-SSH requires a reachable SSH server, valid account/key access, and `rsync` installed at both ends; Windows additionally requires WSL for the documented command. When a genuine rsync daemon URL is available, also show it separately as `rsync://...`. Do not call an SSH source a web link, and do not offer these final-video commands for debug-only or download-only runs that did not produce `dubbed.zh.mp4`.

If validation fails, diagnose and resume the existing workdir. Do not claim completion until the final file passes these checks.
