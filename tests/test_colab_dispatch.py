from unittest.mock import MagicMock, patch

import pytest

from worker.config import Config
from controller.colab_dispatch import _write_bootstrap, dispatch_to_colab


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


def test_write_bootstrap_includes_env_worker_id_and_repo_excludes_token(monkeypatch):
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    src = _write_bootstrap(WORKER_ENV, worker_id="colab-abc123")
    assert "os.environ.update(json.loads(" in src
    assert "https://github.com/owner/repo.git" in src
    assert "COLAB_TOKEN_JSON" not in src
    assert "from worker.main import main as worker_main" in src
    assert "gspread>=6.0,<7" in src
    snippet = src.splitlines()[1]
    ns = {"os": __import__("os"), "json": __import__("json"),
          "subprocess": MagicMock(), "sys": __import__("sys")}
    exec(snippet, ns)
    env = ns["os"].environ
    assert env["TELETHON_SESSION"] == "sess"
    assert env["WORKER_ID"] == "colab-abc123"
    assert "COLAB_TOKEN_JSON" not in env


def test_dispatch_no_job_returns_zero_without_colab(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    monkeypatch.delenv("HOME", raising=False)
    q = MagicMock()
    q.next_job.return_value = None
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run") as run, \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{}")
    assert rc == 0
    run.assert_not_called()


def test_dispatch_claims_then_runs_colab(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    q = MagicMock()
    job = MagicMock(job_id="JOB-1", status="PENDING", url="https://x/v.mp4")
    q.next_job.return_value = job
    q.claim.return_value = True
    run = MagicMock(return_value=MagicMock(returncode=0))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run) as run_mock, \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 0
    cmd = run_mock.call_args[0][0]
    assert cmd[:2] == ["colab", "run"]
    assert "--timeout" in cmd and "3600" in cmd
    bootstrap = cmd[cmd.index("--timeout") + 2]
    assert bootstrap.endswith(".py")
    token = tmp_path / ".config" / "colab-cli" / "token.json"
    assert token.exists() and token.read_text() == "{tok}"


def test_dispatch_nonzero_exit_maps_transient_and_preserves_code(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    q = MagicMock()
    q.next_job.return_value = MagicMock(job_id="JOB-1", status="PENDING", url="u")
    q.claim.return_value = True
    run = MagicMock(return_value=MagicMock(returncode=2))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run), \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 2
    token = tmp_path / ".config" / "colab-cli" / "token.json"
    assert token.exists() and token.read_text() == "{tok}"
