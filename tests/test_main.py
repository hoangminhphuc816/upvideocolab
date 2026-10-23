import time
from unittest.mock import AsyncMock, MagicMock, ANY, call, patch

import pytest

from worker.config import Config
from worker.downloader import CHUNK_SIZE, DownloadError, OversizeError
from worker.sheet_queue import Job


def make_cfg(dl_dir: str = "/tmp") -> Config:
    return Config(
        telethon_session="s", api_id=1, api_hash="h",
        target_channel=-100123, owner_chat_id=42,
        sa_json="{}", sheet_id="S", bot_token="t",
        sheet_name="Jobs", dl_dir=dl_dir,
    )


def scripted_clock(*values):
    """Giá trị scripted cho `time.monotonic`; hết script thì rơi về đồng hồ thật.

    KHÔNG patch trực tiếp `worker.main.time.monotonic`: `worker.main.time` chính
    là module `time` toàn cục, mà `asyncio.BaseEventLoop.time()` cũng gọi
    `time.monotonic()` ⇒ patch sẽ đóng băng đồng hồ event loop và treo future
    (side_effect kiểu list còn cạn kiệt ⇒ StopIteration). Thay vào đó gán hẳn
    một đối tượng vào namespace `worker.main`.
    """
    it = iter(values)

    def clock() -> float:
        try:
            return next(it)
        except StopIteration:
            return time.monotonic()

    return clock


def make_job():
    return Job(
        job_id="JOB-1", status="PENDING", url="https://x/v.mp4", chat_id=42,
        created_at=1, updated_at=None, worker="", msg_id=None, error="",
        retry_count=0, checkpoint="0", row_index=2,
    )


def statuses_written(q):
    return [c.args[1] for c in q.set_status.call_args_list if len(c.args) >= 2]


@pytest.mark.asyncio
async def test_run_once_no_job_returns_zero():
    q = MagicMock()
    q.next_job.return_value = None
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=False):
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 0


@pytest.mark.asyncio
async def test_run_once_claim_miss_returns_zero_without_processing():
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = False  # worker khác thắng race
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=False), \
         patch("worker.main.download_to_file") as dl:
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 0
    dl.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_full_success_flow(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    fake = tmp_path / "v.mp4"
    fake.write_bytes(b"")
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=False), \
         patch("worker.main.download_to_file", return_value=(str(fake), 1024)), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done") as nd, \
         patch("worker.main.notify_failed") as nf, \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
    assert rc == 0
    assert statuses_written(q) == ["DOWNLOADING", "UPLOADING", "DONE"]
    q.record_message_id.assert_called_once_with("JOB-1", 777)
    nd.assert_called_once()
    nf.assert_not_called()
    unlink.assert_called_once_with(str(fake))


@pytest.mark.asyncio
async def test_run_once_permanent_error_marks_failed_and_notifies():
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=False), \
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
async def test_run_once_permanent_notify_failed_swallowed_keeps_rc2():
    from worker.notifier import NotifyError
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=False), \
         patch("worker.main.download_to_file",
               side_effect=DownloadError("HTTP 404 khi tải")), \
         patch("worker.main.notify_failed", side_effect=NotifyError("bot API lỗi")):
        from worker.main import run_once
        rc = await run_once(make_cfg())
    assert rc == 2
    assert statuses_written(q) == ["DOWNLOADING", "FAILED"]


@pytest.mark.asyncio
async def test_run_once_transient_error_reraises_without_failed():
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=False), \
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
async def test_run_once_notify_failure_does_not_break_success(tmp_path):
    from worker.notifier import NotifyError
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    fake = tmp_path / "v.mp4"
    fake.write_bytes(b"")
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=False), \
         patch("worker.main.download_to_file", return_value=(str(fake), 1024)), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done", side_effect=NotifyError("bot API lỗi")), \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
    assert rc == 0  # notify là best-effort, không phá DONE
    unlink.assert_called_once_with(str(fake))


def test_run_sweep_notifies_newly_failed():
    q = MagicMock()
    q.sweep_stale.return_value = ["JOB-MAX"]
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.notify_failed") as nf:
        from worker.main import run_sweep
        rc = run_sweep(make_cfg())
    assert rc == 0
    nf.assert_called_once()


