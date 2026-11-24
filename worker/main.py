"""Orchestrator: claim 1 job → download → upload → DONE; hoặc --sweep."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

from worker.config import Config, SWEEP_REQUIRED, load_config
from worker.downloader import (
    CHUNK_SIZE, DownloadError, TELEGRAM_MAX,
    assemble, choose_source, download_chunk, download_to_file,
    probe_range_support, validate_video,
)
from worker.notifier import NotifyError, notify_done, notify_failed
from worker.sheet_queue import SheetQueue, checkpoint_dl_bytes
from worker.uploader import UploadError, TransientUploadError, resolve_target_channel, upload_to_channel

log = logging.getLogger("worker")

# Lỗi retry không giúp được — đánh FAILED ngay
PERMANENT_SUBSTRINGS = (
    "HTTP 400",
    "HTTP 403",
    "HTTP 404",
    "HTTP 410",
    "vượt giới hạn",
    "file rỗng",
    "ffprobe",
    "PeerIdInvalid",
    "ChatWriteForbidden",
    # Entity không resolve được: TARGET_CHANNEL sai/không phải kênh, hoặc account
    # không còn trong kênh đó. Retry không cứu — sweep RETRY×3 chỉ đốt thêm session
    # Colab (mỗi lượt tải lại từ đầu) rồi FAILED 'quá số lần retry'.
    # Đã quan sát: "send_file thất bại: ValueError: Could not find the input entity
    # for PeerUser(user_id=…) (PeerUser)" khi TARGET_CHANNEL là id dương/0 (Telethon
    # resolve_id map id >=0 → PeerUser, kênh phải là -100…).
    "Could not find the input entity",
    "ChannelInvalid",
    "ChannelPrivate",
    # Session là của BOT, không phải user account. Retry vô nghĩa: bot vĩnh viễn
    # không gọi được get_dialogs (BotMethodInvalidError) nên không có access_hash.
    # Nguyên nhân thật: dán bot token vào prompt 'phone (or bot token)' của
    # tools/make_session.py. Phục hồi: re-mint bằng số điện thoại account thật.
    "session của BOT",
    "BotMethodInvalid",
    # Session Telegram đã chết vĩnh viễn — retry chỉ đốt thêm session Colab.
    # Phục hồi: tools/make_session.py → cập nhật secret TELETHON_SESSION (runbook §3).
    "AuthKeyDuplicated",
    "AuthKeyUnregistered",
    "AuthKeyInvalid",
    "SessionRevoked",
    "UserDeactivated",
    # Cấu hình sai cũng vĩnh viễn: retry không cứu được, phải re-mint secret hoặc sửa api.
    # - "Not a valid string": byte version đầu Sai / secret hoàn toàn sai (StringSession ValueError).
    # - "Incorrect padding": secret bị cắt cụt giữa chừng (binascii.Error).
    # - "unpack requires a buffer": secret ngắn nhưng vẫn khớp padding (struct.error).
    # - "ApiIdInvalid": api_id/api_hash sai.
    "Not a valid string",
    "Incorrect padding",
    "unpack requires a buffer",
    "ApiIdInvalid",
    # Marker ổn định từ uploader.py khi StringSession raise mọi lớp lỗi cắt cụt/padding/struct/ASCII.
    "TELETHON_SESSION không hợp lệ",
    # get_entity trả entity min/không hash (chưa từng "gặp" kênh trong session
    # này) — retry cùng session không bao giờ nạp được hash.
    "TARGET_CHANNEL không dùng được",
)

# Nhịp tim Sheet trong cả 2 pha. Phải < 15 phút (sweep_stale mặc định) với biên rộng:
# một job UPLOADING bị bỏ nhịp 3 lần liên tiếp mới bị coi là stale và bị cướp.
HEARTBEAT_INTERVAL_S = 300


def _permanent(reason: str) -> bool:
    return any(s in reason for s in PERMANENT_SUBSTRINGS)


def _owns_job(queue: SheetQueue, job_id: str, worker_id: str) -> bool:
    """Fencing: chỉ worker đang giữ cột `worker` mới được upload.

    Sweep đổi status → RETRY nhưng KHÔNG xoá cột `worker`; quyền thực sự được
    chuyển khi VM kế tiếp claim (ghi đè cột). Đọc lại tươi ngay trước upload để
    VM bị cướp không mở client Telegram thứ hai (AuthKeyDuplicatedError).
    """
    fresh = queue.get_job(job_id)
    return fresh is not None and fresh.worker == worker_id


def _try_notify_failed(cfg: Config, job_id: str, reason: str) -> None:
    try:
        notify_failed(cfg, job_id, reason)
    except NotifyError:
        log.exception("notify thất bại (bỏ qua): %s", job_id)


def _reset_checkpoint_part(cfg: Config, queue: SheetQueue, job) -> None:
    """Xoá checkpoint + part khi đổi nguồn (byte file A không ghép được với B)."""
    queue.set_checkpoint(job.job_id, "dl:0")
    job.checkpoint = "dl:0"
    part = os.path.join(cfg.dl_dir, "video.part")
    if os.path.exists(part):
        try:
            os.unlink(part)
        except OSError:
            log.exception("không xóa được part khi đổi nguồn: %s", part)


def _run_download(url: str, cfg: Config, queue: SheetQueue, job,
                  on_progress) -> tuple[str, str | None]:
    """Tải 1 URL; trả (path, part_path cuối) để finally xoá."""
    if probe_range_support(url):
        start = checkpoint_dl_bytes(job)
        entry_checkpoint = start
        retried_reset = False
        part = None
        while True:
            try:
                part, got = download_chunk(url, start=start, dest_dir=cfg.dl_dir,
                                           on_progress=on_progress)
            except DownloadError as e:
                msg = str(e)
                if not retried_reset and "cần reset checkpoint" in msg:
                    queue.set_checkpoint(job.job_id, "dl:0")
                    job.checkpoint = "dl:0"
                    part_path = os.path.join(cfg.dl_dir, "video.part")
                    if os.path.exists(part_path):
                        try:
                            os.unlink(part_path)
                        except OSError:
                            log.exception("không xóa được part khi reset checkpoint: %s",
                                          part_path)
                    retried_reset = True
                    start = 0
                    part = None
                    continue
                if start > entry_checkpoint and ("HTTP 416" in msg or "chunk rỗng" in msg):
                    queue.set_checkpoint(job.job_id, "dl:full")
                    return assemble(cfg.dl_dir), part
                raise
            start += got
            queue.set_checkpoint(job.job_id, f"dl:{start}")
            if got < CHUNK_SIZE:
                return assemble(cfg.dl_dir), part

    path, _size = download_to_file(url, cfg.dl_dir, on_progress=on_progress)
    queue.set_checkpoint(job.job_id, "dl:full")
    return path, None


async def run_once(cfg: Config) -> int:
    queue = SheetQueue(cfg)
    job = queue.next_job()
    if job is None:
        log.info("không có job — thoát")
        return 0

    worker_id = os.environ.get("WORKER_ID", "worker-" + os.environ.get("GITHUB_RUN_ID", "local"))
    if not queue.claim(job.job_id, worker_id):
        log.warning("claim %s thất bại — worker khác đã lấy", job.job_id)
        return 0

    # Preflight TRƯỚC pha tải: TARGET_CHANNEL sai/phải-mint-lại-session phải chết
    # trong vài giây, không đốt một lượt tải video (có thể 30+ phút, 1.5GB).
    try:
        channel = await resolve_target_channel(cfg)
    except TransientUploadError:
        raise
    except UploadError as e:
        reason = str(e)
        if not _permanent(reason):
            raise
        queue.set_status(job.job_id, "FAILED", error=reason)
        _try_notify_failed(cfg, job.job_id, reason)
        return 2

    path = None
    part = None
    try:
        queue.set_status(job.job_id, "DOWNLOADING")
        last_log = last_touch = 0.0

        def on_progress(done: int, total: int | None, elapsed: float) -> None:
            nonlocal last_log, last_touch
            now = time.monotonic()
            if now - last_log >= 15:
                last_log = now
                pct = f"{done * 100 // total}%" if total else "?"
                log.info("download %s: %d/%s byte (%s)", job.job_id, done, total or "?", pct)
            if now - last_touch >= HEARTBEAT_INTERVAL_S:
                last_touch = now
                queue.touch(job.job_id)

        url, reason = choose_source(job.url, job.url_alt, TELEGRAM_MAX)
        if url is None:
            raise DownloadError(reason)

        if url != job.url:
            _reset_checkpoint_part(cfg, queue, job)

        candidates = [url]
        if job.url_alt and job.url_alt != url:
            candidates.append(job.url_alt)

        for idx, src in enumerate(candidates):
            if idx > 0:
                _reset_checkpoint_part(cfg, queue, job)
                part = None
            try:
                path, part = _run_download(src, cfg, queue, job, on_progress)
                break
            except DownloadError as e:
                if idx == len(candidates) - 1:
                    raise
                log.warning("nguồn %s lỗi (%s) — thử bản dự phòng", src, e)

        validate_video(path)

        if not _owns_job(queue, job.job_id, worker_id):
            log.warning("mất quyền xử lý %s — bỏ pha upload, không mở client Telegram thứ hai",
                        job.job_id)
            return 0

        last_upload_touch = 0.0

        def upload_heartbeat() -> None:
            nonlocal last_upload_touch
            now = time.monotonic()
            if now - last_upload_touch >= HEARTBEAT_INTERVAL_S:
                last_upload_touch = now
                queue.touch(job.job_id)

        queue.set_status(job.job_id, "UPLOADING")
        try:
            msg_id = await upload_to_channel(cfg, path, caption=job.job_id,
                                             channel=channel,
                                             heartbeat=upload_heartbeat)
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
        reason = "quá số lần retry"
        try:
            job = queue.get_job(job_id)
        except Exception:  # best-effort: notify phải chạy kể cả khi đọc Sheet lỗi
            log.exception("không đọc được dòng %s để lấy lý do", job_id)
        else:
            if job and job.error:
                reason = job.error
        _try_notify_failed(cfg, job_id, reason)
    if newly_failed:
        print(f"::error::{len(newly_failed)} job chuyển FAILED: "
              f"{', '.join(newly_failed)} — xem cột error trên Sheet", flush=True)
        log.error("sweep xong, %d job chuyển FAILED: %s",
                  len(newly_failed), ", ".join(newly_failed))
        return 1
    log.info("sweep xong, không có job nào chuyển FAILED")
    return 0


def _run_once_blocking(cfg: Config) -> int:
    """Chạy run_once(cfg) từ code đồng bộ.

    Trên script/CLI thường: asyncio.run() trực tiếp. Trên Jupyter/Colab kernel
    (colab run exec bootstrap) đã có event loop đang chạy trong thread chính —
    asyncio.run() raise RuntimeError → chuyển sang thread riêng có loop riêng.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_once(cfg))

    result: list[int] = []
    error: list[BaseException] = []

    def _target() -> None:
        try:
            result.append(asyncio.run(run_once(cfg)))
        except BaseException as e:  # noqa: BLE001 — re-raised on main thread below
            error.append(e)

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if error:
        raise error[0]
    return result[0]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--sweep" in sys.argv[1:]:
        cfg = load_config(required=SWEEP_REQUIRED)
        return run_sweep(cfg)
    cfg = load_config()
    try:
        return _run_once_blocking(cfg)
    except (DownloadError, UploadError):
        log.exception("job lỗi tạm thời — sweep sẽ RETRY")
        return 1


if __name__ == "__main__":
    sys.exit(main())
