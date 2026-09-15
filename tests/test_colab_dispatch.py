from unittest.mock import MagicMock, patch

import pytest

from worker.config import Config
from controller.colab_dispatch import dispatch_to_colab, build_env_args


def make_cfg():
    return Config(
        telethon_session="s", api_id=1, api_hash="h",
        target_channel=-100123, owner_chat_id=42,
        sa_json="{}", sheet_id="S", bot_token="t",
        sheet_name="Jobs", dl_dir="/tmp",
    )


WORKER_ENV = {
    "TELETHON_SESSION": "sess",
    "TELEGRAM_API_ID": "1",
    "TELEGRAM_API_HASH": "h",
    "TARGET_CHANNEL": "-100123",
    "OWNER_CHAT_ID": "42",
    "GCP_SA_JSON": "{}",
    "SHEET_ID": "S",
    "BOT_TOKEN": "t",
}


def test_build_env_args_includes_worker_id_and_not_token_json():
    args = build_env_args(WORKER_ENV, worker_id="colab-abc123")
    envs = [a for a in args if a.startswith("--env ")]
    joined = " ".join(envs)
    assert "WORKER_ID=colab-abc123" in joined
    assert "COLAB_TOKEN_JSON" not in joined
    assert "TELETHON_SESSION=sess" in joined


def test_dispatch_no_job_returns_zero_without_colab(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    q = MagicMock()
    q.next_job.return_value = None
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run") as run:
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{}")
    assert rc == 0
    run.assert_not_called()


def test_dispatch_claims_then_runs_colab(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    token_path = tmp_path / ".config" / "colab-cli" / "token.json"
    q = MagicMock()
    job = MagicMock(job_id="JOB-1", status="PENDING", url="https://x/v.mp4")
    q.next_job.return_value = job
    q.claim.return_value = True
    run = MagicMock(return_value=MagicMock(returncode=0))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run) as run_mock, \
         patch("controller.colab_dispatch.TOKEN_PATH", token_path):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 0
    cmd = run_mock.call_args[0][0]
    assert "colab" in cmd[0] or "colab" in " ".join(cmd[:3])
    assert "--timeout" in cmd and "3600" in cmd
    assert "worker.py" in cmd[-1]
    token = tmp_path / ".config" / "colab-cli" / "token.json"
    assert token.exists() and token.read_text() == "{tok}"


def test_dispatch_nonzero_exit_maps_transient(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    token_path = tmp_path / ".config" / "colab-cli" / "token.json"
    q = MagicMock()
    q.next_job.return_value = MagicMock(job_id="JOB-1", status="PENDING", url="u")
    q.claim.return_value = True
    run = MagicMock(return_value=MagicMock(returncode=1))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run), \
         patch("controller.colab_dispatch.TOKEN_PATH", token_path):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 1
