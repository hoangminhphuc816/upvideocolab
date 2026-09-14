"""Tải MP4 streaming bằng requests + validate bằng ffprobe."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import requests

# 2GB trừ 1 part 512KB — an toàn biên cho Telegram non-premium
TELEGRAM_MAX = 2 * 1024**3 - 512 * 1024


class DownloadError(Exception):
    pass


def download_to_file(url: str, dest_dir: str, max_bytes: int = TELEGRAM_MAX) -> tuple[str, int]:
    os.makedirs(dest_dir, exist_ok=True)
    path = Path(dest_dir) / "video.mp4"
    try:
        resp = requests.get(
            url,
            stream=True,
            timeout=(30, 60),
            headers={"User-Agent": "Mozilla/5.0 (video-pipeline)"},
        )
    except requests.RequestException as e:
        raise DownloadError(f"không kết nối được nguồn: {e}") from None

    if resp.status_code != 200:
        raise DownloadError(f"HTTP {resp.status_code} khi tải {url}")

    declared = resp.headers.get("Content-Length")
    if declared and int(declared) > max_bytes:
        raise DownloadError(f"file vượt giới hạn 2GB: {declared} bytes")

    size = 0
    try:
        with open(path, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise DownloadError(f"file vượt giới hạn 2GB giữa luồng: {size} bytes")
                f.write(chunk)
    except DownloadError:
        path.unlink(missing_ok=True)
        raise
    except (requests.RequestException, OSError) as e:
        path.unlink(missing_ok=True)
        raise DownloadError(f"mất kết nối giữa chừng: {e}") from None

    if size == 0:
        path.unlink(missing_ok=True)
        raise DownloadError(f"file rỗng: {url}")
    return str(path), size


def validate_video(path: str) -> None:
    """ffprobe phải tìm thấy ít nhất 1 video stream, nếu không raise DownloadError."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DownloadError(f"ffprobe lỗi: {e}") from None
    if proc.returncode != 0 or "video" not in (proc.stdout or ""):
        raise DownloadError(
            f"ffprobe không đọc được video stream: {(proc.stderr or '').strip()[:200]}"
        )
