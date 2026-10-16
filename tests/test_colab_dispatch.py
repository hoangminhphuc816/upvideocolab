from unittest.mock import MagicMock, patch

import types
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
    env_copy = {}
    ns = {"os": types.SimpleNamespace(environ=env_copy), "json": __import__("json"),
          "subprocess": MagicMock(), "sys": __import__("sys")}
    exec(snippet, ns)
    assert env_copy["TELETHON_SESSION"] == "sess"
    assert env_copy["WORKER_ID"] == "colab-abc123"
    assert "COLAB_TOKEN_JSON" not in env_copy


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


def test_dispatch_peeks_then_runs_colab(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    monkeypatch.setenv("WORKER_ID", "override-worker")
    q = MagicMock()
    job = MagicMock(job_id="JOB-1", status="PENDING", url="https://x/v.mp4")
    q.next_job.return_value = job
    run = MagicMock(return_value=MagicMock(returncode=0))
    history = tmp_path / "history"
    history.mkdir(parents=True)
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run) as run_mock, \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"), \
         patch("controller.colab_dispatch.HISTORY_DIR", history):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 0
    cmd = run_mock.call_args[0][0]
    assert cmd[:2] == ["colab", "run"]
    assert "--timeout" in cmd and "3600" in cmd
    bootstrap = cmd[cmd.index("--timeout") + 2]
    assert bootstrap.endswith(".py")
    q.claim.assert_not_called()
    token = tmp_path / ".config" / "colab-cli" / "token.json"
    assert token.exists() and token.read_text() == "{tok}"
    assert not history.exists()


def test_dispatch_job_permanent_failure_is_green(tmp_path, monkeypatch, capsys):
    """exit 2 + Sheet=FAILED (worker đã claim) → CI xanh + annotation warning."""
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    monkeypatch.setenv("WORKER_ID", "override-worker")
    q = MagicMock()
    q.next_job.return_value = MagicMock(job_id="JOB-1", status="PENDING", url="u")
    q.get_job.return_value = MagicMock(status="FAILED", worker="override-worker", error="HTTP 403")
    run = MagicMock(return_value=MagicMock(returncode=2))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run), \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 0
    out = capsys.readouterr().out
    assert "::warning::JOB-1" in out
    assert "HTTP 403" in out


def test_dispatch_worker_never_claimed_is_red(tmp_path, monkeypatch, capsys):
    """exit != 0 + worker chưa claim job → dispatch hỏng → CI đỏ + annotation error."""
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    monkeypatch.setenv("WORKER_ID", "override-worker")
    q = MagicMock()
    q.next_job.return_value = MagicMock(job_id="JOB-1", status="PENDING", url="u")
    q.get_job.return_value = MagicMock(status="PENDING", worker="", error="")
    run = MagicMock(return_value=MagicMock(returncode=1))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run), \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 1
    assert "::error::JOB-1" in capsys.readouterr().out


def test_dispatch_vm_died_midway_is_green(tmp_path, monkeypatch, capsys):
    """exit != 0 + worker đã claim nhưng job dở dang → sweep sẽ RETRY → CI xanh."""
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    monkeypatch.setenv("WORKER_ID", "override-worker")
    q = MagicMock()
    q.next_job.return_value = MagicMock(job_id="JOB-1", status="PENDING", url="u")
    q.get_job.return_value = MagicMock(status="DOWNLOADING", worker="override-worker", error="")
    run = MagicMock(return_value=MagicMock(returncode=1))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run), \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 0
    assert "::warning::JOB-1" in capsys.readouterr().out
