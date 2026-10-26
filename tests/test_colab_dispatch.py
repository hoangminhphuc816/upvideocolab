from unittest.mock import MagicMock, patch

import json
import types
import pytest

from worker.config import Config
from worker.sheet_queue import Job
from controller.colab_dispatch import _write_bootstrap, _materialize_token, dispatch_to_colab, _classify_run


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


def test_write_bootstrap_captures_subprocess_output(monkeypatch):
    """Output của pip/git phải được in ở tầng Python, nếu không ipykernel nuốt mất
    (không bắt được output tầng fd) → log CI trống, không chẩn đoán được."""
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    src = _write_bootstrap(WORKER_ENV, worker_id="colab-abc123")
    assert "capture_output=True" in src
    assert "print(" in src


def test_materialize_token_writes_settings(tmp_path):
    """settings.json phải được ghi cùng token để tắt update-check trong CI."""
    token = tmp_path / ".config" / "colab-cli" / "token.json"
    with patch("controller.colab_dispatch.TOKEN_PATH", token):
        _materialize_token("{tok}")
    settings = token.parent / "settings.json"
    assert json.loads(settings.read_text()) == {"enable_update_check": False}


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
    q.count_done_since.return_value = 0
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
    assert "--timeout" in cmd and "7200" in cmd
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
    q.count_done_since.return_value = 0
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
    q.count_done_since.return_value = 0
    q.get_job.return_value = MagicMock(status="PENDING", worker="", error="")
    run = MagicMock(return_value=MagicMock(returncode=1))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run), \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 1
    assert "::error::JOB-1" in capsys.readouterr().out


def test_dispatch_retry_with_stale_worker_is_red(tmp_path, monkeypatch, capsys):
    """Job bị sweep về RETRY nhưng cột worker vẫn giữ id của run CŨ ⇒ lượt này
    không claim được job ⇒ dispatch hỏng ⇒ phải ĐỎ (không được false-green)."""
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    monkeypatch.setenv("WORKER_ID", "override-worker")
    q = MagicMock()
    q.next_job.return_value = MagicMock(job_id="JOB-1", status="RETRY", url="u")
    q.count_done_since.return_value = 0
    q.get_job.return_value = MagicMock(status="RETRY", worker="colab-old-run", error="")
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
    q.count_done_since.return_value = 0
    q.get_job.return_value = MagicMock(status="DOWNLOADING", worker="override-worker", error="")
    run = MagicMock(return_value=MagicMock(returncode=1))
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run", run), \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{tok}")
    assert rc == 0
    assert "::warning::JOB-1" in capsys.readouterr().out


def test_dispatch_throttle_du_4_upload_thi_khong_goi_colab(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    q = MagicMock()
    q.next_job.return_value = Job("JOB-1", "PENDING", "u", 42, 100, None, "", None, "", 0, "0", 2)
    q.count_done_since.return_value = 4          # đã đủ 4 upload hôm nay
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run") as run, \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{}")
    assert rc == 0
    run.assert_not_called()                      # KHÔNG tốn session Colab


def test_dispatch_duoi_nguong_thi_van_chay_colab(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    q = MagicMock()
    q.next_job.return_value = Job("JOB-1", "PENDING", "u", 42, 100, None, "", None, "", 0, "0", 2)
    q.count_done_since.return_value = 3
    q.get_job.return_value = Job("JOB-1", "DONE", "u", 42, 100, 200, "local", None, "", 0, "0", 2)
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run") as run, \
         patch("controller.colab_dispatch.TOKEN_PATH", tmp_path / ".config" / "colab-cli" / "token.json"):
        run.return_value = types.SimpleNamespace(returncode=0)
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV, colab_token_json="{}")
    assert rc == 0
    run.assert_called_once()                     # còn quota => chạy như cũ


def test_classify_run_ghi_ly_do_khi_worker_dung_giua_chung(capsys):
    """Job bị cắt giữa chừng: CI vẫn xanh (sweep sẽ RETRY) nhưng phải để lại
    lý do trong cột error để lần sau không mò."""
    job = Job("JOB-1", "DOWNLOADING", "u", 42, 100, 200, "colab-run1", None, "", 0, "dl:5", 2)
    q = MagicMock()
    q.get_job.return_value = job
    assert _classify_run(q, "JOB-1", "colab-run1", 1) == 0
    assert "::warning::JOB-1" in capsys.readouterr().out
    q.set_status.assert_called_once()
    kwargs = q.set_status.call_args.kwargs
    assert kwargs["error"].startswith("worker dừng giữa chừng")
    assert "7200" in kwargs["error"]


def test_classify_run_khong_ghi_de_error_da_co(capsys):
    job = Job("JOB-1", "DOWNLOADING", "u", 42, 100, 200, "colab-run1", None, "lỗi cũ", 0, "dl:5", 2)
    q = MagicMock()
    q.get_job.return_value = job
    assert _classify_run(q, "JOB-1", "colab-run1", 1) == 0
    assert "::warning::JOB-1" in capsys.readouterr().out
    q.set_status.assert_not_called()


def test_classify_run_bo_qua_when_status_khong_hop_le(capsys):
    job = Job("JOB-1", "GHOST", "u", 42, 100, 200, "colab-run1", None, "", 0, "dl:5", 2)
    q = MagicMock()
    q.get_job.return_value = job
    assert _classify_run(q, "JOB-1", "colab-run1", 1) == 0
    out = capsys.readouterr().out
    assert "::warning::JOB-1" in out
    assert "status lạ 'GHOST'" in out
    q.set_status.assert_not_called()


def test_dispatch_goi_release_waiting_truoc_next_job(tmp_path, monkeypatch):
    from unittest.mock import ANY

    monkeypatch.setenv("WORKER_REPO", "owner/repo")
    q = MagicMock()
    q.release_waiting.return_value = 0
    q.next_job.return_value = None
    with patch("controller.colab_dispatch.SheetQueue", return_value=q), \
         patch("controller.colab_dispatch.subprocess.run") as run, \
         patch("controller.colab_dispatch.TOKEN_PATH",
               tmp_path / ".config" / "colab-cli" / "token.json"):
        rc = dispatch_to_colab(make_cfg(), worker_env=WORKER_ENV,
                              colab_token_json="{}")
    assert rc == 0
    assert [c[0] for c in q.method_calls] == ["release_waiting", "next_job"]
    q.release_waiting.assert_called_once_with(ANY)
    q.next_job.assert_called_once()
    run.assert_not_called()
