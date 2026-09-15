"""Orchestrator: claim 1 job → download → upload → DONE; hoặc --sweep."""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from worker.config import Config, SWEEP_REQUIRED, load_config
from worker.downloader import CHUNK_SIZE, DownloadError, download_chunk, download_to_file, probe_range_support, assemble, validate_video
from worker.notifier import NotifyError, notify_done, notify_failed
from worker.sheet_queue import SheetQueue, checkpoint_dl_bytes
from worker.uploader import UploadError, upload_to_channel

log = logging.getLogger("worker")

# Lỗi retry không giúp được — đánh FAILED ngay
PERMANENT_SUBSTRINGS = (
    "HTTP 400",
    "HTTP 403",
    "HTTP 404",
    "HTTP 410",
    "vượt giới hạn",
    "rỗng",
    "ffprobe",
    "PeerIdInvalid",
    "ChatWriteForbidden",
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
    part = None
    try:
        queue.set_status(job.job_id, "DOWNLOADING")
        if probe_range_support(job.url):
            start = checkpoint_dl_bytes(job)
            retried_reset = False
            while True:
                try:
                    part, got = download_chunk(job.url, start=start, dest_dir=cfg.dl_dir)
                except DownloadError as e:
                    msg = str(e)
                    if not retried_reset and "cần reset checkpoint" in msg:
                        queue.set_checkpoint(job.job_id, "dl:0")
                        part_path = os.path.join(cfg.dl_dir, "video.part")
                        if os.path.exists(part_path):
                            try:
                                os.unlink(part_path)
                            except OSError:
                                log.exception("không xóa được part khi reset checkpoint: %s", part_path)
                        retried_reset = True
                        start = 0
                        continue
                    raise
                start += got
                queue.set_checkpoint(job.job_id, f"dl:{start}")
                if got < CHUNK_SIZE:
                    break
            path = assemble(cfg.dl_dir)
        else:
            path, _size = download_to_file(job.url, cfg.dl_dir)
            queue.set_checkpoint(job.job_id, "dl:full")
        validate_video(path)

        queue.set_status(job.job_id, "UPLOADING")
        try:
            msg_id = await upload_to_channel(cfg, path, caption=job.job_id)
        except UploadError:
            queue.set_checkpoint(job.job_id, "dl:full")
            raise
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
        for target in (path, part):
            if target and os.path.exists(target):
                try:
                    os.unlink(target)
                except OSError:
                    log.exception("không xóa được file tạm: %s", target)


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
