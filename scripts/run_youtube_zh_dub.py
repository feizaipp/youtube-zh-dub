#!/usr/bin/env python3
"""Launch the verified YouTube-to-Chinese dubbing pipeline with safe defaults."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse


DEFAULT_PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_MAX_TEMPO = "1.15"
DEFAULT_REWRITE_ATTEMPTS = "5"
QUALITY_CHOICES = ("best", "720p", "1080p")


def youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    video_id = ""
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
            parts = parsed.path.strip("/").split("/")
            video_id = parts[1] if len(parts) > 1 else ""
    else:
        raise ValueError("只接受 youtube.com 或 youtu.be 链接")

    cleaned = "".join(char for char in video_id if char.isalnum() or char in "_-")
    if not cleaned:
        raise ValueError("无法从链接中识别 YouTube 视频 ID")
    return cleaned


def find_pipeline(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    configured_project = os.environ.get("YOUTUBE_DUB_PROJECT")
    if configured_project:
        candidates.append(Path(configured_project).expanduser() / "youtube_dub.py")
    candidates.extend([Path.cwd() / "youtube_dub.py", DEFAULT_PROJECT / "youtube_dub.py"])
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"找不到 youtube_dub.py；已检查：{searched}")


def has_option(arguments: list[str], option: str) -> bool:
    return option in arguments or any(item.startswith(option + "=") for item in arguments)


def option_value(arguments: list[str], option: str) -> str | None:
    for index, item in enumerate(arguments):
        if item == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if item.startswith(option + "="):
            return item.split("=", 1)[1]
    return None


def is_download_only(args: argparse.Namespace, passthrough: list[str]) -> bool:
    return args.download_only or option_value(passthrough, "--stop-after") == "download"


def is_cover_only(args: argparse.Namespace) -> bool:
    """Return whether the launcher should download only the video cover image."""
    return bool(getattr(args, "download_cover", False))


def should_download_cover(args: argparse.Namespace) -> bool:
    """Return whether a normal non-dry-run workflow must retrieve its cover."""
    return not is_cover_only(args) and not bool(getattr(args, "dry_run", False))


def needs_openrouter(args: argparse.Namespace, passthrough: list[str]) -> bool:
    """Return whether the run reaches OpenRouter transcription or synthesis."""
    if is_cover_only(args) or is_download_only(args, passthrough):
        return False
    backend = option_value(passthrough, "--transcriber-backend") or "faster-whisper"
    if backend == "openrouter-whisper1":
        return True
    stop_after = option_value(passthrough, "--stop-after") or "mux"
    tts_backend = option_value(passthrough, "--tts-backend") or "aliyun-cosyvoice"
    return tts_backend == "mai" and stop_after in {"synthesize", "mux"}


def needs_dashscope(args: argparse.Namespace, passthrough: list[str]) -> bool:
    """Return whether the run reaches Alibaba Cloud speech synthesis."""
    if is_cover_only(args) or is_download_only(args, passthrough):
        return False
    stop_after = option_value(passthrough, "--stop-after") or "mux"
    tts_backend = option_value(passthrough, "--tts-backend") or "aliyun-cosyvoice"
    return tts_backend == "aliyun-cosyvoice" and stop_after in {"synthesize", "mux"}


def safe_directory_name(title: str, video_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", title)
    normalized = "".join(
        char
        for char in normalized
        if char.isspace()
        or char in "-_"
        or unicodedata.category(char)[0] in {"L", "M", "N"}
    )
    normalized = re.sub(r"\s+", "-", normalized).strip(" .-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    if normalized in {"", ".", ".."}:
        normalized = f"YouTube-{video_id}"
    while len(normalized.encode("utf-8")) > 180:
        normalized = normalized[:-1].rstrip(" .")
    return normalized or f"YouTube-{video_id}"


def prepare_environment(project: Path) -> dict[str, str]:
    environment = os.environ.copy()
    allowed_secrets = {
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_WORKSPACE_ID",
        "ALIYUN_COSYVOICE_VOICE",
    }
    secret_files = (
        project / ".secrets" / "dashscope.env",
        project.parent / ".secrets" / "dashscope.env",
    )
    for secret_file in secret_files:
        if not secret_file.is_file():
            continue
        for line in secret_file.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(
                r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*",
                line,
            )
            if not match or match.group(1) not in allowed_secrets:
                continue
            name, value = match.groups()
            value = value.strip().strip("'\"")
            if value and not environment.get(name):
                environment[name] = value
    venv_bin = project / ".venv" / "bin"
    if venv_bin.is_dir():
        environment["PATH"] = f"{venv_bin}{os.pathsep}{environment.get('PATH', '')}"
    return environment


def fetch_video_title(url: str, environment: dict[str, str], passthrough: list[str]) -> str:
    command = ["yt-dlp", "--no-playlist", "--no-warnings"]
    if shutil.which("node", path=environment.get("PATH")):
        command.extend(["--js-runtimes", "node", "--remote-components", "ejs:github"])
    proxy = option_value(passthrough, "--proxy")
    if not proxy:
        system_proxies = urllib.request.getproxies()
        proxy = system_proxies.get("https") or system_proxies.get("http")
    if proxy:
        command.extend(["--proxy", proxy])
    cookies = option_value(passthrough, "--cookies-from-browser")
    if cookies:
        command.extend(["--cookies-from-browser", cookies])
    command.extend(["--print", "%(title)s", url])
    try:
        result = subprocess.run(
            command,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        details = exc.stderr.strip()[-2000:]
        raise RuntimeError(f"无法读取 YouTube 视频标题：{details}") from exc
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("yt-dlp 没有返回唯一的视频标题")
    return lines[0]


def manifest_video_id(workdir: Path) -> str | None:
    metadata = workdir / "video_metadata.json"
    if metadata.is_file():
        try:
            document = json.loads(metadata.read_text(encoding="utf-8"))
            video_id = str(document["video_id"])
            if video_id:
                return video_id
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    manifest = workdir / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
        return youtube_video_id(str(document["input"]["url"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def choose_title_workdir(project: Path, title: str, video_id: str) -> Path:
    output_root = project / "output"
    safe_title = safe_directory_name(title, video_id)
    candidate = output_root / safe_title
    if not candidate.exists() or manifest_video_id(candidate) == video_id:
        return candidate
    candidate = output_root / f"{safe_title} [{video_id}]"
    if not candidate.exists() or manifest_video_id(candidate) == video_id:
        return candidate
    counter = 2
    while True:
        numbered = output_root / f"{safe_title} [{video_id}]-{counter}"
        if not numbered.exists() or manifest_video_id(numbered) == video_id:
            return numbered
        counter += 1


def build_command(
    args: argparse.Namespace,
    passthrough: list[str],
    pipeline: Path,
    workdir: Path,
    canonical_url: str,
) -> list[str]:
    command = [sys.executable, str(pipeline), canonical_url]
    if args.debug_seconds is None:
        command.append("--full")
    else:
        command.extend(["--debug-seconds", str(args.debug_seconds)])
    command.extend(["--workdir", str(workdir)])
    if not has_option(passthrough, "--max-tempo"):
        command.extend(["--max-tempo", DEFAULT_MAX_TEMPO])
    if not has_option(passthrough, "--timing-rewrite-attempts"):
        command.extend(["--timing-rewrite-attempts", DEFAULT_REWRITE_ATTEMPTS])
    if args.download_only and not has_option(passthrough, "--stop-after"):
        command.extend(["--stop-after", "download"])
    if not has_option(passthrough, "--quality"):
        command.extend(["--quality", args.quality])
    command.extend(passthrough)
    return command


def cover_candidates(video_id: str) -> list[str]:
    """Return public YouTube thumbnail URLs from highest to lowest resolution."""
    base = f"https://i.ytimg.com/vi/{video_id}"
    return [
        f"{base}/maxresdefault.jpg",
        f"{base}/sddefault.jpg",
        f"{base}/hqdefault.jpg",
        f"{base}/mqdefault.jpg",
        f"{base}/default.jpg",
    ]


def download_cover(workdir: Path, video_id: str, force: bool) -> Path:
    """Download the best available public JPEG thumbnail without fetching media."""
    destination = workdir / "cover.jpg"
    if destination.is_file() and not force:
        print("[youtube-zh-dub] 复用已下载的封面：cover.jpg", flush=True)
        return destination
    failures: list[str] = []
    for url in cover_candidates(video_id):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content_type = response.headers.get_content_type()
                payload = response.read()
            if not content_type.startswith("image/") or not payload:
                failures.append(f"{url} 返回 {content_type or '空内容'}")
                continue
            temporary = destination.with_suffix(".jpg.tmp")
            temporary.write_bytes(payload)
            temporary.replace(destination)
            print(f"[youtube-zh-dub] 已下载封面：{url}", flush=True)
            return destination
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            failures.append(f"{url}：{exc}")
    details = "；".join(failures)
    raise RuntimeError(f"无法下载 YouTube 封面：{details}")


def write_video_metadata(
    workdir: Path,
    title: str,
    video_id: str,
    canonical_url: str,
    requested_url: str,
    requested_quality: str,
) -> None:
    destination = workdir / "video_metadata.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "title": title,
                "video_id": video_id,
                "url": canonical_url,
                "requested_url": requested_url,
                "requested_quality": requested_quality,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="自动生成带中文字幕的中文配音 YouTube 视频。"
    )
    parser.add_argument("url", help="youtube.com 或 youtu.be 链接")
    parser.add_argument("--workdir", help="覆盖输出目录；默认使用 output/视频标题")
    parser.add_argument(
        "--debug-seconds",
        type=float,
        help="仅调试指定秒数；未提供时自动处理完整视频",
    )
    parser.add_argument("--pipeline-script", help="覆盖 youtube_dub.py 路径")
    parser.add_argument(
        "--video-title",
        help="覆盖 yt-dlp 返回的视频标题，主要用于离线测试",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将执行的命令和输出目录，不下载或调用模型",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="只下载所选清晰度的独立视频流和最高质量音频流，不调用模型",
    )
    parser.add_argument(
        "--download-cover",
        action="store_true",
        help="仅下载视频封面为 cover.jpg；不下载媒体、不调用配音流程或模型",
    )
    parser.add_argument(
        "--quality",
        choices=QUALITY_CHOICES,
        default="best",
        help="视频清晰度上限：720p、1080p 或 best；默认 best",
    )
    args, passthrough = parser.parse_known_args()

    if args.debug_seconds is not None and args.debug_seconds <= 0:
        parser.error("--debug-seconds 必须大于 0")
    requested_stop = option_value(passthrough, "--stop-after")
    if args.download_only and requested_stop not in (None, "download"):
        parser.error("--download-only 不能与其他 --stop-after 阶段同时使用")
    if args.download_cover and args.download_only:
        parser.error("--download-cover 不能与 --download-only 同时使用")
    if args.download_cover and requested_stop is not None:
        parser.error("--download-cover 不能与 --stop-after 同时使用")
    try:
        video_id = youtube_video_id(args.url)
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        pipeline = find_pipeline(args.pipeline_script)
        project = pipeline.parent
        environment = prepare_environment(project)
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))

    download_only = is_download_only(args, passthrough)
    if not args.dry_run and needs_openrouter(args, passthrough) and not environment.get("OPENROUTER_API_KEY"):
        print(
            "错误：请先通过环境变量 OPENROUTER_API_KEY 提供 OpenRouter API Key（用于所选远程转录后端或 MAI 中文语音合成）。",
            file=sys.stderr,
        )
        return 2
    if not args.dry_run and needs_dashscope(args, passthrough):
        if not environment.get("DASHSCOPE_API_KEY"):
            print(
                "错误：请先通过环境变量 DASHSCOPE_API_KEY 提供华北2（北京）地域的百炼 API Key。",
                file=sys.stderr,
            )
            return 2
        if not (
            environment.get("ALIYUN_COSYVOICE_VOICE")
            or option_value(passthrough, "--voice")
        ):
            print(
                "错误：请通过 ALIYUN_COSYVOICE_VOICE 或 --voice 提供与 cosyvoice-v3.5-flash 绑定的 voice_id。",
                file=sys.stderr,
            )
            return 2
    if args.dry_run:
        required_tools = ("yt-dlp",)
    elif is_cover_only(args):
        required_tools = ()
    else:
        required_tools = ("yt-dlp", "ffmpeg", "ffprobe")
    missing = [
        name
        for name in required_tools
        if not shutil.which(name, path=environment.get("PATH"))
    ]
    if missing:
        print("错误：缺少命令行工具：" + ", ".join(missing), file=sys.stderr)
        return 2

    title = args.video_title
    container: Path
    if args.workdir:
        workdir = Path(args.workdir).expanduser().resolve()
        container = workdir
    else:
        try:
            title = title or fetch_video_title(args.url, environment, passthrough)
        except RuntimeError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2
        container = choose_title_workdir(project, title, video_id)
        if args.debug_seconds is None:
            workdir = container
        else:
            start = option_value(passthrough, "--start-seconds") or "0"
            debug_name = safe_directory_name(
                f"start-{start}s_duration-{args.debug_seconds:g}s",
                video_id,
            )
            workdir = container / "_debug" / debug_name

    if title:
        print(f"[youtube-zh-dub] 视频标题：{title}", flush=True)
    print(f"[youtube-zh-dub] 输出目录：{workdir}", flush=True)
    if is_cover_only(args):
        print("[youtube-zh-dub] 封面源：" + cover_candidates(video_id)[0], flush=True)
    else:
        command = build_command(args, passthrough, pipeline, workdir, canonical_url)
        print("[youtube-zh-dub] 命令：" + " ".join(command), flush=True)
    if args.dry_run:
        return 0

    workdir.mkdir(parents=True, exist_ok=True)
    if title:
        container.mkdir(parents=True, exist_ok=True)
        write_video_metadata(
            container, title, video_id, canonical_url, args.url, args.quality
        )
        write_video_metadata(
            workdir, title, video_id, canonical_url, args.url, args.quality
        )
    if is_cover_only(args) or should_download_cover(args):
        try:
            cover = download_cover(
                workdir, video_id, force=has_option(passthrough, "--force")
            )
        except RuntimeError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 1
        print(f"[youtube-zh-dub] 封面文件：{cover}", flush=True)
        if is_cover_only(args):
            return 0
    completed = subprocess.run(command, env=environment)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
