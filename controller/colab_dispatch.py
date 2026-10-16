"""Controller: peek queue, cấp Colab VM qua colab run, map exit code.

Exit-code semantics:
- `colab run` propagate exit code của script chạy trên VM (bootstrap →
  worker.main) VÀ dùng non-zero cho lỗi của chính CLI ⇒ exit code một mình
  không phân biệt được "job lỗi" với "đường dispatch hỏng".
- Vì vậy sau khi CLI kết thúc, controller đọc lại dòng job trên Sheet (nguồn
  sự thật) rồi mới quyết định: chỉ trả non-zero khi worker của LƯỢT NÀY
  không claim được job (dispatch thật sự hỏng). Job lỗi vĩnh viễn / VM dừng
  giữa chừng đã được worker ghi Sheet + notify owner ⇒ CI xanh kèm annotation
  `::warning::`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from worker.config import Config
from worker.sheet_queue import SheetQueue

TOKEN_PATH = Path.home() / ".config" / "colab-cli" / "token.json"
HISTORY_DIR = Path.home() / ".config" / "colab-cli" / "history"
# `--timeout` của `colab run` là HẠN TUYỆT ĐỐI cho cả cell (jupyter_kernel_client
# wsclient.py: deadline = monotonic() + timeout). In log KHÔNG gia hạn nó.
# Giá trị thật lấy từ env; Task 7 chốt chính sách.
COLAB_RUN_TIMEOUT_S = os.environ.get("COLAB_RUN_TIMEOUT_S", "3600")

_REQUIRED_ENV_KEYS = (
    "TELETHON_SESSION",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TARGET_CHANNEL",
    "OWNER_CHAT_ID",
    "GCP_SA_JSON",
    "SHEET_ID",
    "BOT_TOKEN",
)


def _materialize_token(colab_token_json: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(colab_token_json)
    # Settings là pydantic model có default cho mọi field ⇒ ghi thiếu field vẫn
    # hợp lệ. Tắt update-check cho log CI sạch + bớt 1 request mạng mỗi lần chạy.
    (TOKEN_PATH.parent / "settings.json").write_text(
        json.dumps({"enable_update_check": False})
    )


def _annotate(level: str, message: str) -> None:
    """Annotation hiện trên trang run của GitHub Actions (warning/error)."""
    print(f"::{level}::{message}", flush=True)


def _classify_run(queue: SheetQueue, job_id: str, worker_id: str, returncode: int) -> int:
    """Map exit code của `colab run` → exit code của CI (xem docstring đầu file).

    - worker của lượt này không claim được job (cột `worker` khác `worker_id`)
      ⇒ không có gì chạy ⇒ đỏ.
    - worker của lượt này đã claim ⇒ job-level: DONE/FAILED (đã ghi Sheet +
      notify) hoặc dở dang (sweep sẽ RETRY sau ≤15 phút) ⇒ xanh +
      `::warning::`.
    """
    if returncode == 0:
        return 0
    job = queue.get_job(job_id)
    if job is None:
        _annotate("error", f"{job_id}: colab run exit {returncode}, không đọc được dòng job trên Sheet")
        return 1
    if job.worker != worker_id:
        _annotate("error", f"{job_id}: colab run exit {returncode}, Sheet={job.status}, "
                            f"worker={job.worker} != {worker_id} — dispatch thất bại")
        return 1
    if job.status in ("DONE", "FAILED"):
        detail = f" ({job.error})" if job.error else ""
        _annotate("warning", f"{job_id}: worker kết thúc exit {returncode}, "
                             f"Sheet={job.status}{detail}")
        return 0
    _annotate("warning", f"{job_id}: worker dừng giữa chừng (exit {returncode}, "
                         f"Sheet={job.status}) — sweep sẽ RETRY")
    return 0


def _write_bootstrap(worker_env: dict[str, str], worker_id: str) -> str:
    repo = os.environ["WORKER_REPO"]
    payload = {
        **worker_env,
        "WORKER_ID": worker_id,
    }
    payload_json = json.dumps(json.dumps(payload))
    return f"""\
import json, os, subprocess, sys
os.environ.update(json.loads({payload_json}))


def run(cmd):
    # capture_output + print: ipykernel chỉ bắt output ở tầng Python, KHÔNG bắt
    # output tầng fd ⇒ subprocess.run để mặc định làm mất sạch log pip/git trên VM.
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    print("$ " + " ".join(cmd), flush=True)
    print(((proc.stdout or "") + (proc.stderr or ""))[-4000:], flush=True)
    if proc.returncode != 0:
        print("[bootstrap] lenh that bai:", proc.returncode, " ".join(cmd), flush=True)
        sys.exit(1)


run([sys.executable, "-m", "pip", "install", "-q",
     "gspread>=6.0,<7", "telethon==1.45.0", "cryptg", "requests"])
run(["git", "clone", "--depth", "1",
     "https://github.com/{repo}.git", "/content/app"])
sys.path.insert(0, "/content/app")
from worker.config import load_config
from worker.main import main as worker_main
sys.exit(worker_main())
"""


def dispatch_to_colab(cfg: Config, worker_env: dict[str, str], colab_token_json: str) -> int:
    _materialize_token(colab_token_json)
    queue = SheetQueue(cfg)
    job = queue.next_job()
    if job is None:
        return 0

    worker_id = os.environ.get("WORKER_ID", os.environ.get("GITHUB_RUN_ID", "local"))
    bootstrap = _write_bootstrap(worker_env, worker_id)
    fd, path = tempfile.mkstemp(suffix=".py", prefix="colab-bootstrap-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(bootstrap)
        cmd = ["colab", "run", "--timeout", COLAB_RUN_TIMEOUT_S, path]
        proc = subprocess.run(cmd, check=False)
        return _classify_run(queue, job.job_id, worker_id, proc.returncode)
    finally:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(HISTORY_DIR, ignore_errors=True)


def main() -> int:
    cfg = Config(
        telethon_session=os.environ["TELETHON_SESSION"],
        api_id=int(os.environ["TELEGRAM_API_ID"]),
        api_hash=os.environ["TELEGRAM_API_HASH"],
        target_channel=int(os.environ["TARGET_CHANNEL"]),
        owner_chat_id=int(os.environ["OWNER_CHAT_ID"]),
        sa_json=os.environ["GCP_SA_JSON"],
        sheet_id=os.environ["SHEET_ID"],
        bot_token=os.environ["BOT_TOKEN"],
        sheet_name=os.environ.get("SHEET_NAME", "Jobs"),
        dl_dir=os.environ.get("DL_DIR", "/tmp"),
    )
    worker_env = {k: os.environ[k] for k in _REQUIRED_ENV_KEYS}
    return dispatch_to_colab(cfg, worker_env, os.environ["COLAB_TOKEN_JSON"])


if __name__ == "__main__":
    sys.exit(main())
