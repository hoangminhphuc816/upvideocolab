"""Test tool chẩn đoán TARGET_CHANNEL — KHÔNG chạm Telegram thật."""
from unittest.mock import AsyncMock, MagicMock, patch

import subprocess
import sys

import pytest
from telethon.tl import types

from tests.test_uploader import make_cfg


def make_diag_client(*, is_admin=True, access_hash=123456789, entity_factory=None):
    client = MagicMock()
    client.connect = AsyncMock()
    client.start = AsyncMock()
    client.disconnect = AsyncMock()
    client.get_dialogs = AsyncMock(return_value=[MagicMock(), MagicMock()])

    if entity_factory is None:
        def entity_factory():
            return types.Channel(
                id=4401263522, title="Kênh test", photo=types.PhotoEmpty(id=0),
                date=None, access_hash=access_hash)

    entity = entity_factory()
    client.get_entity = AsyncMock(return_value=entity)
    perms = MagicMock()
    perms.is_admin = is_admin
    perms.is_creator = False
    client.get_permissions = AsyncMock(return_value=perms)
    return client


@pytest.mark.asyncio
async def test_diagnose_bao_du_thong_tin():
    from tools.check_telegram_target import diagnose
    client = make_diag_client()
    with patch("tools.check_telegram_target.TelegramClient", return_value=client), \
         patch("tools.check_telegram_target.StringSession"):
        info = await diagnose(make_cfg())
    assert info["entity_id"] == 4401263522
    assert info["access_hash"] == 123456789
    assert info["is_admin"] is True
    assert info["dialogs"] == 2
    assert info["needs_hash"] is True
    client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_diagnose_access_hash_0_canh_bao():
    """Channel without access_hash -> TypeError from get_input_peer -> access_hash=0, needs_hash=True."""
    from tools.check_telegram_target import diagnose
    client = make_diag_client(access_hash=None)
    with patch("tools.check_telegram_target.TelegramClient", return_value=client), \
         patch("tools.check_telegram_target.StringSession"):
        info = await diagnose(make_cfg())
    assert info["access_hash"] == 0
    assert info["needs_hash"] is True


@pytest.mark.asyncio
async def test_diagnose_loi_resolve_nem_upload_error():
    from tools.check_telegram_target import diagnose
    from worker.uploader import UploadError
    client = make_diag_client()
    client.get_entity = AsyncMock(
        side_effect=ValueError("Could not find the input entity for PeerChannel(channel_id=1)"))
    with patch("tools.check_telegram_target.TelegramClient", return_value=client), \
         patch("tools.check_telegram_target.StringSession"):
        with pytest.raises(UploadError):
            await diagnose(make_cfg())


def test_tool_chay_duoc_tu_duong_dan_docs(tmp_path):
    """`.venv/bin/python tools/check_telegram_target.py` phải import được.

    Chạy như `main(argv)` thật: `sys.path[0]` là thư mục của script (`tools/`),
    nên `import worker...` chỉ chạy được nhờ `sys.path.insert` trong tool.
    `-I` (isolated) tắt PYTHONPATH kế thừa; `cwd=tmp_path` để repo root không
    lọt vào path qua thư mục hiện tại.
    """
    from pathlib import Path
    tool = Path(__file__).resolve().parent.parent / "tools" / "check_telegram_target.py"
    r = subprocess.run(
        [sys.executable, "-I", str(tool)],
        cwd=tmp_path, capture_output=True, text=True)
    assert "ModuleNotFoundError" not in r.stderr, r.stderr
    # Không có env -> load_config() ném ValueError; đó là dấu hiệu import ĐÃ chạy.
    assert "Thiếu biến môi trường" in r.stderr or "ValueError" in r.stderr, r.stderr


@pytest.mark.asyncio
async def test_diagnose_nhom_cu_khong_can_hash():
    """Nhóm cũ `-123…` tạo InputPeerChat: không cần access_hash."""

    def chat_factory():
        return MagicMock()

    from tools.check_telegram_target import diagnose, utils as diag_utils
    from telethon.tl import types as tl_types

    client = make_diag_client(entity_factory=chat_factory)
    with patch("tools.check_telegram_target.TelegramClient", return_value=client), \
         patch("tools.check_telegram_target.StringSession"), \
         patch.object(diag_utils, "get_input_peer", return_value=tl_types.InputPeerChat(chat_id=123456)):
        info = await diagnose(make_cfg())
    assert info["needs_hash"] is False
    assert info["access_hash"] == 0
