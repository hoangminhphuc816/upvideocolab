"""Tải MP4 streaming bằng requests + validate bằng ffprobe."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import requests

# 2GB trừ 1 part 512KB — an toàn biên cho Telegram non-premium
TELEGRAM_MAX = 2 * 1024**3 - 512 * 1024
CHUNK_SIZE = 256 * 1024 * 1024  # 256MB/chunk — session ngắn tự nhiên


class DownloadError(Exception):
    pass


def probe_range_support(url: str) -> bool:
    try:
        r = requests.head(url, timeout=(30, 30), allow_redirects=True)
    except requests.RequestException:
        return False
    return r.status_code == 200 and "bytes" in (r.headers.get("Accept-Ranges", "").lower())


def download_chunk(url: str, start: int, dest_dir: str, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    os.makedirs(dest_dir, exist_ok=True)
    part = Path(dest_dir) / "video.part"
    end = start + chunk_size - 1
    try:
        resp = requests.get(url, stream=True, timeout=(30, 60),
                            headers={"Range": f"bytes={start}-{end}",
                                     "User-Agent": "Mozilla/5.0 (video-pipeline)"})
    except requests.RequestException as e:
        raise DownloadError(f"không kết nối được nguồn: {e}") from None
    if resp.status_code != 206:
        raise DownloadError(f"server không trả 206 cho Range (HTTP {resp.status_code}) — nguồn không hỗ trợ resume")
    got = 0
    try:
        with open(part, "ab") as f:
            for chunk in resp.iter_content(1024 * 1024):
                if chunk:
                    got += len(chunk)
                    f.write(chunk)
    except (requests.RequestException, OSError) as e:
        raise DownloadError(f"mất kết nối giữa chừng (chunk @{start}): {e}") from None
    if got == 0:
        raise DownloadError("chunk rỗng")
    return str(part), got


def assemble(dest_dir: str) -> str:
    part = Path(dest_dir) / "video.part"
    out = Path(dest_dir) / "video.mp4"
    part.rename(out)
    return str(out)


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
    if declared:
        try:
            declared_len = int(declared)
        except ValueError:
            raise DownloadError(f"Content-Length không hợp lệ: {declared!r}") from None
        if declared_len > max_bytes:
            raise DownloadError(f"file vượt giới hạn 2GB: {declared_len} bytes")

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
