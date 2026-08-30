#!/usr/bin/env python3
"""Create a timestamp-aligned Chinese dub for a YouTube video.

The pipeline is intentionally resumable.  During development it processes only
the first 45 seconds unless --full is supplied explicitly.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_URL = "https://www.youtube.com/watch?v=2cTDRKRQ5oc"
DEFAULT_TRANSCRIBER = "medium.en"
DEFAULT_TRANSCRIBER_BACKEND = "faster-whisper"
DEFAULT_WHISPER_DEVICE = "cpu"
DEFAULT_WHISPER_COMPUTE_TYPE = "int8"
DEFAULT_TTS_BACKEND = "cosyvoice3-source"
DEFAULT_TTS = "microsoft/mai-voice-2"
DEFAULT_VOICE = "zh-CN-Mei:MAI-Voice-2"
DEFAULT_TRANSCRIBE_WORKERS = 1
DEFAULT_TTS_WORKERS = 4
DEFAULT_FIT_WORKERS = 4
MAX_NETWORK_WORKERS = 16
POLISH_BATCH_MAX_CHARACTERS = 9_000
TRANSLATION_BATCH_MAX_CHARACTERS = 6_000
TEXT_BACKEND = "codex-cli"
TEXT_PIPELINE_VERSION = 3
TIMESTAMP_PIPELINE_VERSION = 1
ALIGNMENT_PIPELINE_VERSION = 2
BILIBILI_SUBTITLE_RENDER_VERSION = 2
STAGES = ("download", "transcribe", "polish", "translate", "synthesize", "mux")
QUALITY_VIDEO_FORMATS = {
    "best": "bestvideo",
    "720p": "bestvideo[height<=720]",
    "1080p": "bestvideo[height<=1080]",
}


class PipelineError(RuntimeError):
    """A user-facing pipeline failure."""


@dataclass(frozen=True)
class Segment:
    id: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class TimedWord:
    text: str
    start: float
    end: float


def log(message: str) -> None:
    print(f"[youtube-dub] {message}", flush=True)


def run(command: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """Run a command without invoking a shell."""
    printable = " ".join(
        re.sub(r"(https?://)[^/@\s]+@", r"\1***@", argument) for argument in command
    )
    log(f"$ {printable}")
    try:
        return subprocess.run(
            list(command),
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        if len(details) > 3000:
            details = details[-3000:]
        raise PipelineError(f"命令执行失败（退出码 {exc.returncode}）：{printable}\n{details}") from exc


def require_tools(args: argparse.Namespace) -> None:
    required = ["yt-dlp", "ffmpeg", "ffprobe"]
    if (
        not args.remux_only
        and not args.subtitles_only
        and stage_enabled(args.stop_after, "polish")
    ):
        required.append("codex")
    missing = [tool for tool in required if not shutil.which(tool)]
    if missing:
        raise PipelineError(f"缺少命令行工具：{', '.join(missing)}")


def probe_duration(path: Path) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise PipelineError(f"无法读取媒体时长：{path}") from exc


def probe_video_duration(path: Path) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return probe_duration(path)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_segments(document: dict[str, Any], language_key: str = "text") -> list[Segment]:
    segments: list[Segment] = []
    for item in document.get("segments", []):
        text = item.get(language_key, item.get("text", ""))
        segments.append(
            Segment(
                id=int(item["id"]),
                start=float(item["start"]),
                end=float(item["end"]),
                text=str(text).strip(),
            )
        )
    return segments


def parse_words(document: dict[str, Any]) -> list[TimedWord]:
    words: list[TimedWord] = []
    for item in document.get("words", []):
        text = str(item.get("text", item.get("word", ""))).strip()
        start = float(item["start"])
        end = float(item["end"])
        if not text or end <= start:
            continue
        words.append(TimedWord(text=text, start=start, end=end))
    return words


def seconds_to_srt(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def write_srt(path: Path, segments: Iterable[Segment]) -> None:
    blocks = []
    for number, segment in enumerate(segments, 1):
        blocks.append(
            f"{number}\n{seconds_to_srt(segment.start)} --> {seconds_to_srt(segment.end)}\n"
            f"{segment.text}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def openrouter_request(
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    expect_binary: bool = False,
    attempts: int = 3,
    timeout_seconds: float = 300,
) -> bytes | dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{OPENROUTER_BASE_URL}/{endpoint.lstrip('/')}",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/youtube-dub",
            "X-OpenRouter-Title": "Timestamp Aligned YouTube Dub",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read()
                if expect_binary:
                    return response_body
                return json.loads(response_body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            last_error = PipelineError(f"OpenRouter HTTP {exc.code}: {error_body[:2000]}")
            if exc.code not in (408, 409, 429, 500, 502, 503, 504):
                break
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
        if attempt < attempts:
            delay = 2 ** (attempt - 1)
            log(f"API 请求失败，{delay} 秒后重试（{attempt}/{attempts}）")
            time.sleep(delay)
    raise PipelineError(f"OpenRouter 请求失败：{last_error}")


def require_openrouter_api_key(purpose: str) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if api_key:
        return api_key
    raise PipelineError(
        f"{purpose}需要 OPENROUTER_API_KEY 环境变量。该 Key 只用于 MAI 转录或语音合成，"
        "不会用于英文校对、主题识别、中文翻译或超时译文精简。"
    )


def yt_dlp_common(args: argparse.Namespace) -> list[str]:
    command = [
        "yt-dlp",
        "--no-playlist",
        "--newline",
        "--http-chunk-size",
        "256K",
        "--throttled-rate",
        "50K",
    ]
    if shutil.which("node"):
        command.extend(
            ["--js-runtimes", "node", "--remote-components", "ejs:github"]
        )
    youtube_player_client = getattr(args, "youtube_player_client", "web_embedded")
    if youtube_player_client:
        command.extend(
            [
                "--extractor-args",
                f"youtube:player_client={youtube_player_client}",
            ]
        )
    if args.cookies_from_browser:
        command.extend(["--cookies-from-browser", args.cookies_from_browser])
    proxy = args.proxy or urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
    if proxy:
        # Both streams are fetched by yt-dlp's native downloader, so a single
        # proxy setting keeps metadata and signed media requests on one route.
        command.extend(["--proxy", proxy])
    return command


def probe_stream_types(path: Path) -> set[str]:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture=True,
    )
    document = json.loads(result.stdout)
    return {str(item["codec_type"]) for item in document.get("streams", [])}


def probe_video_codec(path: Path) -> str:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture=True,
    )
    codec = result.stdout.strip().lower()
    if not codec:
        raise PipelineError(f"没有在视频中找到可用的视频轨：{path}")
    return codec


def quicktime_video_options(codec: str) -> list[str]:
    if codec == "h264":
        return ["-c:v", "copy", "-tag:v", "avc1"]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-tag:v",
        "avc1",
    ]


def prepare_quicktime_subtitle(source: Path, destination: Path) -> Path:
    """Delay only a cue starting at zero so QuickTime receives an activation event."""
    content = source.read_text(encoding="utf-8")
    adjusted, replacements = re.subn(
        r"(?m)^00:00:00,000(?= --> )",
        "00:00:00,100",
        content,
        count=1,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(adjusted, encoding="utf-8")
    if replacements:
        log("QuickTime 字幕启动修复：首条字幕延后 100 毫秒")
    return destination


def escape_subtitle_filter_path(path: Path) -> str:
    """Escape a path for ffmpeg's subtitles filter (not for a shell)."""
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def find_standardized_stream(workdir: Path, prefix: str, kind: str) -> Path | None:
    matches = sorted(workdir.glob(f"{prefix}_{kind}_original.*"))
    return matches[0] if matches else None


def preserve_downloaded_streams(workdir: Path, prefix: str) -> tuple[Path, Path]:
    """Give yt-dlp's retained pure video/audio streams stable file names."""
    video_stream: Path | None = None
    audio_stream: Path | None = None
    candidates = sorted(workdir.glob(f"{prefix}.f*.*"))
    for candidate in candidates:
        if candidate.name.endswith(".part"):
            continue
        stream_types = probe_stream_types(candidate)
        if stream_types == {"video"}:
            video_stream = candidate
        elif stream_types == {"audio"}:
            audio_stream = candidate
    if not video_stream or not audio_stream:
        names = ", ".join(path.name for path in candidates) or "无"
        raise PipelineError(
            "yt-dlp 没有保留下完整的纯视频流和纯音频流。"
            f"找到的中间文件：{names}"
        )

    stable_video = workdir / f"{prefix}_video_original{video_stream.suffix}"
    stable_audio = workdir / f"{prefix}_audio_original{audio_stream.suffix}"
    video_stream.replace(stable_video)
    audio_stream.replace(stable_audio)
    log(f"保留 yt-dlp 纯视频流：{stable_video.name}")
    log(f"保留 yt-dlp 纯音频流：{stable_audio.name}")
    return stable_video, stable_audio


def make_transcription_wav(source: Path, destination: Path) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def build_download_command(
    args: argparse.Namespace,
    format_selector: str,
    output_template: Path,
) -> list[str]:
    command = yt_dlp_common(args) + [
        "-f",
        format_selector,
        "--keep-video",
        "--merge-output-format",
        "mp4",
        "--remux-video",
        "mp4",
    ]
    if args.force:
        command.append("--force-overwrites")
    command.extend(["-o", str(output_template), args.url])
    return command


def video_format_for_quality(quality: str) -> str:
    try:
        return QUALITY_VIDEO_FORMATS[quality]
    except KeyError as exc:
        choices = ", ".join(QUALITY_VIDEO_FORMATS)
        raise PipelineError(f"不支持的清晰度 {quality!r}；可选：{choices}") from exc


def download_media(args: argparse.Namespace, workdir: Path) -> tuple[Path, Path]:
    video = workdir / "source.mp4"
    audio = workdir / "source_audio.wav"
    workdir.mkdir(parents=True, exist_ok=True)
    video_format = args.video_format or video_format_for_quality(args.quality)
    audio_format = args.audio_format or "bestaudio"
    combined_format = f"{video_format}+{audio_format}"
    log(f"下载清晰度：{args.quality}（视频格式：{video_format}；音频格式：{audio_format}）")
    local_trim = not args.full
    prefix = "raw" if local_trim else "source"
    downloaded_video = workdir / f"{prefix}.mp4"
    downloaded_audio = workdir / f"{prefix}_audio.wav"
    retained_video = find_standardized_stream(workdir, prefix, "video")
    retained_audio = find_standardized_stream(workdir, prefix, "audio")
    if (
        video.exists()
        and audio.exists()
        and retained_video is not None
        and retained_audio is not None
        and not args.force
    ):
        log("复用 yt-dlp 已下载的纯视频流、纯音频流和处理文件")
        return video, audio
    needs_download = (
        args.force
        or not downloaded_video.exists()
        or retained_video is None
        or retained_audio is None
    )
    download_command = build_download_command(
        args,
        combined_format,
        downloaded_video.with_suffix(".%(ext)s"),
    )
    if needs_download:
        log("使用一次 yt-dlp 任务同时下载纯视频流和纯音频流")
        run(download_command)
        retained_video, retained_audio = preserve_downloaded_streams(workdir, prefix)
    assert retained_video is not None and retained_audio is not None

    if not downloaded_audio.exists() or args.force or needs_download:
        log("将 yt-dlp 下载的原始音频转换为转录用 WAV")
        make_transcription_wav(retained_audio, downloaded_audio)

    if local_trim:
        log("使用本地 ffmpeg 截取调试片段；模型不会接触全片")
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{args.start_seconds:.3f}",
                "-t",
                f"{args.debug_seconds:.3f}",
                "-i",
                str(downloaded_video),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
                str(video),
            ]
        )
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{args.start_seconds:.3f}",
                "-t",
                f"{args.debug_seconds:.3f}",
                "-i",
                str(downloaded_audio),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(audio),
            ]
        )

    if not video.exists() or not audio.exists():
        raise PipelineError("yt-dlp 已结束，但没有得到预期的 source.mp4 和 source_audio.wav")
    return video, audio


SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silence_midpoints(audio: Path) -> list[float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(audio),
            "-af",
            "silencedetect=noise=-35dB:d=0.30",
            "-f",
            "null",
            "-",
        ],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    starts: list[float] = []
    midpoints: list[float] = []
    for line in result.stderr.splitlines():
        start_match = SILENCE_START_RE.search(line)
        if start_match:
            starts.append(float(start_match.group(1)))
        end_match = SILENCE_END_RE.search(line)
        if end_match and starts:
            start = starts.pop(0)
            end = float(end_match.group(1))
            midpoints.append((start + end) / 2)
    return midpoints


def choose_boundaries(
    duration: float,
    silence_midpoints: Sequence[float],
    target_seconds: float,
    *,
    search_seconds: float = 4.0,
    minimum_seconds: float = 4.0,
) -> list[float]:
    """Create a complete, non-overlapping timeline, preferring nearby silences."""
    if duration <= 0:
        raise ValueError("duration must be positive")
    boundaries = [0.0]
    cursor = 0.0
    while duration - cursor > target_seconds + minimum_seconds / 2:
        target = cursor + target_seconds
        candidates = [
            point
            for point in silence_midpoints
            if cursor + minimum_seconds <= point < duration - minimum_seconds / 2
            and abs(point - target) <= search_seconds
        ]
        boundary = min(candidates, key=lambda point: abs(point - target)) if candidates else target
        if boundary <= cursor:
            boundary = min(duration, cursor + target_seconds)
        boundaries.append(round(boundary, 3))
        cursor = boundary
    boundaries.append(round(duration, 3))
    if len(boundaries) >= 3 and boundaries[-1] - boundaries[-2] < minimum_seconds / 2:
        boundaries.pop(-2)
    return boundaries


def extract_segment(source: Path, destination: Path, start: float, end: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def transcribe_segment(
    args: argparse.Namespace,
    audio: Path,
    segment_dir: Path,
    index: int,
    start: float,
    end: float,
    api_key: str,
) -> tuple[Segment, list[TimedWord]]:
    """Extract and transcribe one independent ASR segment."""
    chunk = segment_dir / f"segment_{index:04d}.wav"
    extract_segment(audio, chunk, start, end)
    encoded = base64.b64encode(chunk.read_bytes()).decode("ascii")
    response = openrouter_request(
        "audio/transcriptions",
        api_key,
        {
            "model": args.transcriber_model,
            "input_audio": {"data": encoded, "format": "wav"},
            "language": "en",
            "temperature": 0,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word"],
        },
        timeout_seconds=75,
    )
    assert isinstance(response, dict)
    text = str(response.get("text", "")).strip()
    if not text:
        raise PipelineError(f"转录模型没有为片段 {index} 返回文本")
    words = []
    for item in response.get("words", []):
        word = str(item.get("word", item.get("text", ""))).strip()
        word_start = start + float(item["start"])
        word_end = start + float(item["end"])
        if word and word_end > word_start:
            words.append(TimedWord(word, round(word_start, 3), round(word_end, 3)))
    if not words:
        raise PipelineError(
            f"片段 {index} 没有词级时间戳；禁止退回按字数估算时间轴"
        )
    return Segment(index, start, end, text), words


def transcribe_segment_locally(
    model: Any,
    audio: Path,
    segment_dir: Path,
    index: int,
    start: float,
    end: float,
) -> tuple[Segment, list[TimedWord]]:
    """Extract and transcribe one ASR segment with a shared faster-whisper model."""
    chunk = segment_dir / f"segment_{index:04d}.wav"
    extract_segment(audio, chunk, start, end)
    generated_segments, _info = model.transcribe(
        str(chunk),
        language="en",
        temperature=0,
        beam_size=5,
        word_timestamps=True,
        vad_filter=False,
    )
    local_segments = list(generated_segments)
    text = " ".join(item.text.strip() for item in local_segments if item.text.strip()).strip()
    if not text:
        raise PipelineError(f"本地转录模型没有为片段 {index} 返回文本")
    words: list[TimedWord] = []
    for segment in local_segments:
        for item in segment.words or ():
            word = str(item.word).strip()
            word_start = start + float(item.start)
            word_end = start + float(item.end)
            if word and word_end > word_start:
                words.append(TimedWord(word, round(word_start, 3), round(word_end, 3)))
    if not words:
        raise PipelineError(
            f"本地转录片段 {index} 没有词级时间戳；禁止退回按字数估算时间轴"
        )
    return Segment(index, start, end, text), words


def transcribe_audio(
    args: argparse.Namespace, workdir: Path, audio: Path
) -> list[Segment]:
    transcript_path = workdir / "transcript.en.json"
    cached_by_id: dict[int, Segment] = {}
    cached_words_by_chunk: dict[int, list[TimedWord]] = {}
    if transcript_path.exists() and not args.force:
        cached_document = read_json(transcript_path)
        cached_segments = parse_segments(cached_document)
        cached_words = parse_words(cached_document)
        compatible_cache = (
            cached_document.get("timestamp_pipeline_version") == TIMESTAMP_PIPELINE_VERSION
            and cached_document.get("backend") == args.transcriber_backend
            and cached_document.get("model") == args.transcriber_model
            and (
                args.transcriber_backend != "faster-whisper"
                or (
                    cached_document.get("device") == args.whisper_device
                    and cached_document.get("compute_type")
                    == args.whisper_compute_type
                )
            )
        )
        if (
            cached_document.get("complete")
            and compatible_cache
            and cached_words
        ):
            log("复用英文转录")
            return cached_segments
        if compatible_cache:
            cached_by_id = {item.id: item for item in cached_segments}
            for item in cached_document.get("words", []):
                chunk_id = int(item.get("chunk_id", -1))
                parsed = parse_words({"words": [item]})
                if parsed and chunk_id >= 0:
                    cached_words_by_chunk.setdefault(chunk_id, []).extend(parsed)
            log(f"发现未完成的词级英文转录，将从 {len(cached_segments)} 个已完成片段继续")
        else:
            log("旧英文转录不含兼容词级时间轴，将重新转录")

    duration = probe_duration(audio)
    silences = detect_silence_midpoints(audio)
    boundaries = choose_boundaries(duration, silences, args.chunk_seconds)
    segment_dir = workdir / "segments" / "english"
    total_segments = len(boundaries) - 1
    completed_by_id: dict[int, Segment] = {}
    completed_words_by_id: dict[int, list[TimedWord]] = {}
    pending: list[tuple[int, float, float]] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        cached = cached_by_id.get(index)
        if (
            cached
            and math.isclose(cached.start, start, abs_tol=0.01)
            and math.isclose(cached.end, end, abs_tol=0.01)
            and cached.text
            and cached_words_by_chunk.get(index)
        ):
            log(f"复用转录片段 {index + 1}/{total_segments}")
            completed_by_id[index] = cached
            completed_words_by_id[index] = cached_words_by_chunk[index]
            continue
        pending.append((index, start, end))

    def write_checkpoint() -> None:
        ordered_ids = sorted(completed_by_id)
        ordered = [completed_by_id[index] for index in ordered_ids]
        ordered_words = [
            {**asdict(word), "chunk_id": index}
            for index in ordered_ids
            for word in completed_words_by_id[index]
        ]
        write_json(
            transcript_path,
            {
                "source_url": args.url,
                "source_start_seconds": args.start_seconds,
                "duration": duration,
                "timestamp_basis": "seconds relative to source.mp4",
                "timestamp_method": (
                    f"{args.transcriber_backend} {args.transcriber_model} word timestamps "
                    "with absolute chunk offsets"
                ),
                "timestamp_pipeline_version": TIMESTAMP_PIPELINE_VERSION,
                "backend": args.transcriber_backend,
                "model": args.transcriber_model,
                "device": (
                    args.whisper_device
                    if args.transcriber_backend == "faster-whisper"
                    else None
                ),
                "compute_type": (
                    args.whisper_compute_type
                    if args.transcriber_backend == "faster-whisper"
                    else None
                ),
                "workers": args.transcribe_workers,
                "segments": [asdict(item) for item in ordered],
                "words": ordered_words,
                "complete": len(ordered) == total_segments,
            },
        )

    if pending:
        api_key: str | None = None
        local_model: Any = None
        if args.transcriber_backend == "openrouter-whisper1":
            api_key = require_openrouter_api_key("OpenAI whisper-1 英文转录")
        else:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise PipelineError(
                    "本地转录需要 faster-whisper；请先安装项目依赖"
                ) from exc
            log(
                f"加载本地 Whisper 模型 {args.transcriber_model} "
                f"（{args.whisper_device}/{args.whisper_compute_type}）"
            )
            local_model = WhisperModel(
                args.transcriber_model,
                device=args.whisper_device,
                compute_type=args.whisper_compute_type,
                cpu_threads=args.whisper_cpu_threads,
            )
        worker_count = min(args.transcribe_workers, len(pending))
        log(f"并发转录 {len(pending)} 个片段（{worker_count} 个线程）")
        failures: list[tuple[int, Exception]] = []
        futures: dict[Future[tuple[Segment, list[TimedWord]]], int] = {}
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="youtube-dub-asr"
        ) as executor:
            for index, start, end in pending:
                if args.transcriber_backend == "faster-whisper":
                    future = executor.submit(
                        transcribe_segment_locally,
                        local_model,
                        audio,
                        segment_dir,
                        index,
                        start,
                        end,
                    )
                else:
                    assert api_key is not None
                    future = executor.submit(
                        transcribe_segment,
                        args,
                        audio,
                        segment_dir,
                        index,
                        start,
                        end,
                        api_key,
                    )
                futures[future] = index
            for future in as_completed(futures):
                index = futures[future]
                try:
                    segment, words = future.result()
                except Exception as exc:  # Preserve other successful checkpoints.
                    failures.append((index, exc))
                    log(f"转录片段 {index + 1}/{total_segments} 失败：{exc}")
                    continue
                completed_by_id[index] = segment
                completed_words_by_id[index] = words
                log(
                    f"转录完成 {len(completed_by_id)}/{total_segments}："
                    f"片段 {index + 1} [{segment.start:.2f}, {segment.end:.2f}]"
                )
                write_checkpoint()
        if failures:
            failed_ids = ", ".join(str(index) for index, _ in failures[:12])
            raise PipelineError(
                f"并发英文转录有 {len(failures)} 个片段失败（ID：{failed_ids}）；"
                f"成功片段已保存，可直接重跑。首个错误：{failures[0][1]}"
            )

    write_checkpoint()
    segments = [completed_by_id[index] for index in range(total_segments)]
    write_srt(workdir / "transcript.en.srt", segments)
    return segments


def parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
    if not isinstance(content, str):
        raise PipelineError("文本模型返回了无法识别的内容格式")
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"文本模型没有返回有效 JSON：{content[:1000]}") from exc
    if isinstance(value, list):
        return {"segments": value}
    if not isinstance(value, dict):
        raise PipelineError("文本模型返回的 JSON 顶层不是对象")
    return value


def text_model_name(args: argparse.Namespace) -> str:
    return args.text_model or "Codex CLI default"


def codex_json_completion(
    args: argparse.Namespace,
    system: str,
    user: str,
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    """Run text-only work through Codex CLI, never through OpenRouter."""
    executable = shutil.which("codex")
    if not executable:
        raise PipelineError(
            "缺少 codex 命令。英文校对、主题识别、中文翻译和超时精简需要"
            "已登录的本机 Codex CLI。"
        )

    prompt = (
        "Complete this text transformation without reading files, using tools, or making network "
        "requests yourself. Follow the editorial instructions exactly.\n\n"
        f"EDITORIAL INSTRUCTIONS:\n{system}\n\nTASK INPUT:\n{user}\n\n"
        "Return only the JSON value required by the supplied output schema."
    )
    with tempfile.TemporaryDirectory(prefix="youtube-dub-codex-") as folder:
        temporary_dir = Path(folder)
        schema_path = temporary_dir / "output.schema.json"
        output_path = temporary_dir / "result.json"
        schema_path.write_text(
            json.dumps(output_schema, ensure_ascii=False), encoding="utf-8"
        )
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if args.text_model:
            command.extend(["--model", args.text_model])
        command.append("-")

        environment = os.environ.copy()
        # The text subprocess must not receive OpenRouter credentials or an
        # environment override that could redirect the OpenAI client there.
        for name in (
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_API_HOST",
        ):
            environment.pop(name, None)
        log(f"调用本机 Codex CLI 文本模型（{text_model_name(args)}）")
        try:
            completed = subprocess.run(
                command,
                cwd=temporary_dir,
                env=environment,
                input=prompt,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip()
            if len(details) > 3000:
                details = details[-3000:]
            raise PipelineError(f"Codex CLI 文本处理失败：{details}") from exc
        if not output_path.exists():
            details = (completed.stderr or completed.stdout or "").strip()
            raise PipelineError(f"Codex CLI 没有生成结构化结果：{details[-2000:]}")
        return parse_json_content(output_path.read_text(encoding="utf-8"))


def segment_translation_schema(*, include_topic: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "zh": {"type": "string"}},
                "required": ["id", "zh"],
                "additionalProperties": False,
            },
        }
    }
    required = ["segments"]
    if include_topic:
        properties["topic"] = {
            "type": "object",
            "properties": {
                "english": {"type": "string"},
                "chinese": {"type": "string"},
                "expertise": {"type": "string"},
            },
            "required": ["english", "chinese", "expertise"],
            "additionalProperties": False,
        }
        required.insert(0, "topic")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def segments_fingerprint(segments: Sequence[Segment]) -> str:
    payload = [asdict(item) for item in segments]
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def json_fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_text_batch_cache(
    args: argparse.Namespace,
    path: Path,
    *,
    source_fingerprint: str,
    request_fingerprint: str,
) -> dict[str, Any] | None:
    """Load a batch only when every input that can affect its text still matches."""
    if args.force or not path.exists():
        return None
    cached = read_json(path)
    if not (
        cached.get("source_fingerprint") == source_fingerprint
        and cached.get("request_fingerprint") == request_fingerprint
        and cached.get("text_backend") == TEXT_BACKEND
        and cached.get("text_pipeline_version") == TEXT_PIPELINE_VERSION
        and cached.get("model") == text_model_name(args)
    ):
        return None
    result = cached.get("result")
    return result if isinstance(result, dict) and result else None


def write_text_batch_cache(
    args: argparse.Namespace,
    path: Path,
    *,
    source_fingerprint: str,
    request_fingerprint: str,
    result: dict[str, Any],
) -> None:
    write_json(
        path,
        {
            "source_fingerprint": source_fingerprint,
            "request_fingerprint": request_fingerprint,
            "text_backend": TEXT_BACKEND,
            "text_pipeline_version": TEXT_PIPELINE_VERSION,
            "model": text_model_name(args),
            "result": result,
        },
    )


