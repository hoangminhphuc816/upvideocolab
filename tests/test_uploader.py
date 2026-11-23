from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from worker.config import Config
from worker.uploader import upload_to_channel, UploadError


def make_cfg():
    return Config(
        telethon_session="sess", api_id=1, api_hash="h",
        target_channel=-100123, owner_chat_id=42,
        sa_json="{}", sheet_id="S", bot_token="t",
        sheet_name="Jobs", dl_dir="/tmp",
    )


@pytest.mark.asyncio
async def test_upload_returns_message_id_and_disconnects():
    msg = MagicMock()
    msg.id = 777
    client = MagicMock()
    client.start = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_file = AsyncMock(return_value=msg)
    with patch("worker.uploader.TelegramClient", return_value=client) as tc, \
         patch("worker.uploader.StringSession"):
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert mid == 777
    args, kwargs = client.send_file.call_args
    assert args[0] == -100123
    assert kwargs["caption"] == "JOB-1"
    assert kwargs["supports_streaming"] is True
    assert callable(kwargs["progress_callback"])
    args_tc, kwargs_tc = tc.call_args
    assert kwargs_tc["flood_sleep_threshold"] == 120
    # Fingerprint client giả lập Telegram Desktop (runbook §3a)
    assert kwargs_tc["device_model"] == "Desktop"
    assert kwargs_tc["system_version"] == "Windows 11 x64"
    assert kwargs_tc["app_version"] == "7.2.8"
    assert kwargs_tc["lang_code"] == "en"
    assert kwargs_tc["system_lang_code"] == "en-US"
    assert "connections" not in kwargs_tc
    client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_upload_wraps_errors():
    client = MagicMock()
    client.start = AsyncMock()
    client.disconnect = AsyncMock()
    client.send_file = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"):
        with pytest.raises(UploadError, match="boom"):
            await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_upload_passes_generated_thumb():
    msg = MagicMock(); msg.id = 777
    client = MagicMock()
    client.start = AsyncMock(); client.disconnect = AsyncMock()
    client.send_file = AsyncMock(return_value=msg)
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 120)), \
         patch("worker.uploader.extract_thumbnail", return_value="/tmp/thumb.jpg") as et:
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert mid == 777
    assert et.call_args.kwargs.get("seek_seconds") == 30.0  # min(120/4, 30)
    kwargs = client.send_file.call_args.kwargs
    assert kwargs["thumb"] == "/tmp/thumb.jpg"


@pytest.mark.asyncio
async def test_upload_without_thumb_still_works():
    msg = MagicMock(); msg.id = 778
    client = MagicMock()
    client.start = AsyncMock(); client.disconnect = AsyncMock()
    client.send_file = AsyncMock(return_value=msg)
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 3)), \
         patch("worker.uploader.extract_thumbnail", return_value=None):
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert mid == 778
    assert client.send_file.call_args.kwargs["thumb"] is None


@pytest.mark.asyncio
async def test_upload_start_failure_wraps_and_disconnects():
    client = MagicMock()
    client.start = AsyncMock(side_effect=RuntimeError("start boom"))
    client.disconnect = AsyncMock()
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"):
        with pytest.raises(UploadError, match="start boom"):
            await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_upload_deletes_thumb_on_success_and_on_error(tmp_path):
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"\xff\xd8jpeg")
    msg = MagicMock(); msg.id = 779
    client = MagicMock()
    client.start = AsyncMock(); client.disconnect = AsyncMock()
    client.send_file = AsyncMock(return_value=msg)
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 120)), \
         patch("worker.uploader.extract_thumbnail", return_value=str(thumb)):
        await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert not thumb.exists()

    thumb.write_bytes(b"\xff\xd8jpeg")
    client.send_file = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 120)), \
         patch("worker.uploader.extract_thumbnail", return_value=str(thumb)), \
         pytest.raises(UploadError, match="boom"):
        await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert not thumb.exists()


@pytest.mark.asyncio
async def test_upload_goi_heartbeat_moi_tick_tien_do():
    """Upload >15 phut khong duoc de Sheet 'im lang' — heartbeat phai chay moi part."""
    msg = MagicMock(); msg.id = 900
    client = MagicMock()
    client.start = AsyncMock(); client.disconnect = AsyncMock()

    async def fake_send_file(*a, **kw):
        kw["progress_callback"](512 * 1024, 4 * 512 * 1024)   # part 1/4
        kw["progress_callback"](1024 * 1024, 4 * 512 * 1024)  # part 2/4
        return msg

    client.send_file = AsyncMock(side_effect=fake_send_file)
    beats = []
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(0, 0, 0)), \
         patch("worker.uploader.extract_thumbnail", return_value=None):
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1",
                                      heartbeat=lambda: beats.append(1))
    assert mid == 900
    assert len(beats) == 2


@pytest.mark.asyncio
async def test_upload_heartbeat_loi_khong_abort_upload():
    """Sheets 429/timeout khong duoc giet upload dang chay."""
    msg = MagicMock(); msg.id = 901
    client = MagicMock()
    client.start = AsyncMock(); client.disconnect = AsyncMock()

    async def fake_send_file(*a, **kw):
        kw["progress_callback"](512 * 1024, 1024 * 1024)
        return msg

    client.send_file = AsyncMock(side_effect=fake_send_file)

    def boom() -> None:
        raise RuntimeError("sheets down")

    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(0, 0, 0)), \
         patch("worker.uploader.extract_thumbnail", return_value=None):
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1", heartbeat=boom)
    assert mid == 901