@pytest.fixture(autouse=True)
def _stub_choose_source(monkeypatch):
    """Test cũ không chạm curl/network; test mới override bằng patch."""
    monkeypatch.setattr(
        "worker.main.choose_source",
        lambda primary, alt, max_bytes: (primary, None),
    )


def make_job_addr(url="https://x/v.mp4", url_alt=""):
    job = make_job()
    job.url = url
    job.url_alt = url_alt
    return job


@pytest.mark.asyncio
async def test_run_once_tu_choi_som_khi_1_ban_qua_nguong(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job_addr()   # không có alt
    q.claim.return_value = True
    reason = "file vượt giới hạn 1.90 GB: chỉ có 1 bản (2.10 GB)"
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.choose_source", return_value=(None, reason)), \
         patch("worker.main.download_to_file") as dl, \
         patch("worker.main.download_chunk") as dc, \
         patch("worker.main.notify_failed") as nf:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
    assert rc == 2
    dl.assert_not_called(); dc.assert_not_called()
    assert statuses_written(q) == ["DOWNLOADING", "FAILED"]
    nf.assert_called_once()


@pytest.mark.asyncio
async def test_run_once_chon_ban_ke_khi_ban_goc_qua_nguong(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job_addr(url_alt="https://x/lo.mp4")
    q.claim.return_value = True
    fake = tmp_path / "v.mp4"; fake.write_bytes(b"")
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.choose_source", return_value=("https://x/lo.mp4", None)), \
         patch("worker.main.probe_range_support", return_value=False), \
         patch("worker.main.download_to_file", return_value=(str(fake), 10)) as dl, \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=1)), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed"):
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
    assert rc == 0
    assert dl.call_args.args[0] == "https://x/lo.mp4"
    q.set_checkpoint.assert_any_call("JOB-1", "dl:0")


@pytest.mark.asyncio
async def test_run_once_oversize_giua_luong_chuyen_ban_ke(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job_addr(url_alt="https://x/lo.mp4")
    q.claim.return_value = True
    (tmp_path / "video.part").write_bytes(b"x")   # part cua ban goc phai bi xoa
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.choose_source", return_value=("https://x/v.mp4", None)), \
         patch("worker.main.probe_range_support", return_value=True), \
         patch("worker.main.download_chunk",
               side_effect=[OversizeError("file vượt giới hạn 1.90 GB"),
                            ("/tmp/p.part", 100)]) as dc, \
         patch("worker.main.checkpoint_dl_bytes", return_value=0), \
         patch("worker.main.assemble", return_value=str(tmp_path / "video.mp4")), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=1)), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed"):
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
    assert rc == 0
    assert dc.call_args_list == [
        call("https://x/v.mp4", start=0, dest_dir=str(tmp_path), on_progress=ANY),
        call("https://x/lo.mp4", start=0, dest_dir=str(tmp_path), on_progress=ANY),
    ]
    q.set_checkpoint.assert_any_call("JOB-1", "dl:0")
    assert not (tmp_path / "video.part").exists()


def test_permanent_substrings():
    from worker.main import _permanent
    assert _permanent("HTTP 400")
    assert _permanent("HTTP 403")
    assert _permanent("HTTP 404")
    assert _permanent("HTTP 410")
    assert _permanent("vượt giới hạn")
    assert _permanent("file rỗng")
    assert _permanent("ffprobe")
    assert _permanent("PeerIdInvalid")
    assert _permanent("ChatWriteForbidden")
    assert not _permanent("mất kết nối giữa chừng")
    assert not _permanent("không kết nối được Telegram: timeout")
    assert _permanent("HTTP 400 khi tải")
    assert _permanent("HTTP 403 khi tải")
    assert _permanent("HTTP 404 khi tải")
    assert _permanent("HTTP 410 khi tải")
    assert _permanent("file vượt giới hạn 2GB")
    assert _permanent("file rỗng")
    assert _permanent("ffprobe không đọc được video stream")
    assert _permanent("send_file thất bại: ... (PeerIdInvalidError)")
    assert _permanent("send_file thất bại: ... (ChatWriteForbiddenError)")
    assert not _permanent("HTTP 429 khi tải")
    assert not _permanent("mất kết nối giữa chừng")
    assert not _permanent("không kết nối được Telegram: timeout")


