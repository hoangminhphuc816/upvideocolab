from unittest.mock import AsyncMock, MagicMock, patch

import binascii
import pytest
import struct

from worker.config import Config
from worker.uploader import upload_to_channel, UploadError


def make_cfg():
    return Config(
        telethon_session="sess", api_id=1, api_hash="h",
        target_channel=-100123, owner_chat_id=42,
        sa_json="{}", sheet_id="S", bot_token="t",
        sheet_name="Jobs", dl_dir="/tmp",
    )


def make_client(msg_id: int = 777, *, resolved_id: int = 4401263522):
    """Client giả có đủ API mà upload_to_channel gọi, kể cả get_entity.

    get_entity PHẢI là AsyncMock: code thật `await client.get_entity(...)`, và
    MagicMock thường sẽ ném TypeError khi await.
    """
    msg = MagicMock()
    msg.id = msg_id
    client = MagicMock()
    client.start = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_dialogs = AsyncMock()
    client.get_entity = AsyncMock(return_value=MagicMock(id=resolved_id))
    client.send_file = AsyncMock(return_value=msg)
    return client


@pytest.mark.asyncio
async def test_upload_resolve_kenh_truoc_khi_send():
    """Phải get_entity() tường minh, không dựa vào get_dialogs may rủi.

    get_dialogs bị bọc `except Exception: pass`: khi nó fail, entity cache rỗng,
    send_file gửi InputChannel(access_hash=0) => ChannelInvalidError =>
    ValueError: Could not find the input entity.
    """
    client = make_client()
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"):
        await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    client.get_entity.assert_awaited_once_with(-100123)
    assert client.send_file.call_args[0][0] is client.get_entity.return_value


@pytest.mark.asyncio
async def test_upload_bao_loi_ro_khi_khong_resolve_duoc_kenh():
    client = make_client()
    client.get_entity = AsyncMock(
        side_effect=ValueError("Could not find the input entity for PeerChannel(4401263522)"))
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"):
        with pytest.raises(UploadError, match="TARGET_CHANNEL"):
            await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    client.send_file.assert_not_called()

@pytest.mark.asyncio
async def test_upload_returns_message_id_and_disconnects():
    client = make_client()
    with patch("worker.uploader.TelegramClient", return_value=client) as tc, \
         patch("worker.uploader.StringSession"):
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert mid == 777
    args, kwargs = client.send_file.call_args
    assert args[0] is client.get_entity.return_value
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
    client.get_entity.assert_awaited_once_with(-100123)


@pytest.mark.asyncio
async def test_upload_wraps_errors():
    client = make_client()
    client.send_file = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"):
        with pytest.raises(UploadError, match="boom"):
            await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    client.disconnect.assert_awaited()
    client.get_entity.assert_awaited_once_with(-100123)


@pytest.mark.asyncio
async def test_upload_passes_generated_thumb():
    msg = MagicMock(); msg.id = 777
    client = make_client()
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 120)), \
         patch("worker.uploader.extract_thumbnail", return_value="/tmp/thumb.jpg") as et:
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert mid == 777
    assert et.call_args.kwargs.get("seek_seconds") == 30.0  # min(120/4, 30)
    kwargs = client.send_file.call_args.kwargs
    assert kwargs["thumb"] == "/tmp/thumb.jpg"
    client.get_entity.assert_awaited_once_with(-100123)


@pytest.mark.asyncio
async def test_upload_without_thumb_still_works():
    msg = MagicMock(); msg.id = 778
    client = make_client(778)
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 3)), \
         patch("worker.uploader.extract_thumbnail", return_value=None):
        mid = await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert mid == 778
    assert client.send_file.call_args.kwargs["thumb"] is None
    client.get_entity.assert_awaited_once_with(-100123)


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
    client = make_client(779)
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 120)), \
         patch("worker.uploader.extract_thumbnail", return_value=str(thumb)):
        await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert not thumb.exists()
    client.get_entity.assert_awaited_once_with(-100123)

    thumb.write_bytes(b"\xff\xd8jpeg")
    client.send_file = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("worker.uploader.TelegramClient", return_value=client), \
         patch("worker.uploader.StringSession"), \
         patch("worker.uploader.probe_video_metadata", return_value=(640, 360, 120)), \
         patch("worker.uploader.extract_thumbnail", return_value=str(thumb)), \
         pytest.raises(UploadError, match="boom"):
        await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
    assert not thumb.exists()
    assert client.get_entity.call_count == 2


@pytest.mark.asyncio
async def test_upload_goi_heartbeat_moi_tick_tien_do():
    """Upload >15 phut khong duoc de Sheet 'im lang' — heartbeat phai chay moi part."""
    msg = MagicMock(); msg.id = 900
    client = make_client(900)

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
    client.get_entity.assert_awaited_once_with(-100123)


@pytest.mark.asyncio
async def test_upload_heartbeat_loi_khong_abort_upload():
    """Sheets 429/timeout khong duoc giet upload dang chay."""
    msg = MagicMock(); msg.id = 901
    client = make_client(901)

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
    client.get_entity.assert_awaited_once_with(-100123)


@pytest.mark.asyncio
async def test_upload_session_hong_bao_loi_ro_rang():
    """Secret cắt cụt/sai/base64 lỗi/ASCII lạ → UploadError message ổn định,
    TelegramClient không được khởi tạo."""
    for err in [
        ValueError("Not a valid string"),
        binascii.Error("Incorrect padding"),
        binascii.Error("Invalid base64-encoded string: number of data characters (N) cannot be 1 more than a multiple of 4"),
        struct.error("unpack requires a buffer of 275 bytes"),
        ValueError("string argument should contain only ASCII characters"),
    ]:
        with patch("worker.uploader.StringSession", side_effect=err) as ss, \
             patch("worker.uploader.TelegramClient") as tc:
            with pytest.raises(UploadError, match="TELETHON_SESSION không hợp lệ"):
                await upload_to_channel(make_cfg(), "/tmp/v.mp4", "JOB-1")
        ss.assert_called_once_with("sess")
        tc.assert_not_called()
