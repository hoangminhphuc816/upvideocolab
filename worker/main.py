"""Orchestrator: claim 1 job → download → upload → DONE; hoặc --sweep."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from worker.config import Config, SWEEP_REQUIRED, load_config
from worker.downloader import DownloadError, download_to_file, validate_video
from worker.notifier import NotifyError, notify_done, notify_failed
from worker.sheet_queue import SheetQueue
from worker.uploader import UploadError, upload_to_channel

log = logging.getLogger("worker")

# Lỗi retry không giúp được — đánh FAILED ngay
PERMANENT_SUBSTRINGS = (
    "HTTP 4",
    "vượt giới hạn",
    "rỗng",
    "ffprobe",
    "PeerIdInvalid",
    "ChatWriteForbidden",
    "chat not found",
)


def _permanent(reason: str) -> bool:
    return any(s in reason for s in PERMANENT_SUBSTRINGS)


def _try_notify_failed(cfg: Config, job_id: str, reason: str) -> None:
    try:
        notify_failed(cfg, job_id, reason)
    except NotifyError:
        log.exception("notify thất bại (bỏ qua): %s", job_id)


async def run_once(cfg: Config) -> int:
    queue = SheetQueue(cfg)
    job = queue.next_job()
    if job is None:
        log.info("không có job — thoát")
        return 0

    worker_id = "worker-" + os.environ.get("GITHUB_RUN_ID", "local")
    if not queue.claim(job.job_id, worker_id):
        log.warning("claim %s thất bại — worker khác đã lấy", job.job_id)
        return 0

    path = None
    try:
        queue.set_status(job.job_id, "DOWNLOADING")
        path, _size = download_to_file(job.url, cfg.dl_dir)
        validate_video(path)

        queue.set_status(job.job_id, "UPLOADING")
        msg_id = await upload_to_channel(cfg, path, caption=job.job_id)
        queue.record_message_id(job.job_id, msg_id)
        queue.set_status(job.job_id, "DONE")
        try:
            notify_done(cfg, job.job_id, msg_id)
        except NotifyError:
            log.exception("notify DONE thất bại (bỏ qua): %s", job.job_id)
        return 0

    except (DownloadError, UploadError) as e:
        reason = str(e)
        if not _permanent(reason):
            raise  # tạm thời — sweep sẽ RETRY sau ≤15 phút
        queue.set_status(job.job_id, "FAILED", error=reason)
        _try_notify_failed(cfg, job.job_id, reason)
        return 2

    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                log.exception("không xóa được file tạm: %s", path)


def run_sweep(cfg: Config) -> int:
    queue = SheetQueue(cfg)
    newly_failed = queue.sweep_stale()
    for job_id in newly_failed:
        _try_notify_failed(cfg, job_id, "quá số lần retry")
    log.info("sweep xong, %d job chuyển FAILED", len(newly_failed))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--sweep" in sys.argv[1:]:
        cfg = load_config(required=SWEEP_REQUIRED)
        return run_sweep(cfg)
    cfg = load_config()
    try:
        return asyncio.run(run_once(cfg))
    except (DownloadError, UploadError):
        log.exception("job lỗi tạm thời — sweep sẽ RETRY")
        return 1


if __name__ == "__main__":
    sys.exit(main())