@pytest.mark.asyncio
async def test_run_once_chunked_resume_flow(tmp_path):
    q = MagicMock()
    row = make_job()
    row.checkpoint = "dl:0"
    q.next_job.return_value = row
    q.claim.return_value = True
    clock = scripted_clock(1000.0)
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.time", MagicMock(monotonic=clock)), \
         patch("worker.main.probe_range_support", return_value=True), \
         patch("worker.main.download_chunk", side_effect=[("/tmp/p.part", CHUNK_SIZE), ("/tmp/p.part", 10)]) as dc, \
         patch("worker.main.checkpoint_dl_bytes", side_effect=[0, CHUNK_SIZE]), \
         patch("worker.main.assemble", return_value=str(tmp_path / "video.mp4")), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed"), \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
        cb = dc.call_args_list[0].kwargs["on_progress"]
        cb(CHUNK_SIZE, CHUNK_SIZE + 10, 0.1)
    assert rc == 0
    assert dc.call_args_list == [
        call("https://x/v.mp4", start=0, dest_dir=str(tmp_path), on_progress=ANY),
        call("https://x/v.mp4", start=CHUNK_SIZE, dest_dir=str(tmp_path), on_progress=ANY),
    ]
    # giờ giả 1000s > 300s ⇒ nhịp tim phải được ghi
    q.touch.assert_called_with("JOB-1")
    assert q.set_checkpoint.call_args_list == [
        call("JOB-1", f"dl:{CHUNK_SIZE}"),
        call("JOB-1", f"dl:{CHUNK_SIZE + 10}"),
    ]


@pytest.mark.asyncio
async def test_on_progress_nhip_tim_theo_nguong_300s(tmp_path):
    q = MagicMock()
    row = make_job()
    row.checkpoint = "dl:0"
    q.next_job.return_value = row
    q.claim.return_value = True
    # 4 lần gọi callback, mỗi lần đọc 1 giá trị: 299 < 300, 300 = ngưỡng,
    # 599 (599-300=299 < 300), 900 (900-300=600 ≥ 300).
    clock = scripted_clock(299.0, 300.0, 599.0, 900.0)
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.time", MagicMock(monotonic=clock)), \
         patch("worker.main.probe_range_support", return_value=True), \
         patch("worker.main.download_chunk", side_effect=[("/tmp/p.part", CHUNK_SIZE), ("/tmp/p.part", 10)]) as dc, \
         patch("worker.main.checkpoint_dl_bytes", return_value=0), \
         patch("worker.main.assemble", return_value=str(tmp_path / "video.mp4")), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed"), \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
        cb = dc.call_args.kwargs["on_progress"]

        cb(CHUNK_SIZE, CHUNK_SIZE + 10, 0.1)      # 299s: chưa đủ 300s
        q.touch.assert_not_called()

        cb(CHUNK_SIZE, CHUNK_SIZE + 10, 0.2)      # đúng ngưỡng 300s
        q.touch.assert_called_once_with("JOB-1")

        cb(CHUNK_SIZE, CHUNK_SIZE + 10, 0.3)      # 599s: mới 299s kể từ nhịp trước
        assert q.touch.call_count == 1

        cb(CHUNK_SIZE, CHUNK_SIZE + 10, 0.4)      # 900s: 600s kể từ nhịp trước
        assert q.touch.call_count == 2
    assert rc == 0


@pytest.mark.asyncio
async def test_run_once_no_range_fallback_full_download(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    clock = scripted_clock(1000.0)
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.time", MagicMock(monotonic=clock)), \
         patch("worker.main.probe_range_support", return_value=False), \
         patch("worker.main.download_to_file", return_value=(str(tmp_path / "v.mp4"), 10)) as dl, \
         patch("worker.main.download_chunk") as dc, \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=1)), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed"), \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
        cb = dl.call_args.kwargs["on_progress"]
        cb(1, 2, 0.1)
    assert rc == 0
    dl.assert_called_once()
    dc.assert_not_called()
    q.touch.assert_called_with("JOB-1")
    q.set_checkpoint.assert_called_with("JOB-1", "dl:full")


