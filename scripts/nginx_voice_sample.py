#!/usr/bin/env python3
"""Publish, revoke, and prune short-lived Nginx voice sample URLs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import urllib.parse
import wave
from pathlib import Path


DEFAULT_ROOT = Path("/srv/youtube-dub-voice-enroll")
DEFAULT_SECRET = Path("/etc/nginx/private-download/voice-enroll-secret")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def inspect_wav(path: Path) -> dict[str, float | int]:
    try:
        with wave.open(str(path), "rb") as stream:
            channels = stream.getnchannels()
            sample_width = stream.getsampwidth()
            sample_rate = stream.getframerate()
            frames = stream.getnframes()
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"不是有效的 PCM WAV：{path}") from exc
    duration = frames / sample_rate if sample_rate else 0.0
    if sample_width != 2:
        raise ValueError("声音复刻样本必须是 16-bit WAV")
    if sample_rate < 16_000:
        raise ValueError("声音复刻样本采样率必须至少为 16 kHz")
    if channels not in (1, 2):
        raise ValueError("声音复刻样本只能是单声道或双声道")
    if not 5.0 <= duration <= 60.0:
        raise ValueError("声音复刻样本时长必须在 5 到 60 秒之间")
    return {
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
        "duration": round(duration, 3),
    }


def signature(expires: int, uri: str, secret: str) -> str:
    digest = hashlib.md5(f"{expires}{uri}:{secret}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def safe_token(value: str) -> str:
    token = Path(urllib.parse.urlparse(value).path).stem
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("无效的声音样本 token 或 URL")
    return token


def publish(args: argparse.Namespace) -> dict[str, object]:
    source = args.source.expanduser().resolve()
    properties = inspect_wav(source)
    secret = args.secret_file.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    destination = args.document_root / f"{token}.wav"
    metadata = args.document_root / f".{token}.json"
    args.document_root.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".wav.tmp")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o640)
    os.chown(temporary, -1, args.document_root.stat().st_gid)
    temporary.replace(destination)

    expires = int(time.time()) + args.expires_in
    uri = f"/voice-enroll/{token}.wav"
    query = urllib.parse.urlencode(
        {"md5": signature(expires, uri, secret), "expires": expires}
    )
    url = f"{args.base_url.rstrip('/')}{uri}?{query}"
    document: dict[str, object] = {
        "url": url,
        "token": token,
        "published_path": str(destination),
        "expires_at": expires,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "wav": properties,
    }
    temporary_metadata = metadata.with_suffix(".json.tmp")
    temporary_metadata.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary_metadata, 0o600)
    temporary_metadata.replace(metadata)
    return document


def revoke(args: argparse.Namespace) -> dict[str, object]:
    token = safe_token(args.token_or_url)
    removed: list[str] = []
    for path in (
        args.document_root / f"{token}.wav",
        args.document_root / f".{token}.json",
    ):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return {"token": token, "removed": removed}


def prune(args: argparse.Namespace) -> dict[str, object]:
    now = int(time.time())
    removed: list[str] = []
    for metadata in args.document_root.glob(".*.json"):
        try:
            document = json.loads(metadata.read_text(encoding="utf-8"))
            if int(document["expires_at"]) > now:
                continue
            token = safe_token(str(document["token"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        for path in (args.document_root / f"{token}.wav", metadata):
            if path.exists():
                path.unlink()
                removed.append(str(path))
    return {"removed": removed, "checked_at": now}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--document-root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("source", type=Path)
    publish_parser.add_argument("--base-url", required=True)
    publish_parser.add_argument("--expires-in", type=int, default=900)
    publish_parser.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET)
    publish_parser.set_defaults(handler=publish)

    revoke_parser = subparsers.add_parser("revoke")
    revoke_parser.add_argument("token_or_url")
    revoke_parser.set_defaults(handler=revoke)

    prune_parser = subparsers.add_parser("prune")
    prune_parser.set_defaults(handler=prune)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "expires_in") and not 60 <= args.expires_in <= 3600:
        parser.error("--expires-in 必须在 60 到 3600 秒之间")
    try:
        result = args.handler(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
