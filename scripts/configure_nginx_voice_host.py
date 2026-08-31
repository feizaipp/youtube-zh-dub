#!/usr/bin/env python3
"""Install an isolated, expiring HTTP endpoint for voice-enrollment samples."""

from __future__ import annotations

import argparse
import ipaddress
import os
import pwd
import secrets
import subprocess
from pathlib import Path


DEFAULT_CONFIG = Path("/etc/nginx/conf.d/youtube-dub-voice-enrollment.conf")
DEFAULT_SECRET = Path("/etc/nginx/private-download/voice-enroll-secret")
DEFAULT_ROOT = Path("/srv/youtube-dub-voice-enroll")


def atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)


def render_config(public_host: str, document_root: Path, secret: str) -> str:
    return f"""# Managed by youtube-zh-dub/scripts/configure_nginx_voice_host.py
server {{
    listen 80;
    listen [::]:80;
    server_name {public_host};

    location ~ \"^/voice-enroll/(?<sample>[A-Za-z0-9_-]{{32,64}}\\.wav)$\" {{
        secure_link $arg_md5,$arg_expires;
        secure_link_md5 \"$secure_link_expires$uri:{secret}\";
        if ($secure_link = \"\") {{ return 403; }}
        if ($secure_link = \"0\") {{ return 410; }}

        alias {document_root}/$sample;
        autoindex off;
        access_log off;
        limit_except GET HEAD {{ deny all; }}
        default_type audio/wav;
        add_header Cache-Control \"no-store\" always;
        add_header X-Content-Type-Options \"nosniff\" always;
    }}

    location /voice-enroll/ {{ return 404; }}
    location / {{ return 301 https://$host$request_uri; }}
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-host", required=True)
    parser.add_argument("--document-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--secret-file", type=Path, default=DEFAULT_SECRET)
    parser.add_argument("--no-reload", action="store_true")
    args = parser.parse_args()

    if os.geteuid() != 0:
        parser.error("必须以 root 运行，才能安全配置 Nginx")
    try:
        address = ipaddress.ip_address(args.public_host)
    except ValueError:
        parser.error("--public-host 必须是公网 IPv4 地址")
    if address.version != 4 or not address.is_global:
        parser.error("--public-host 必须是公网 IPv4 地址")

    args.document_root.mkdir(parents=True, exist_ok=True)
    www_data = pwd.getpwnam("www-data")
    os.chown(args.document_root, 0, www_data.pw_gid)
    os.chmod(args.document_root, 0o750)

    if args.secret_file.exists():
        secret = args.secret_file.read_text(encoding="utf-8").strip()
    else:
        secret = secrets.token_hex(32)
        atomic_write(args.secret_file, secret + "\n", 0o600)
    if len(secret) < 32:
        raise RuntimeError("voice enrollment URL secret is unexpectedly short")

    previous = args.config.read_bytes() if args.config.exists() else None
    atomic_write(
        args.config,
        render_config(args.public_host, args.document_root.resolve(), secret),
        0o600,
    )
    try:
        subprocess.run(["nginx", "-t"], check=True)
    except Exception:
        if previous is None:
            args.config.unlink(missing_ok=True)
        else:
            temporary = args.config.with_name(args.config.name + ".rollback")
            temporary.write_bytes(previous)
            os.chmod(temporary, 0o600)
            temporary.replace(args.config)
        raise
    if not args.no_reload:
        subprocess.run(["systemctl", "reload", "nginx"], check=True)
    print(f"Configured http://{args.public_host}/voice-enroll/", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
