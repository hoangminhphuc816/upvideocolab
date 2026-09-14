from unittest.mock import MagicMock, patch

import pytest

from worker.config import Config
from worker.notifier import notify_done, notify_failed, NotifyError


def make_cfg():
    return Config(
        telethon_session="s", api_id=1, api_hash="h",
        target_channel=-100123, owner_chat_id=42,
        sa_json="{}", sheet_id="S", bot_token="123:ABC",
        sheet_name="Jobs", dl_dir="/tmp",
    )


def ok_response():
    r = MagicMock()
    r.status_code = 200
    r.json = lambda: {"ok": True}
    return r


def test_notify_done_sends_to_owner():
    with patch("worker.notifier.requests.post", return_value=ok_response()) as post:
        notify_done(make_cfg(), "JOB-1", 555)
    url = post.call_args[0][0]
    assert "sendMessage" in url and "123:ABC" in url
    body = post.call_args[1]["json"]
    assert body["chat_id"] == 42
    assert "JOB-1" in body["text"]
    assert "555" in body["text"]


def test_notify_failed_sends_reason():
    with patch("worker.notifier.requests.post", return_value=ok_response()) as post:
        notify_failed(make_cfg(), "JOB-1", "timeout")
    body = post.call_args[1]["json"]
    assert body["chat_id"] == 42
    assert "JOB-1" in body["text"]
    assert "timeout" in body["text"]


def test_notify_bot_api_error_raises():
    bad = MagicMock()
    bad.status_code = 400
    bad.json = lambda: {"ok": False, "description": "Bad Request: chat not found"}
    with patch("worker.notifier.requests.post", return_value=bad):
        with pytest.raises(NotifyError, match="chat not found"):
            notify_done(make_cfg(), "JOB-1", 555)
