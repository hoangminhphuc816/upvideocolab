from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.config import Config
from worker.downloader import DownloadError
from worker.sheet_queue import Job


def make_cfg():
    return Config(
        telethon_session="s", api_id=1, api_hash="h",
        target_channel=-100123, owner_chat_id=42,
        sa_json="{}", sheet_id="S", bot_token="t",
        sheet_name="Jobs", dl_dir="/tmp",
    )


def make_job():
    return Job(
        job_id="JOB-1", status="PENDING", url="https://x/v.mp4", chat_id=42,
        created_at=1, updated_at=None, worker="", msg_id=None, error="",
        retry_count=0, row_index=2,
    )


def statuses_written(q):
    return [c.args[1] for c in q.set_status.call_args_list if len(c.args) >= 2]


@pytest.mark.asyncio
async def test_run_once_no_job_returns_zero():
    q = MagicMock()
    q.next_job.return_value = None
    with patch("worker.main.SheetQueue", return_value=q):
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 0


@pytest.mark.asyncio
async def test_run_once_claim_miss_returns_zero_without_processing():
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = False  # worker khác thắng race
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.download_to_file") as dl:
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 0
    dl.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_full_success_flow():
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.download_to_file", return_value=("/tmp/v.mp4", 1024)), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done") as nd, \
         patch("worker.main.notify_failed") as nf:
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 0
    assert statuses_written(q) == ["DOWNLOADING", "UPLOADING", "DONE"]
    q.record_message_id.assert_called_once_with("JOB-1", 777)
    nd.assert_called_once()
    nf.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_permanent_error_marks_failed_and_notifies():
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.download_to_file",
               side_effect=DownloadError("HTTP 404 khi tải")), \
         patch("worker.main.notify_done") as nd, \
         patch("worker.main.notify_failed") as nf:
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 2
    assert statuses_written(q) == ["DOWNLOADING", "FAILED"]
    nf.assert_called_once()
    nd.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_transient_error_reraises_without_failed():
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.download_to_file",
               side_effect=DownloadError("mất kết nối giữa chừng: net")), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed") as nf:
        from worker.main import run_once
        with pytest.raises(DownloadError, match="mất kết nối"):
            await run_once(make_cfg())
    assert "FAILED" not in statuses_written(q)
    nf.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_notify_failure_does_not_break_success():
    from worker.notifier import NotifyError
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.download_to_file", return_value=("/tmp/v.mp4", 1024)), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done", side_effect=NotifyError("bot API lỗi")):
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 0  # notify là best-effort, không phá DONE


def test_run_sweep_notifies_newly_failed():
    q = MagicMock()
    q.sweep_stale.return_value = ["JOB-MAX"]
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.notify_failed") as nf:
        from worker.main import run_sweep
        rc = run_sweep(make_cfg())
    assert rc == 0
    nf.assert_called_once()


def test_permanent_substrings():
    from worker.main import _permanent
    assert _permanent("HTTP 404 khi tải")
    assert _permanent("file vượt giới hạn 2GB")
    assert _permanent("ffprobe không đọc được video stream")
    assert _permanent("send_file thất bại: ... (PeerIdInvalidError)")
    assert not _permanent("mất kết nối giữa chừng: net")
    assert not _permanent("không kết nối được Telegram: timeout")
