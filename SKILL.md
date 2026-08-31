---
name: youtube-zh-dub
description: Download selectable-quality YouTube streams, or turn an English YouTube video into a timestamped Simplified Chinese dub that can clone each source speaker with local Fun-CosyVoice3 and embed subtitles. Use for YouTube 中文配音/中文翻译视频, complete dubbing workflows, or clear/best-quality YouTube downloads.
---

# YouTube 中文配音

Run the verified pipeline through `scripts/run_youtube_zh_dub.py`. Treat the command “转录 <YouTube Link>” as a request for the complete Chinese-dubbed video, not only a text transcript.
Set `SKILL_DIR` to this Skill package's root before using the examples below.

## Execute

For a fresh machine or a different agent environment, read the **“给其他 Agent 的快速部署”**
section in `README.md` before running the workflow. It defines the required separate
Skill/CosyVoice environments, a low-memory smoke test, and the exact resume rule.

1. Extract exactly one `youtube.com` or `youtu.be` URL from the request. Reject a missing or non-YouTube URL instead of guessing.
2. The defaults are local `faster-whisper medium.en` transcription (CPU INT8) and local `Fun-CosyVoice3-0.5B` source-voice TTS. Neither needs an API key. Require `OPENROUTER_API_KEY` only when `--tts-backend mai` or `--transcriber-backend openrouter-whisper1` is explicitly selected. Never expose or recover the key from plaintext. English polishing, topic detection, Chinese translation, and timing rewrites are completed by the Agent currently running this Skill, using its own configured backend model; they do not invoke a separate model CLI.
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
- A sanitized title that preserves readable Unicode while replacing filesystem-invalid characters and converting each run of title whitespace to one `-`. Append the video ID only if another video already occupies the same title.
- Maximum post-processing speed-up of `1.15x` and up to five translation-shortening attempts.
- Source-voice synthesis: extract each polished English sentence window from `source_audio.wav` and use it as that Chinese sentence's cross-lingual CosyVoice3 reference, so multi-speaker videos follow the current source speaker without manual voice labels.
- Memory-bounded local TTS: load one CosyVoice3 model and generate sentences sequentially. Use `--cosyvoice-threads` for CPU compute; do not create concurrent model copies. Four `--tts-workers` apply only to the optional MAI backend.
- Background preservation: use local Demucs `htdemucs` to remove the original English vocal stem, then sidechain-duck and mix the remaining stereo background under the Chinese dub. Cache both separation and mixing by content hash. Use `--background-mode none` only when the user explicitly wants the legacy voice-only output or Demucs is unavailable.
- The bundled verified source pipeline at `<skill-directory>/youtube_dub.py`.
- The project `.venv/bin` prepended to `PATH` when present.

Pass supported pipeline options after the URL. Examples:

```bash
# Use Chrome cookies only when the user authorizes it or YouTube requires login.
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --cookies-from-browser chrome

# Explicit alternate output directory.
python3 "$SKILL_DIR/scripts/run_youtube_zh_dub.py" URL --workdir /absolute/output/path
```

The local TTS backend discovers `COSYVOICE_ROOT`, `COSYVOICE_PYTHON`, and `COSYVOICE_MODEL`, or accepts the matching `--cosyvoice-root`, `--cosyvoice-python`, and `--cosyvoice-model` options. The default layout is a sibling `cosyvoice/` checkout, `miniconda-cosyvoice/bin/python`, and `cosyvoice/pretrained_models/Fun-CosyVoice3-0.5B`. Use `--tts-backend mai` when local CosyVoice is unavailable or the user explicitly prefers the hosted voice.

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
- Include the exact source-reference WAV hash in each local TTS cache key. Changing source audio, sentence boundaries, Chinese text, backend, or model must regenerate the affected sentence.
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