@pytest.mark.asyncio
async def test_run_once_upload_dead_resets_checkpoint(tmp_path):
    from worker.uploader import UploadError
    q = MagicMock()
    row = make_job()
    q.next_job.return_value = row
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=True), \
         patch("worker.main.download_chunk", side_effect=[("/tmp/p.part", 100), ("/tmp/p.part", 0)]), \
         patch("worker.main.checkpoint_dl_bytes", side_effect=[0, 100]), \
         patch("worker.main.assemble", return_value=str(tmp_path / "video.mp4")), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(side_effect=UploadError("send_file thất bại: TimeoutError"))), \
         patch("worker.main.notify_failed") as nf, \
         patch("os.path.getsize", return_value=100), \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        with pytest.raises(UploadError):
            await run_once(make_cfg(dl_dir=str(tmp_path)))
    calls = [c.args for c in q.set_checkpoint.call_args_list]
    assert calls[-1][1] == "dl:full"


@pytest.mark.asyncio
async def test_run_once_download_chunk_transient_reraises(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=True), \
         patch("worker.main.download_chunk", side_effect=DownloadError("mất kết nối giữa chừng (chunk @0)")), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed") as nf:
        from worker.main import run_once
        with pytest.raises(DownloadError, match="mất kết nối"):
            await run_once(make_cfg(dl_dir=str(tmp_path)))
    assert "FAILED" not in statuses_written(q)
    nf.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_chunked_reset_retry_once_then_success(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    clock = scripted_clock(1000.0)
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.time", MagicMock(monotonic=clock)), \
         patch("worker.main.probe_range_support", return_value=True), \
         patch("worker.main.download_chunk", side_effect=[
             DownloadError("part file 0 bytes != checkpoint 100 — cần reset checkpoint"),
             ("/tmp/p.part", 50),
         ]) as dc, \
         patch("worker.main.checkpoint_dl_bytes", side_effect=[100, 0]), \
         patch("worker.main.assemble", return_value=str(tmp_path / "video.mp4")), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed"), \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
        cb = dc.call_args_list[0].kwargs["on_progress"]
        cb(1, 2, 0.1)
    assert rc == 0
    assert dc.call_args_list == [
        call("https://x/v.mp4", start=100, dest_dir=str(tmp_path), on_progress=ANY),
        call("https://x/v.mp4", start=0, dest_dir=str(tmp_path), on_progress=ANY),
    ]
    q.touch.assert_called_with("JOB-1")
    q.set_checkpoint.assert_any_call("JOB-1", "dl:0")
    q.set_checkpoint.assert_any_call("JOB-1", "dl:50")


@pytest.mark.asyncio
async def test_run_once_eof_416_after_full_chunk_assembles(tmp_path):
    q = MagicMock()
    q.next_job.return_value = make_job()
    q.claim.return_value = True
    with patch("worker.main.SheetQueue", return_value=q), \
         patch("worker.main.probe_range_support", return_value=True), \
         patch("worker.main.download_chunk", side_effect=[
             ("/tmp/p.part", CHUNK_SIZE),
             DownloadError("server không trả 206 cho Range (HTTP 416)"),
         ]), \
         patch("worker.main.checkpoint_dl_bytes", side_effect=[0, CHUNK_SIZE]), \
         patch("worker.main.assemble", return_value=str(tmp_path / "video.mp4")), \
         patch("worker.main.validate_video"), \
         patch("worker.main.upload_to_channel", AsyncMock(return_value=777)), \
         patch("worker.main.notify_done"), \
         patch("worker.main.notify_failed"), \
         patch("os.unlink") as unlink:
        from worker.main import run_once
        rc = await run_once(make_cfg(dl_dir=str(tmp_path)))
    assert rc == 0
    q.set_checkpoint.assert_called_with("JOB-1", "dl:full")