def english_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+(?:[+'’-][A-Za-z0-9]+)*", text))


def english_tokens(text: str) -> list[str]:
    return [
        token.replace("’", "'").lower()
        for token in re.findall(r"[A-Za-z0-9]+(?:[+'’-][A-Za-z0-9]+)*", text)
    ]


def repair_collapsed_sentence_windows(
    aligned: list[Segment],
    sentence_tokens: Sequence[Sequence[str]],
    usable_words: Sequence[TimedWord],
) -> list[Segment]:
    """Redistribute impossible sentence windows using only real word boundaries."""
    aligned = list(aligned)
    minimum_seconds_per_token = 0.08
    collapsed = {
        index
        for index, (segment, tokens) in enumerate(zip(aligned, sentence_tokens))
        if segment.duration < max(0.12, len(tokens) * minimum_seconds_per_token)
    }
    while collapsed:
        first_index = min(collapsed)
        last_collapsed = first_index
        while last_collapsed + 1 in collapsed:
            last_collapsed += 1
        repair_end = last_collapsed + 1
        if repair_end >= len(aligned):
            raise PipelineError(
                f"校对句子 {first_index + 1} 起的时间窗塌缩，且缺少可靠的后续词边界"
            )
        next_reliable = repair_end + 1
        while next_reliable < len(aligned) and next_reliable in collapsed:
            next_reliable += 1
        window_start = aligned[first_index].start
        window_end = (
            aligned[next_reliable].start
            if next_reliable < len(aligned)
            else aligned[repair_end].end
        )
        window_words = [
            word
            for word in usable_words
            if word.start >= window_start - 0.001 and word.end <= window_end + 0.001
        ]
        repair_indexes = list(range(first_index, repair_end + 1))
        token_counts = [len(sentence_tokens[index]) for index in repair_indexes]
        if len(window_words) < len(repair_indexes) or sum(token_counts) <= 0:
            raise PipelineError(
                f"校对句子 {first_index + 1} 起的时间窗塌缩，Whisper 词边界不足以安全修复"
            )
        cursor_word = 0
        remaining_tokens = sum(token_counts)
        for offset, (sentence_index, token_count) in enumerate(zip(repair_indexes, token_counts)):
            remaining_sentences = len(repair_indexes) - offset - 1
            if remaining_sentences:
                available = len(window_words) - cursor_word
                take = round(available * token_count / remaining_tokens)
                take = max(1, min(take, available - remaining_sentences))
            else:
                take = len(window_words) - cursor_word
            chosen = window_words[cursor_word : cursor_word + take]
            aligned[sentence_index] = Segment(
                aligned[sentence_index].id,
                round(chosen[0].start, 3),
                round(chosen[-1].end, 3),
                aligned[sentence_index].text,
            )
            cursor_word += take
            remaining_tokens -= token_count
        collapsed.difference_update(repair_indexes)
    return aligned


def align_sentences_to_timed_words(
    raw_words: Sequence[TimedWord], sentences: Sequence[str]
) -> list[Segment]:
    """Align corrected sentences to ASR words without inventing a continuous timeline."""
    if not raw_words or not sentences:
        raise PipelineError("词级时间轴或校对句子为空，无法进行强制词序对齐")
    raw_tokens = [english_tokens(word.text)[0] for word in raw_words if english_tokens(word.text)]
    usable_words = [word for word in raw_words if english_tokens(word.text)]
    sentence_tokens = [english_tokens(sentence) for sentence in sentences]
    if not raw_tokens or any(not tokens for tokens in sentence_tokens):
        raise PipelineError("词级时间轴或校对句子不含可对齐的英文单词")
    corrected_tokens = [token for tokens in sentence_tokens for token in tokens]
    mapping: list[int | None] = [None] * len(corrected_tokens)
    matcher = difflib.SequenceMatcher(None, raw_tokens, corrected_tokens, autojunk=False)
    if matcher.ratio() < 0.65:
        raise PipelineError(
            "校对文本与原始 ASR 差异过大，无法可靠映射到真实词级时间轴；"
            "请检查校对结果，禁止使用估算时间戳继续"
        )
    for tag, raw_start, raw_end, corrected_start, corrected_end in matcher.get_opcodes():
        corrected_size = corrected_end - corrected_start
        raw_size = raw_end - raw_start
        if tag == "delete" or not corrected_size:
            continue
        if tag == "equal":
            for offset in range(corrected_size):
                mapping[corrected_start + offset] = raw_start + offset
        elif raw_size:
            for offset in range(corrected_size):
                position = min(raw_size - 1, int(offset * raw_size / corrected_size))
                mapping[corrected_start + offset] = raw_start + position
        else:
            anchor = min(len(usable_words) - 1, max(0, raw_start - 1))
            for offset in range(corrected_size):
                mapping[corrected_start + offset] = anchor
    last = 0
    for index, value in enumerate(mapping):
        if value is None:
            mapping[index] = last
        else:
            last = value
    last = len(usable_words) - 1
    for index in range(len(mapping) - 1, -1, -1):
        value = mapping[index]
        if value is None:
            mapping[index] = last
        else:
            last = value

    aligned: list[Segment] = []
    cursor = 0
    for sentence_id, (sentence, tokens) in enumerate(zip(sentences, sentence_tokens)):
        indexes = [int(value) for value in mapping[cursor : cursor + len(tokens)] if value is not None]
        cursor += len(tokens)
        if not indexes:
            raise PipelineError(f"校对句子 {sentence_id + 1} 无法映射到原音频单词")
        first = usable_words[min(indexes)]
        last_word = usable_words[max(indexes)]
        aligned.append(
            Segment(sentence_id, round(first.start, 3), round(last_word.end, 3), sentence.strip())
        )

    # Corrections can map two neighboring sentences onto the same ASR word. Split only
    # such overlaps; preserve genuine inter-sentence silence exactly as returned by ASR.
    for index in range(1, len(aligned)):
        previous = aligned[index - 1]
        current = aligned[index]
        if previous.end > current.start:
            boundary = round((max(previous.start, current.start) + min(previous.end, current.end)) / 2, 3)
            aligned[index - 1] = Segment(
                previous.id, previous.start, boundary, previous.text
            )
            aligned[index] = Segment(current.id, boundary, current.end, current.text)
        if aligned[index].end <= aligned[index].start:
            raise PipelineError(f"校对句子 {index + 1} 的真实词级时间窗无效")

    # SequenceMatcher can occasionally anchor a block of heavily rewritten text
    # to one repeated word.  That produces technically monotonic but impossible
    # windows (for example, several full sentences squeezed into milliseconds).
    # Repair only those collapsed runs, and put every new boundary on an actual
    # Whisper word boundary inside the span ending at the next reliable sentence.
    return repair_collapsed_sentence_windows(aligned, sentence_tokens, usable_words)


def batch_english_for_polish(
    segments: Sequence[Segment],
    maximum_characters: int = POLISH_BATCH_MAX_CHARACTERS,
) -> list[list[Segment]]:
    """Bound Codex output size while avoiding arbitrary ASR chunk boundaries."""
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        current.append(segment)
        size += len(segment.text) + 80
        if size >= maximum_characters and re.search(
            r"[.!?][\"'”’)]*$", segment.text.strip()
        ):
            batches.append(current)
            current = []
            size = 0
    if current:
        batches.append(current)
    return batches


def polish_transcript(
    args: argparse.Namespace,
    workdir: Path,
    raw_english: Sequence[Segment],
    raw_words: Sequence[TimedWord],
) -> list[Segment]:
    output_path = workdir / "transcript.en.polished.json"
    source_fingerprint = json_fingerprint(
        {
            "segments": [asdict(item) for item in raw_english],
            "words": [asdict(item) for item in raw_words],
            "timestamp_pipeline_version": TIMESTAMP_PIPELINE_VERSION,
            "alignment_pipeline_version": ALIGNMENT_PIPELINE_VERSION,
        }
    )
    if output_path.exists() and not args.force:
        cached = read_json(output_path)
        if (
            cached.get("source_fingerprint") == source_fingerprint
            and cached.get("complete")
            and cached.get("text_backend") == TEXT_BACKEND
            and cached.get("text_pipeline_version") == TEXT_PIPELINE_VERSION
            and cached.get("model") == text_model_name(args)
        ):
            log("复用已校对并按完整句子重分段的英文稿")
            return parse_segments(cached)
        log("英文校对缓存不是当前 Codex 文本后端生成，将重新校对")

    system = (
        "You are a meticulous English transcript copy editor. The ASR chunks were cut at arbitrary "
        "times, often in the middle of a sentence or word. Join all chunks into one continuous "
        "transcript before editing. Fix boundary artifacts, repeated or overlapping words, obvious "
        "ASR mistakes, missing function words required by grammar, capitalization, and punctuation. "
        "For example, 'The big' followed by 'Biggest mistake' must become 'The biggest mistake', not "
        "'The big Biggest mistake'. Do not summarize, paraphrase, translate, reorder, censor, or add "
        "new claims. Preserve names, product names, numbers, and the speaker's conversational tone. "
        "Return JSON only with two fields: sentences, an array containing the complete corrected "
        "sentences in order; and corrections, an array of objects with before, after, and reason for "
        "material grammar/ASR repairs. Every sentences item must be a complete sentence ending in "
        "., ?, or !."
    )
    output_schema = {
        "type": "object",
        "properties": {
            "sentences": {"type": "array", "items": {"type": "string"}},
            "corrections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "before": {"type": "string"},
                        "after": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["before", "after", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["sentences", "corrections"],
        "additionalProperties": False,
    }
    batches = batch_english_for_polish(raw_english)
    batch_dir = workdir / "segments" / "english_polished_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    sentences: list[str] = []
    corrections: list[Any] = []
    log(f"跨片段检查英文语法、重复词和不完整句子（{len(batches)} 批）")
    for batch_number, batch in enumerate(batches, 1):
        source = [{"chunk_id": item.id, "text": item.text} for item in batch]
        user_prompt = (
            "Correct and re-segment this transcript batch:\n"
            + json.dumps(source, ensure_ascii=False)
        )
        batch_path = batch_dir / f"batch_{batch_number - 1:04d}.json"
        batch_fingerprint = segments_fingerprint(batch)
        request_fingerprint = json_fingerprint(
            {"system": system, "user": user_prompt, "schema": output_schema}
        )
        value = load_text_batch_cache(
            args,
            batch_path,
            source_fingerprint=batch_fingerprint,
            request_fingerprint=request_fingerprint,
        )
        if value is not None:
            log(f"复用英文校对批次 {batch_number}/{len(batches)}")
            generated = False
        else:
            log(f"英文校对批次 {batch_number}/{len(batches)}")
            value = codex_json_completion(args, system, user_prompt, output_schema)
            generated = True
        sentence_values = value.get("sentences")
        if not isinstance(sentence_values, list) or not sentence_values:
            raise PipelineError(f"英文校对批次 {batch_number} 没有返回 sentences 数组")
        batch_sentences = [
            str(item).strip() for item in sentence_values if str(item).strip()
        ]
        if len(batch_sentences) != len(sentence_values):
            raise PipelineError(f"英文校对批次 {batch_number} 返回了空句子")
        sentences.extend(batch_sentences)
        batch_corrections = value.get("corrections", [])
        if isinstance(batch_corrections, list):
            corrections.extend(batch_corrections)
        if generated:
            write_text_batch_cache(
                args,
                batch_path,
                source_fingerprint=batch_fingerprint,
                request_fingerprint=request_fingerprint,
                result=value,
            )
    incomplete = [text for text in sentences if not re.search(r"[.!?][\"'”’)]*$", text)]
    if incomplete:
        raise PipelineError(f"英文校对结果仍含不完整句子：{incomplete[0]}")

    raw_word_count = sum(english_word_count(item.text) for item in raw_english)
    corrected_word_count = sum(english_word_count(text) for text in sentences)
    ratio = corrected_word_count / max(1, raw_word_count)
    if not 0.85 <= ratio <= 1.15:
        raise PipelineError(
            f"英文校对前后词数变化异常（{raw_word_count} -> {corrected_word_count}，比例 {ratio:.2f}）"
        )

    polished = align_sentences_to_timed_words(raw_words, sentences)
    write_json(
        output_path,
        {
            "source_url": args.url,
            "source_fingerprint": source_fingerprint,
            "timestamp_basis": "corrected sentences aligned to Whisper word start/end timestamps",
            "timestamp_pipeline_version": TIMESTAMP_PIPELINE_VERSION,
            "alignment_pipeline_version": ALIGNMENT_PIPELINE_VERSION,
            "text_backend": TEXT_BACKEND,
            "text_pipeline_version": TEXT_PIPELINE_VERSION,
            "model": text_model_name(args),
            "grammar_check": True,
            "complete_sentences": True,
            "word_count_before": raw_word_count,
            "word_count_after": corrected_word_count,
            "corrections": corrections,
            "segments": [asdict(item) for item in polished],
            "complete": True,
        },
    )
    write_srt(workdir / "transcript.en.polished.srt", polished)
    return polished


def batch_segments(
    segments: Sequence[Segment],
    maximum_characters: int = TRANSLATION_BATCH_MAX_CHARACTERS,
) -> list[list[Segment]]:
    batches: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        estimated = len(segment.text) + 80
        if current and size + estimated > maximum_characters:
            batches.append(current)
            current = []
            size = 0
        current.append(segment)
        size += estimated
    if current:
        batches.append(current)
    return batches


def topic_context(segments: Sequence[Segment], maximum_characters: int = 16_000) -> str:
    """Return representative text from the beginning, middle, and end."""
    transcript = "\n".join(segment.text for segment in segments)
    if len(transcript) <= maximum_characters:
        return transcript
    section = maximum_characters // 3
    middle_start = max(0, len(transcript) // 2 - section // 2)
    return (
        transcript[:section]
        + "\n[... middle sample ...]\n"
        + transcript[middle_start : middle_start + section]
        + "\n[... ending sample ...]\n"
        + transcript[-section:]
    )


def validate_translations(batch: Sequence[Segment], value: dict[str, Any]) -> dict[int, str]:
    items = value.get("segments")
    if not isinstance(items, list):
        raise PipelineError("翻译 JSON 缺少 segments 数组")
    translated: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict) or "id" not in item or "zh" not in item:
            raise PipelineError("翻译片段必须包含 id 和 zh")
        translated[int(item["id"])] = str(item["zh"]).strip()
    expected = {segment.id for segment in batch}
    if set(translated) != expected:
        raise PipelineError(
            f"翻译片段 ID 不一致；期望 {sorted(expected)}，实际 {sorted(translated)}"
        )
    if any(not translated[item_id] for item_id in expected):
        raise PipelineError("翻译模型返回了空译文")
    return translated


def translate_segments(
    args: argparse.Namespace, workdir: Path, english: Sequence[Segment]
) -> list[Segment]:
    output_path = workdir / "transcript.zh.json"
    source_fingerprint = segments_fingerprint(english)
    if output_path.exists() and not args.force:
        cached = read_json(output_path)
        if (
            cached.get("source_fingerprint") == source_fingerprint
            and cached.get("text_backend") == TEXT_BACKEND
            and cached.get("text_pipeline_version") == TEXT_PIPELINE_VERSION
            and cached.get("model") == text_model_name(args)
        ):
            log("复用中文翻译")
            return parse_segments(cached)
        log("英文稿或 Codex 文本后端已变化，将重新生成中文翻译")

    system = (
        "You are a senior domain expert, Chinese localization editor, and dubbing script adapter. "
        "First infer the video's subject from the transcript. Translate English into accurate, natural "
        "Simplified Chinese using terminology appropriate to that subject. Preserve every segment id. "
        "Do not merge, split, omit, or add segments. Each source segment is one complete sentence, so "
        "each Chinese segment must also be a complete sentence ending with appropriate sentence-final "
        "punctuation. Keep each Chinese line concise enough to be spoken inside its source time window. "
        "Do not include timestamps. Return JSON only."
    )
    topic: dict[str, Any] | None = None
    translated_by_id: dict[int, str] = {}
    batches = batch_segments(english)
    batch_dir = workdir / "segments" / "chinese_translation_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for batch_number, batch in enumerate(batches, 1):
        source = [{"id": item.id, "english": item.text} for item in batch]
        if topic is None:
            instruction = (
                "Return an object with: topic (an object containing english, chinese, and expertise), "
                "and segments (an array of objects containing exactly id and zh). Infer the topic from "
                "this representative context sampled across the complete transcript:\n"
                + topic_context(english)
                + "\n\nTranslate this batch:\n"
            )
        else:
            instruction = (
                f"The established topic is {json.dumps(topic, ensure_ascii=False)}. Return an object with "
                "segments (an array of objects containing exactly id and zh). Transcript:\n"
            )
        user_prompt = instruction + json.dumps(source, ensure_ascii=False)
        output_schema = segment_translation_schema(include_topic=topic is None)
        batch_path = batch_dir / f"batch_{batch_number - 1:04d}.json"
        batch_fingerprint = segments_fingerprint(batch)
        request_fingerprint = json_fingerprint(
            {"system": system, "user": user_prompt, "schema": output_schema}
        )
        value = load_text_batch_cache(
            args,
            batch_path,
            source_fingerprint=batch_fingerprint,
            request_fingerprint=request_fingerprint,
        )
        if value is not None:
            log(f"复用翻译批次 {batch_number}/{len(batches)}")
            generated = False
        else:
            log(f"翻译批次 {batch_number}/{len(batches)}")
            value = codex_json_completion(
                args,
                system,
                user_prompt,
                output_schema,
            )
            generated = True
        if topic is None:
            candidate = value.get("topic")
            if not isinstance(candidate, dict):
                raise PipelineError("首个翻译批次没有返回 topic 对象")
            topic = candidate
        translated_by_id.update(validate_translations(batch, value))
        if generated:
            write_text_batch_cache(
                args,
                batch_path,
                source_fingerprint=batch_fingerprint,
                request_fingerprint=request_fingerprint,
                result=value,
            )

    chinese = [Segment(item.id, item.start, item.end, translated_by_id[item.id]) for item in english]
    incomplete = [
        item.text
        for item in chinese
        if not re.search(r"(?:[。！？.!?]|…{1,2})[\"'”’）)】]*$", item.text)
    ]
    if incomplete:
        raise PipelineError(f"中文翻译结果仍含不完整句子：{incomplete[0]}")
    write_json(
        output_path,
        {
            "source_url": args.url,
            "source_start_seconds": args.start_seconds,
            "source_fingerprint": source_fingerprint,
            "timestamp_basis": "copied exactly from transcript.en.polished.json",
            "text_backend": TEXT_BACKEND,
            "text_pipeline_version": TEXT_PIPELINE_VERSION,
            "model": text_model_name(args),
            "topic": topic,
            "segments": [asdict(item) for item in chinese],
        },
    )
    write_srt(workdir / "transcript.zh.srt", chinese)
    return chinese


def atempo_chain(factor: float) -> str:
    """Return one or more ffmpeg atempo filters for an arbitrary positive factor."""
    if factor <= 0:
        raise ValueError("tempo factor must be positive")
    filters: list[float] = []
    while factor > 2.0:
        filters.append(2.0)
        factor /= 2.0
    while factor < 0.5:
        filters.append(0.5)
        factor /= 0.5
    filters.append(factor)
    return ",".join(f"atempo={item:.8f}" for item in filters)


def translation_character_count(text: str) -> int:
    """Count spoken-content characters while ignoring spaces and punctuation."""
    return len(re.sub(r"[\s，。！？：；、,.!?;:'\"“”‘’（）()《》【】…—-]", "", text))


def tts_cache_key(args: argparse.Namespace, text: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "text": text,
                "backend": args.tts_backend,
                "model": args.tts_model,
                "voice": args.voice,
                "speed": args.tts_speed,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cosyvoice_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    root_value = args.cosyvoice_root or os.environ.get("COSYVOICE_ROOT")
    root = Path(root_value).expanduser() if root_value else Path(__file__).resolve().parent.parent / "cosyvoice"
    python_value = args.cosyvoice_python or os.environ.get("COSYVOICE_PYTHON")
    python = Path(python_value).expanduser() if python_value else root.parent / "miniconda-cosyvoice" / "bin" / "python"
    model_value = args.cosyvoice_model or os.environ.get("COSYVOICE_MODEL")
    model = Path(model_value).expanduser() if model_value else root / "pretrained_models" / "Fun-CosyVoice3-0.5B"
    worker = Path(__file__).resolve().parent / "scripts" / "run_cosyvoice3_source_tts.py"
    return root.resolve(), python.resolve(), model.resolve(), worker


def tts_audit_model(args: argparse.Namespace) -> str:
    if args.tts_backend == "cosyvoice3-source":
        return cosyvoice_paths(args)[2].name
    return args.tts_model


def tts_audit_voice(args: argparse.Namespace) -> str:
    if args.tts_backend == "cosyvoice3-source":
        return "per-segment source voice reference"
    return args.voice


def source_reference_audio(workdir: Path, segment: Segment) -> Path:
    reference_dir = workdir / "segments" / "source_voice_reference"
    reference_dir.mkdir(parents=True, exist_ok=True)
    source = workdir / "source_audio.wav"
    source_stat = source.stat()
    reference_key = hashlib.sha256(
        f"{source.resolve()}:{source_stat.st_size}:{source_stat.st_mtime_ns}:"
        f"{segment.start:.3f}:{segment.end:.3f}".encode("utf-8")
    ).hexdigest()[:12]
    reference = reference_dir / f"segment_{segment.id:04d}_{reference_key}.wav"
    if not reference.exists():
        extract_segment(source, reference, segment.start, segment.end)
    return reference


def prepare_cosyvoice3_sources(
    args: argparse.Namespace,
    workdir: Path,
    segments: Sequence[Segment],
    raw_dir: Path,
    trimmed_dir: Path,
    *,
    force: bool = False,
) -> dict[int, tuple[Path, float, str]]:
    root, python, model, worker = cosyvoice_paths(args)
    required = (root / "cosyvoice", root / "third_party" / "Matcha-TTS", model, python, worker)
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise PipelineError("CosyVoice3 本地后端缺少：" + "、".join(missing))

    jobs: list[dict[str, Any]] = []
    outputs: dict[int, tuple[Path, Path, str]] = {}
    model_marker = model / "llm.pt"
    model_stat = model_marker.stat() if model_marker.exists() else model.stat()
    model_identity = (
        f"{model.resolve()}:{model_stat.st_size}:{model_stat.st_mtime_ns}"
    )
    for segment in segments:
        reference = source_reference_audio(workdir, segment)
        cache_key = hashlib.sha256(
            f"{tts_cache_key(args, segment.text)}:{model_identity}:"
            f"{file_sha256(reference)}".encode("utf-8")
        ).hexdigest()[:12]
        raw = raw_dir / f"segment_{segment.id:04d}_{cache_key}.wav"
        trimmed = trimmed_dir / f"segment_{segment.id:04d}_{cache_key}_trim_v1.wav"
        if force:
            raw.unlink(missing_ok=True)
            trimmed.unlink(missing_ok=True)
        outputs[segment.id] = (raw, trimmed, cache_key)
        if not raw.exists():
            jobs.append({
                "id": segment.id,
                "text": segment.text,
                "reference_audio": str(reference),
                "output": str(raw),
            })

    if jobs:
        jobs_path = workdir / "segments" / "cosyvoice3_jobs.json"
        write_json(jobs_path, jobs)
        log(f"顺序生成 {len(jobs)} 个源音色 CosyVoice3 片段（单模型实例）")
        run([
            str(python), str(worker),
            "--cosyvoice-root", str(root),
            "--model", str(model),
            "--jobs", str(jobs_path),
            "--threads", str(args.cosyvoice_threads),
        ])

    prepared = {}
    for segment in segments:
        raw, trimmed, cache_key = outputs[segment.id]
        if not raw.exists():
            raise PipelineError(f"CosyVoice3 未生成片段 {segment.id}")
        if not trimmed.exists() or trimmed.stat().st_mtime < raw.stat().st_mtime:
            trim_tts_edge_silence(raw, trimmed)
        prepared[segment.id] = (trimmed, probe_duration(trimmed), cache_key)
    return prepared


def trim_tts_edge_silence(source: Path, destination: Path) -> None:
    """Remove only leading/trailing digital silence, preserving pauses inside speech."""
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            (
                "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-50dB,"
                "areverse,"
                "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-50dB,"
                "areverse"
            ),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )
    # Codec/frame rounding can put a valid 100 ms utterance just below 0.100 s.
    if probe_duration(destination) < 0.09:
        raise PipelineError(f"TTS 静音裁剪后没有得到有效语音：{source}")


def ensure_tts_source(
    args: argparse.Namespace,
    segment: Segment,
    raw_dir: Path,
    trimmed_dir: Path,
    *,
    force: bool = False,
) -> tuple[Path, float, str]:
    cache_key = tts_cache_key(args, segment.text)
    raw = raw_dir / f"segment_{segment.id:04d}_{cache_key}.mp3"
    trimmed = trimmed_dir / f"segment_{segment.id:04d}_{cache_key}_trim_v1.wav"
    # Very short acknowledgement slots can be shorter than the TTS model's
    # minimum utterance.  Rewriting a one-character filler cannot make it any
    # shorter, so preserve it in the subtitle transcript and leave its audio
    # slot silent instead of violating the global tempo cap.
    if (
        segment.duration < 0.5
        and re.fullmatch(
            r"(?:嗯|唔|呃|啊|哦|唉|哎|对|是的|没错)[。！？.!?]?",
            segment.text.strip(),
        )
    ):
        cache_key = hashlib.sha256(
            f"nonverbal-silence-v1:{segment.duration:.6f}".encode("utf-8")
        ).hexdigest()[:12]
        trimmed = trimmed_dir / (
            f"segment_{segment.id:04d}_{cache_key}_nonverbal_silence.wav"
        )
        if not trimmed.exists() or force:
            make_silence(trimmed, segment.duration)
        return trimmed, segment.duration, cache_key
    if not raw.exists() or force:
        log(f"调用 TTS：句子 {segment.id + 1}")
        api_key = require_openrouter_api_key("MAI 中文语音合成")
        audio_bytes = openrouter_request(
            "audio/speech",
            api_key,
            {
                "model": args.tts_model,
                "input": segment.text,
                "voice": args.voice,
                "response_format": "mp3",
                "speed": args.tts_speed,
            },
            expect_binary=True,
            timeout_seconds=90,
        )
        assert isinstance(audio_bytes, bytes)
        if len(audio_bytes) < 100:
            raise PipelineError(f"TTS 片段 {segment.id} 返回的数据过短")
        temporary_raw = raw.with_suffix(raw.suffix + ".tmp")
        temporary_raw.write_bytes(audio_bytes)
        temporary_raw.replace(raw)
    if (
        not trimmed.exists()
        or force
        or trimmed.stat().st_mtime < raw.stat().st_mtime
    ):
        trim_tts_edge_silence(raw, trimmed)
    return trimmed, probe_duration(trimmed), cache_key


def prepare_tts_sources(
    args: argparse.Namespace,
    workdir: Path,
    segments: Sequence[Segment],
    raw_dir: Path,
    trimmed_dir: Path,
    *,
    force: bool = False,
) -> dict[int, tuple[Path, float, str]]:
    """Generate or reuse independent TTS segments with bounded concurrency."""
    if not segments:
        return {}
    if args.tts_backend == "cosyvoice3-source":
        return prepare_cosyvoice3_sources(
            args, workdir, segments, raw_dir, trimmed_dir, force=force
        )
    worker_count = min(args.tts_workers, len(segments))
    log(f"并发准备 {len(segments)} 个 TTS 片段（{worker_count} 个线程）")
    prepared: dict[int, tuple[Path, float, str]] = {}
    failures: list[tuple[int, Exception]] = []
    futures: dict[Future[tuple[Path, float, str]], Segment] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="youtube-dub-tts"
    ) as executor:
        for segment in segments:
            future = executor.submit(
                ensure_tts_source,
                args,
                segment,
                raw_dir,
                trimmed_dir,
                force=force,
            )
            futures[future] = segment
        for completed, future in enumerate(as_completed(futures), 1):
            segment = futures[future]
            try:
                prepared[segment.id] = future.result()
            except Exception as exc:  # Let other independent segments finish and cache.
                failures.append((segment.id, exc))
                log(f"TTS 句子 {segment.id + 1} 失败：{exc}")
                continue
            log(f"语音时长检查 {completed}/{len(segments)}：句子 {segment.id + 1}")
    if failures:
        failed_ids = ", ".join(str(index) for index, _ in failures[:12])
        raise PipelineError(
            f"并发中文 TTS 有 {len(failures)} 个句子失败（ID：{failed_ids}）；"
            f"成功片段已缓存，可直接重跑。首个错误：{failures[0][1]}"
        )
    return prepared


def overlong_segments(
    segments: Sequence[Segment], durations: dict[int, float], max_tempo: float
) -> list[Segment]:
    return [
        segment
        for segment in segments
        if durations[segment.id] / segment.duration > max_tempo + 0.001
    ]


def shorten_translations_for_timing(
    args: argparse.Namespace,
    workdir: Path,
    segments: Sequence[Segment],
    durations: dict[int, float],
    attempt: int,
) -> tuple[dict[int, str], list[dict[str, Any]]]:
    english_path = workdir / "transcript.en.polished.json"
    english_by_id: dict[int, str] = {}
    if english_path.exists():
        english_by_id = {
            item.id: item.text for item in parse_segments(read_json(english_path))
        }
    payload = []
    for segment in segments:
        generated = durations[segment.id]
        allowed_natural_duration = segment.duration * args.max_tempo
        reduction_ratio = min(
            0.95,
            allowed_natural_duration
            / generated
            * 0.90
            * (0.82 ** (attempt - 1)),
        )
        current_characters = translation_character_count(segment.text)
        target_characters = max(2, math.floor(current_characters * reduction_ratio))
        payload.append(
            {
                "id": segment.id,
                "english": english_by_id.get(segment.id, ""),
                "current_zh": segment.text,
                "time_window_seconds": round(segment.duration, 3),
                "generated_speech_seconds": round(generated, 3),
                "maximum_post_tempo": args.max_tempo,
                "target_spoken_characters": target_characters,
                "rewrite_attempt": attempt,
            }
        )

    system = (
        "You are a senior Chinese dubbing editor. Rewrite only the supplied Simplified Chinese lines "
        "so each can be spoken naturally within its time budget. Preserve the exact essential meaning, "
        "facts, numbers, product identity, and technical terminology from the English source. Once the "
        "context is established, concise unambiguous aliases are allowed: for example, 'CompTIA Security+ "
        "exam' may become 'Security+', and 'exam objectives document' may become '考试大纲'. Remove filler, "
        "redundancy, and literal English syntax; prefer short idiomatic Chinese. For very short windows, "
        "use a direct complete imperative such as '直接跳过。' and avoid colons or unnecessary pauses. "
        "Every zh value must remain one complete sentence with "
        "sentence-final punctuation. The target_spoken_characters value is a hard maximum for the spoken "
        "content; product names may remain unchanged but surrounding Chinese must be compressed further. "
        "Do not merge or split ids. Return JSON only with a "
        "segments array containing exactly id and zh."
    )
    log(f"第 {attempt} 轮自动精简 {len(segments)} 个超时句子")
    value = codex_json_completion(
        args,
        system,
        "Rewrite these dubbing lines:\n" + json.dumps(payload, ensure_ascii=False),
        segment_translation_schema(),
    )
    rewritten = validate_translations(segments, value)
    records: list[dict[str, Any]] = []
    for segment in segments:
        new_text = rewritten[segment.id]
        if not re.search(r"(?:[。！？.!?]|…{1,2})[\"'”’）)】]*$", new_text):
            raise PipelineError(f"自动精简结果不是完整句子：{new_text}")
        old_length = translation_character_count(segment.text)
        new_length = translation_character_count(new_text)
        if new_text == segment.text or new_length > old_length:
            log(
                f"句子 {segment.id} 本轮未有效缩短（{old_length} -> {new_length} 字），"
                "保留原文并在下一轮使用更严格预算"
            )
            rewritten[segment.id] = segment.text
            continue
        records.append(
            {
                "attempt": attempt,
                "id": segment.id,
                "start": segment.start,
                "end": segment.end,
                "generated_before": round(durations[segment.id], 3),
                "tempo_before": round(durations[segment.id] / segment.duration, 4),
                "before": segment.text,
                "after": new_text,
                "characters_before": old_length,
                "characters_after": new_length,
            }
        )
    return rewritten, records


def persist_timing_adapted_transcript(
    args: argparse.Namespace,
    workdir: Path,
    segments: Sequence[Segment],
    records: Sequence[dict[str, Any]],
) -> None:
    path = workdir / "transcript.zh.json"
    document = read_json(path) if path.exists() else {}
    history = document.get("timing_adaptations", [])
    if not isinstance(history, list):
        history = []
    history.extend(records)
    document.update(
        {
            "segments": [asdict(item) for item in segments],
            "translation_fingerprint": segments_fingerprint(segments),
            "timing_adaptation": {
                "enabled": True,
                "max_tempo": args.max_tempo,
                "edge_silence_trimmed": True,
            },
            "timing_adaptations": history,
        }
    )
    write_json(path, document)
    write_srt(workdir / "transcript.zh.srt", segments)


def make_silence(destination: Path, duration: float, sample_rate: int = 24000) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def fit_audio_to_window(
    source: Path,
    destination: Path,
    target_duration: float,
    max_tempo: float,
    *,
    generated_duration: float | None = None,
    output_duration: float | None = None,
) -> dict[str, Any]:
    if generated_duration is None:
        generated_duration = probe_duration(source)
    # Only accelerate speech that exceeds its slot; shorter speech is padded, not slowed unnaturally.
    tempo = max(1.0, generated_duration / target_duration)
    if tempo > max_tempo + 0.001:
        raise PipelineError(
            f"配音仍需 {tempo:.3f}x 加速，超过 {max_tempo:.3f}x 上限；请继续压缩译文"
        )
    if output_duration is None:
        output_duration = target_duration
    if output_duration + 0.001 < target_duration:
        raise PipelineError("配音输出窗口不能短于真实句子时间窗")
    filter_parts = []
    if tempo > 1.002:
        filter_parts.append(atempo_chain(tempo))
    filter_parts.extend(
        [
            f"apad=whole_dur={output_duration:.6f}",
            f"atrim=duration={output_duration:.6f}",
            "asetpts=PTS-STARTPTS",
        ]
    )
    temporary_destination = destination.with_name(
        destination.stem + ".tmp" + destination.suffix
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            ",".join(filter_parts),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(temporary_destination),
        ]
    )
    temporary_destination.replace(destination)
    return {
        "generated_duration": round(generated_duration, 3),
        "target_duration": round(target_duration, 3),
        "output_duration": round(output_duration, 3),
        "tempo": round(tempo, 4),
        "warning": None,
    }


def fit_audio_segments(
    args: argparse.Namespace,
    segments: Sequence[Segment],
    prepared: dict[int, tuple[Path, float, str]],
    fitted_dir: Path,
    timeline_duration: float,
    *,
    on_progress: Callable[[list[dict[str, Any]]], None] | None = None,
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Fit independent sentence audio concurrently and restore source order."""
    if not segments:
        return [], []
    fitted_dir.mkdir(parents=True, exist_ok=True)
    worker_count = min(args.fit_workers, len(segments))
    log(f"并发对齐 {len(segments)} 个配音片段（{worker_count} 个线程）")
    completed_by_id: dict[int, tuple[Path, dict[str, Any]]] = {}
    failures: list[tuple[int, Exception]] = []
    futures: dict[Future[dict[str, Any]], tuple[Segment, Path]] = {}
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="youtube-dub-fit"
    ) as executor:
        for position, segment in enumerate(segments):
            source, generated_duration, segment_cache_key = prepared[segment.id]
            next_start = (
                segments[position + 1].start
                if position + 1 < len(segments)
                else timeline_duration
            )
            output_duration = max(segment.duration, next_start - segment.start)
            fitted = fitted_dir / (
                f"segment_{segment.id:04d}_{segment_cache_key}_"
                f"tempo_{args.max_tempo:.3f}.wav"
            )
            future = executor.submit(
                fit_audio_to_window,
                source,
                fitted,
                segment.duration,
                args.max_tempo,
                generated_duration=generated_duration,
                output_duration=output_duration,
            )
            futures[future] = (segment, fitted)
        for completed, future in enumerate(as_completed(futures), 1):
            segment, fitted = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # Preserve other independently fitted files.
                failures.append((segment.id, exc))
                log(f"对齐配音句子 {segment.id + 1} 失败：{exc}")
                continue
            result.update(
                {
                    "id": segment.id,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                }
            )
            completed_by_id[segment.id] = (fitted, result)
            ordered_reports = [
                completed_by_id[item.id][1]
                for item in segments
                if item.id in completed_by_id
            ]
            if on_progress:
                on_progress(ordered_reports)
            log(f"对齐配音完成 {completed}/{len(segments)}：句子 {segment.id + 1}")
    if failures:
        failed_ids = ", ".join(str(index) for index, _ in failures[:12])
        raise PipelineError(
            f"并发配音对齐有 {len(failures)} 个句子失败（ID：{failed_ids}）；"
            f"成功文件已保留，可直接重跑。首个错误：{failures[0][1]}"
        )
    fitted_files = [completed_by_id[segment.id][0] for segment in segments]
    reports = [completed_by_id[segment.id][1] for segment in segments]
    return fitted_files, reports


def concat_audio(
    files: Sequence[Path],
    destination: Path,
    workdir: Path,
    *,
    initial_silence: float = 0,
) -> None:
    concat_file = workdir / "segments" / "concat.txt"
    concat_file.parent.mkdir(parents=True, exist_ok=True)
    timeline_files = list(files)
    if initial_silence > 0.001:
        initial_silence_path = workdir / "segments" / "initial_silence.wav"
        make_silence(initial_silence_path, initial_silence)
        timeline_files.insert(0, initial_silence_path)
    lines = []
    for path in timeline_files:
        escaped = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def synthesize_dub(
    args: argparse.Namespace, workdir: Path, chinese: Sequence[Segment]
) -> Path:
    output = workdir / "chinese_voice.wav"
    report_path = workdir / "sync_report.json"

    def make_synthesis_fingerprint(segments: Sequence[Segment]) -> str:
        settings = {
            "source_fingerprint": segments_fingerprint(segments),
            "tts_backend": args.tts_backend,
            "model": tts_audit_model(args),
            "voice": tts_audit_voice(args),
            "speed": args.tts_speed,
            "max_tempo": args.max_tempo,
            "edge_trim_version": 1,
            "adaptive_shortening_version": 2,
            "timeline_silence_version": 3,
        }
        return hashlib.sha256(
            json.dumps(settings, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    requested_fingerprint = make_synthesis_fingerprint(chinese)
    cached_report: dict[str, Any] = {}
    if report_path.exists() and not args.force:
        cached_value = read_json(report_path)
        if isinstance(cached_value, dict):
            cached_report = cached_value
    if (
        output.exists()
        and cached_report.get("complete")
        and cached_report.get("synthesis_fingerprint") == requested_fingerprint
        and not args.force
    ):
        log("复用符合舒适语速上限的中文配音")
        return output
    if output.exists() and not args.force:
        log("中文译文或配音参数已变化，将重新生成中文配音")

    raw_dir = workdir / "segments" / "tts_raw"
    trimmed_dir = workdir / "segments" / "tts_trimmed"
    fitted_dir = workdir / "segments" / "tts_fitted"
    raw_dir.mkdir(parents=True, exist_ok=True)
    trimmed_dir.mkdir(parents=True, exist_ok=True)
    fitted_dir.mkdir(parents=True, exist_ok=True)

    working = list(chinese)
    prepared: dict[int, tuple[Path, float, str]] = {}
    durations: dict[int, float] = {}
    adaptation_records: list[dict[str, Any]] = []
    for rewrite_round in range(args.timing_rewrite_attempts + 1):
        log(
            f"测量 {len(working)} 个句子的自然语音时长"
            + ("（同时裁剪首尾静音）" if rewrite_round == 0 else "")
        )
        prepared = prepare_tts_sources(
            args,
            workdir,
            working,
            raw_dir,
            trimmed_dir,
            force=args.force and rewrite_round == 0,
        )
        durations = {
            segment_id: result[1] for segment_id, result in prepared.items()
        }

        overlong = overlong_segments(working, durations, args.max_tempo)
        if not overlong:
            break
        worst_tempo = max(durations[item.id] / item.duration for item in overlong)
        log(
            f"发现 {len(overlong)} 个句子超过 {args.max_tempo:.2f}x 舒适语速上限"
            f"（最高 {worst_tempo:.2f}x）"
        )
        if rewrite_round >= args.timing_rewrite_attempts:
            ids = ", ".join(str(item.id) for item in overlong[:12])
            raise PipelineError(
                f"经过 {args.timing_rewrite_attempts} 轮自动精简后，仍有 "
                f"{len(overlong)} 个句子超时（ID：{ids}）。已保留中间结果，可直接重跑继续精简。"
            )
        rewritten, records = shorten_translations_for_timing(
            args,
            workdir,
            overlong,
            durations,
            rewrite_round + 1,
        )
        adaptation_records.extend(records)
        working = [
            Segment(
                item.id,
                item.start,
                item.end,
                rewritten.get(item.id, item.text),
            )
            for item in working
        ]
        persist_timing_adapted_transcript(args, workdir, working, records)

    source_fingerprint = segments_fingerprint(working)
    synthesis_fingerprint = make_synthesis_fingerprint(working)

    def write_fit_checkpoint(reports: list[dict[str, Any]]) -> None:
        write_json(
            report_path,
            {
                "complete": False,
                "source_fingerprint": source_fingerprint,
                "synthesis_fingerprint": synthesis_fingerprint,
                "tts_backend": args.tts_backend,
                "model": tts_audit_model(args),
                "voice": tts_audit_voice(args),
                "speed": args.tts_speed,
                "tts_workers": args.tts_workers,
                "fit_workers": args.fit_workers,
                "max_tempo": args.max_tempo,
                "edge_silence_trimmed": True,
                "timing_adaptations": adaptation_records,
                "segments": reports,
            },
        )

    timeline_duration = probe_duration(workdir / "source.mp4")
    fitted_files, reports = fit_audio_segments(
        args,
        working,
        prepared,
        fitted_dir,
        timeline_duration,
        on_progress=write_fit_checkpoint,
    )

    if not fitted_files:
        raise PipelineError("没有可合成的中文片段")
    concat_audio(
        fitted_files,
        output,
        workdir,
        initial_silence=max(0.0, working[0].start),
    )
    write_json(
        report_path,
        {
            "complete": True,
            "source_fingerprint": source_fingerprint,
            "synthesis_fingerprint": synthesis_fingerprint,
            "tts_backend": args.tts_backend,
            "model": tts_audit_model(args),
            "voice": tts_audit_voice(args),
            "speed": args.tts_speed,
            "tts_workers": args.tts_workers,
            "fit_workers": args.fit_workers,
            "max_tempo": args.max_tempo,
            "edge_silence_trimmed": True,
            "timing_adaptations": adaptation_records,
            "segments": reports,
            "warnings": [item for item in reports if item["warning"]],
        },
    )
    return output


def mux_video(args: argparse.Namespace, workdir: Path, video: Path, dub: Path) -> Path:
    output = workdir / "dubbed.zh.mp4"
    subtitle_source = workdir / "transcript.zh.srt"
    remux_requested = args.force or args.remux_only
    if output.exists() and not remux_requested:
        input_paths = [video, dub]
        if subtitle_source.exists():
            input_paths.append(subtitle_source)
        newest_input = max(path.stat().st_mtime for path in input_paths)
        if (
            output.stat().st_mtime >= newest_input
            and math.isclose(
                probe_video_duration(output), probe_video_duration(video), abs_tol=0.1
            )
        ):
            log("复用与当前配音和字幕一致的已合成视频")
            if subtitle_source.exists():
                burn_bilibili_subtitles(workdir, output, subtitle_source)
            return output

    # If only the dub/subtitles changed, reuse the already encoded H.264 video
    # track. This avoids needlessly transcoding the original AV1 stream again.
    video_input = video
    if (
        output.exists()
        and video.stat().st_mtime <= output.stat().st_mtime
        and probe_video_codec(output) == "h264"
        and math.isclose(
            probe_video_duration(output), probe_video_duration(video), abs_tol=0.1
        )
    ):
        video_input = output
        log("复用现有最终文件中的 H.264 视频轨，仅更新配音和字幕")
    temporary_output = workdir / "dubbed.zh.tmp.mp4"
    source_video_codec = probe_video_codec(video_input)
    video_options = quicktime_video_options(source_video_codec)
    if source_video_codec == "h264":
        log("源视频已是 H.264，将直接复制视频轨")
    else:
        log(f"源视频为 {source_video_codec}，将转码为 QuickTime 兼容的 H.264")
    subtitle = (
        prepare_quicktime_subtitle(
            subtitle_source, workdir / "segments" / "transcript.zh.quicktime.srt"
        )
        if subtitle_source.exists()
        else subtitle_source
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-stats_period",
        "10",
        "-nostats",
        "-y",
        "-i",
        str(video_input),
        "-i",
        str(dub),
    ]
    if subtitle.exists():
        command.extend(["-i", str(subtitle)])
    command.extend([
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
    ])
    if subtitle.exists():
        command.extend(["-map", "2:s:0"])
    command.extend([
        *video_options,
        "-c:a",
        "aac",
        "-profile:a",
        "aac_low",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-b:a",
        "192k",
        "-disposition:a:0",
        "default",
        "-metadata:s:a:0",
        "language=zho",
        "-metadata:s:a:0",
        "title=中文配音",
    ])
    if subtitle.exists():
        log("加入 QuickTime 兼容的内嵌中文字幕轨")
        command.extend(
            [
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=chi",
                "-metadata:s:s:0",
                "title=中文字幕",
                "-metadata:s:s:0",
                "handler_name=中文字幕",
                "-disposition:s:0",
                "default+forced",
            ]
        )
    command.extend([
        "-map_metadata",
        "0",
        "-movflags",
        "+faststart",
        "-max_muxing_queue_size",
        "2048",
        "-shortest",
        str(temporary_output),
    ])
    run(command)
    temporary_output.replace(output)
    if subtitle_source.exists():
        burn_bilibili_subtitles(workdir, output, subtitle_source)
    return output


def burn_bilibili_subtitles(
    workdir: Path, source_video: Path, subtitle_source: Path
) -> Path:
    """Create an upload-safe MP4 with Chinese subtitles rendered into pixels."""
    output = workdir / "dubbed.zh.bilibili.mp4"
    metadata_path = workdir / "segments" / "bilibili_subtitle_render.json"
    newest_input = max(source_video.stat().st_mtime, subtitle_source.stat().st_mtime)
    render_metadata = read_json(metadata_path) if metadata_path.exists() else {}
    if (
        output.exists()
        and output.stat().st_mtime >= newest_input
        and render_metadata.get("render_version") == BILIBILI_SUBTITLE_RENDER_VERSION
        and render_metadata.get("source_mtime_ns") == source_video.stat().st_mtime_ns
        and render_metadata.get("subtitle_mtime_ns") == subtitle_source.stat().st_mtime_ns
    ):
        log("复用与当前成片和字幕一致的哔哩哔哩硬字幕版")
        return output

    temporary_output = workdir / "dubbed.zh.bilibili.tmp.mp4"
    subtitle_filter = (
        f"subtitles=filename='{escape_subtitle_filter_path(subtitle_source)}':"
        "force_style='FontName=Noto Sans CJK SC,Alignment=2,MarginV=28,Outline=2,Shadow=0'"
    )
    log("烧录中文字幕，生成哔哩哔哩上传版（视频需要重新编码，音频直接复制）")
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-progress",
            "pipe:1",
            "-stats_period",
            "10",
            "-nostats",
            "-y",
            "-i",
            str(source_video),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            subtitle_filter,
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-tag:v",
            "avc1",
            "-c:a",
            "copy",
            "-sn",
            "-map_metadata",
            "0",
            "-movflags",
            "+faststart",
            str(temporary_output),
        ]
    )
    temporary_output.replace(output)
    write_json(
        metadata_path,
        {
            "render_version": BILIBILI_SUBTITLE_RENDER_VERSION,
            "source_mtime_ns": source_video.stat().st_mtime_ns,
            "subtitle_mtime_ns": subtitle_source.stat().st_mtime_ns,
            "font": "Noto Sans CJK SC",
        },
    )
    return output


def embed_subtitles_only(workdir: Path) -> Path:
    video = workdir / "dubbed.zh.mp4"
    subtitle_source = workdir / "transcript.zh.srt"
    if not video.exists() or not subtitle_source.exists():
        raise PipelineError(
            "--subtitles-only 需要已有的 dubbed.zh.mp4 和 transcript.zh.srt"
        )
    subtitle = prepare_quicktime_subtitle(
        subtitle_source, workdir / "segments" / "transcript.zh.quicktime.srt"
    )
    temporary_output = workdir / "dubbed.zh.tmp.mp4"
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(subtitle),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-map",
            "1:s:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=chi",
            "-metadata:s:s:0",
            "title=中文字幕",
            "-metadata:s:s:0",
            "handler_name=中文字幕",
            "-disposition:s:0",
            "default+forced",
            "-movflags",
            "+faststart",
            str(temporary_output),
        ]
    )
    temporary_output.replace(video)
    return video


def stage_enabled(stop_after: str, stage: str) -> bool:
    return STAGES.index(stage) <= STAGES.index(stop_after)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载 YouTube 视频，生成带时间戳的中英文稿、中文配音和合成视频。"
    )
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="YouTube 视频 URL")
    parser.add_argument("--workdir", type=Path, default=Path("output"), help="输出目录")
    parser.add_argument(
        "--debug-seconds",
        type=float,
        default=45.0,
        help="调试模式处理时长，默认 45 秒",
    )
    parser.add_argument("--start-seconds", type=float, default=0.0, help="从源视频第几秒开始")
    parser.add_argument(
        "--full",
        action="store_true",
        help="显式处理完整视频；未指定时绝不会处理全片",
    )
    parser.add_argument("--chunk-seconds", type=float, default=15.0, help="目标转录片段时长")
    parser.add_argument("--transcriber-model", default=DEFAULT_TRANSCRIBER)
    parser.add_argument(
        "--transcriber-backend",
        choices=("faster-whisper", "openrouter-whisper1"),
        default=DEFAULT_TRANSCRIBER_BACKEND,
        help="英文转录后端；默认使用本地 faster-whisper",
    )
    parser.add_argument(
        "--transcribe-workers",
        type=int,
        default=DEFAULT_TRANSCRIBE_WORKERS,
        help="英文转录并发任务数；本地模型默认 1",
    )
    parser.add_argument("--whisper-device", default=DEFAULT_WHISPER_DEVICE)
    parser.add_argument(
        "--whisper-compute-type", default=DEFAULT_WHISPER_COMPUTE_TYPE
    )
    parser.add_argument(
        "--whisper-cpu-threads",
        type=int,
        default=max(1, min(8, os.cpu_count() or 1)),
        help="本地 Whisper 使用的 CPU 线程数，默认最多 8",
    )
    parser.add_argument(
        "--text-model",
        help="Codex CLI 文本模型；默认使用 Codex CLI 当前默认模型",
    )
    parser.add_argument(
        "--translator-model",
        dest="text_model",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--tts-backend",
        choices=("cosyvoice3-source", "mai"),
        default=DEFAULT_TTS_BACKEND,
        help="中文 TTS 后端；默认用源片段作为 Fun-CosyVoice3 参考音色",
    )
    parser.add_argument("--tts-model", default=DEFAULT_TTS)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--tts-speed", type=float, default=1.0)
    parser.add_argument("--cosyvoice-root", type=Path)
    parser.add_argument("--cosyvoice-python", type=Path)
    parser.add_argument("--cosyvoice-model", type=Path)
    parser.add_argument(
        "--cosyvoice-threads",
        type=int,
        default=2,
        help="CosyVoice3 CPU 计算线程数；模型始终单实例顺序生成",
    )
    parser.add_argument(
        "--tts-workers",
        type=int,
        default=DEFAULT_TTS_WORKERS,
        help="中文 TTS 并发线程数，默认 4",
    )
    parser.add_argument(
        "--fit-workers",
        type=int,
        default=DEFAULT_FIT_WORKERS,
        help="逐句音频对齐的并发线程数，默认 4",
    )
    parser.add_argument(
        "--max-tempo",
        type=float,
        default=1.15,
        help="后期音频允许的最大加速倍率，默认 1.15；超出时自动精简译文",
    )
    parser.add_argument(
        "--timing-rewrite-attempts",
        type=int,
        default=5,
        help="超时译文的最大自动精简轮数，默认 5",
    )
    parser.add_argument(
        "--cookies-from-browser",
        help="遇到 YouTube 登录校验时传给 yt-dlp，例如 chrome 或 safari",
    )
    parser.add_argument(
        "--youtube-player-client",
        default="web_embedded",
        help="yt-dlp 的 YouTube 播放器客户端；默认 web_embedded 以降低 DASH 403 风险",
    )
    parser.add_argument("--proxy", help="显式代理 URL；默认读取系统代理设置")
    parser.add_argument(
        "--download-strategy",
        choices=("local-trim", "remote-section"),
        default="local-trim",
        help="兼容旧命令；当前始终完整下载两条原始流后本地截取调试素材",
    )
    parser.add_argument(
        "--video-format",
        help="覆盖 yt-dlp 纯视频格式选择，默认 bestvideo",
    )
    parser.add_argument(
        "--quality",
        choices=tuple(QUALITY_VIDEO_FORMATS),
        default="best",
        help="视频清晰度上限：720p、1080p 或 best；默认 best",
    )
    parser.add_argument(
        "--audio-format",
        help="覆盖 yt-dlp 纯音频格式选择，默认 bestaudio",
    )
    parser.add_argument("--force", action="store_true", help="覆盖并重新执行已完成的阶段")
    parser.add_argument(
        "--remux-only",
        action="store_true",
        help="仅用现有素材重建最终视频，并自动转为 QuickTime 兼容编码；不调用模型",
    )
    parser.add_argument(
        "--subtitles-only",
        action="store_true",
        help="仅将 transcript.zh.srt 内嵌到已有最终视频；不重新编码音视频",
    )
    parser.add_argument("--stop-after", choices=STAGES, default="mux", help="在指定阶段后停止")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.debug_seconds <= 0:
        raise PipelineError("--debug-seconds 必须大于 0")
    if args.start_seconds < 0:
        raise PipelineError("--start-seconds 不能小于 0")
    if args.chunk_seconds < 4:
        raise PipelineError("--chunk-seconds 不能小于 4")
    if (
        args.transcriber_backend == "openrouter-whisper1"
        and args.transcriber_model != "openai/whisper-1"
    ):
        raise PipelineError(
            "openrouter-whisper1 后端必须使用 --transcriber-model openai/whisper-1"
        )
    if args.whisper_cpu_threads < 1:
        raise PipelineError("--whisper-cpu-threads 必须大于 0")
    if args.cosyvoice_threads < 1:
        raise PipelineError("--cosyvoice-threads 必须大于 0")
    if not 1 <= args.transcribe_workers <= MAX_NETWORK_WORKERS:
        raise PipelineError(
            f"--transcribe-workers 必须在 1 到 {MAX_NETWORK_WORKERS} 之间"
        )
    if not 1 <= args.tts_workers <= MAX_NETWORK_WORKERS:
        raise PipelineError(f"--tts-workers 必须在 1 到 {MAX_NETWORK_WORKERS} 之间")
    if not 1 <= args.fit_workers <= MAX_NETWORK_WORKERS:
        raise PipelineError(f"--fit-workers 必须在 1 到 {MAX_NETWORK_WORKERS} 之间")
    if not 0.5 <= args.tts_speed <= 2.0:
        raise PipelineError("--tts-speed 必须在 0.5 到 2.0 之间")
    if not 1.0 <= args.max_tempo <= 1.5:
        raise PipelineError("--max-tempo 必须在 1.0 到 1.5 之间")
    if not 1 <= args.timing_rewrite_attempts <= 5:
        raise PipelineError("--timing-rewrite-attempts 必须在 1 到 5 之间")
    if args.remux_only and args.subtitles_only:
        raise PipelineError("--remux-only 和 --subtitles-only 不能同时使用")


def ensure_manifest(args: argparse.Namespace, workdir: Path) -> None:
    manifest_path = workdir / "manifest.json"
    mode = {
        "url": args.url,
        "full": args.full,
        "start_seconds": args.start_seconds,
        "debug_seconds": None if args.full else args.debug_seconds,
        "download_strategy": None if args.full else args.download_strategy,
    }
    if manifest_path.exists():
        existing = read_json(manifest_path).get("input")
        if existing != mode and not args.force:
            raise PipelineError(
                "输出目录属于另一组输入参数。请换 --workdir，或确认后使用 --force 覆盖。"
            )
    write_json(manifest_path, {"input": mode, "models": {
        "transcriber": args.transcriber_model,
        "transcriber_backend": args.transcriber_backend,
        "transcriber_device": (
            args.whisper_device if args.transcriber_backend == "faster-whisper" else None
        ),
        "transcriber_compute_type": (
            args.whisper_compute_type
            if args.transcriber_backend == "faster-whisper"
            else None
        ),
        "text_backend": TEXT_BACKEND,
        "text": text_model_name(args),
        "tts_backend": args.tts_backend,
        "tts": tts_audit_model(args),
        "voice": tts_audit_voice(args),
    }, "execution": {
        "transcribe_workers": args.transcribe_workers,
        "tts_workers": args.tts_workers,
        "fit_workers": args.fit_workers,
    }})


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_args(args)
        require_tools(args)
        workdir = args.workdir.expanduser().resolve()

        if args.subtitles_only:
            log("仅加入字幕模式：复制现有音视频轨，不调用模型")
            output = embed_subtitles_only(workdir)
            bilibili_output = burn_bilibili_subtitles(
                workdir, output, workdir / "transcript.zh.srt"
            )
            log(f"中文字幕已加入：{output}")
            log(f"哔哩哔哩硬字幕版：{bilibili_output}")
            return 0

        if args.remux_only:
            log("仅重新封装模式：忽略 URL，不读取或改写 manifest")
            video = workdir / "source.mp4"
            dub = workdir / "chinese_voice.wav"
            if not video.exists() or not dub.exists():
                raise PipelineError("--remux-only 需要已有的 source.mp4 和 chinese_voice.wav")
            output = mux_video(args, workdir, video, dub)
            log(f"重新封装完成：{output}")
            if (workdir / "transcript.zh.srt").exists():
                log(f"哔哩哔哩硬字幕版：{workdir / 'dubbed.zh.bilibili.mp4'}")
            return 0

        ensure_manifest(args, workdir)
        if args.full:
            log("全片模式已启用")
        else:
            log(f"安全调试模式：仅处理 {args.debug_seconds:g} 秒")

        video, source_audio = download_media(args, workdir)
        if args.stop_after == "download":
            log(f"下载完成：{workdir}")
            return 0

        raw_english = transcribe_audio(args, workdir, source_audio)
        if args.stop_after == "transcribe":
            return 0
        transcript_document = read_json(workdir / "transcript.en.json")
        english = polish_transcript(
            args, workdir, raw_english, parse_words(transcript_document)
        )
        if args.stop_after == "polish":
            return 0
        chinese = translate_segments(args, workdir, english)
        if args.stop_after == "translate":
            return 0
        dub = synthesize_dub(args, workdir, chinese)
        if args.stop_after == "synthesize":
            return 0
        output = mux_video(args, workdir, video, dub)
        log(f"完成：{output}")
        log(f"哔哩哔哩硬字幕版：{workdir / 'dubbed.zh.bilibili.mp4'}")
        return 0
    except (PipelineError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
