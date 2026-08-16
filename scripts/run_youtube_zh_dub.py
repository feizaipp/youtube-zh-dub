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


def safe_directory_name(title: str, video_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", title)
    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = re.sub(r"-{2,}", "-", normalized)
    if normalized in {"", ".", ".."}:
        normalized = f"YouTube-{video_id}"
    while len(normalized.encode("utf-8")) > 180:
        normalized = normalized[:-1].rstrip(" .")
    return normalized or f"YouTube-{video_id}"


def prepare_environment(project: Path) -> dict[str, str]:
    environment = os.environ.copy()
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
    try:
        video_id = youtube_video_id(args.url)
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        pipeline = find_pipeline(args.pipeline_script)
        project = pipeline.parent
        environment = prepare_environment(project)
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))

    download_only = is_download_only(args, passthrough)
    if not args.dry_run and not download_only and not environment.get("OPENROUTER_API_KEY"):
        print(
            "错误：请先通过环境变量 OPENROUTER_API_KEY 提供 OpenRouter API Key。",
            file=sys.stderr,
        )
        return 2
    needs_codex = requested_stop not in ("download", "transcribe")
    required_tools = (
        ("yt-dlp",)
        if args.dry_run
        else ("yt-dlp", "ffmpeg", "ffprobe") + (("codex",) if needs_codex else ())
    )
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

    command = build_command(args, passthrough, pipeline, workdir, canonical_url)
    if title:
        print(f"[youtube-zh-dub] 视频标题：{title}", flush=True)
    print(f"[youtube-zh-dub] 输出目录：{workdir}", flush=True)
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
    completed = subprocess.run(command, env=environment)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
