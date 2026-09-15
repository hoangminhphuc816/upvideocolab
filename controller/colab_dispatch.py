"""Controller: peek queue, cấp Colab VM qua colab run, map exit code.

Exit-code semantics:
- 0: no job or job processed thành công trên VM.
- 1+: transient hoặc worker-permanent. Mọi non-zero được watchdog retry;
  permanent failure đã được worker ghi FAILED lên Sheet nên retry vô hại.
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
# Timeout kernel im lặng của colab run — iopub liên tục sẽ reset; 1h an toàn
COLAB_RUN_TIMEOUT_S = "3600"

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


def _write_bootstrap(worker_env: dict[str, str], worker_id: str) -> str:
    repo = os.environ["WORKER_REPO"]
    payload = {
        **worker_env,
        "WORKER_ID": worker_id,
    }
    return f"""\
import json, os, subprocess, sys
os.environ.update(json.loads({json.dumps(json.dumps(payload))}))
subprocess.run([sys.executable, "-m", "pip", "install", "-q",
    "gspread>=6.0,<7", "telethon==1.45.0", "cryptg", "requests"], check=True)
subprocess.run(["git", "clone", "--depth", "1",
    "https://github.com/{repo}.git", "/content/app"], check=True)
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

    bootstrap = _write_bootstrap(worker_env, os.environ.get("WORKER_ID", os.environ.get("GITHUB_RUN_ID", "local")))
    fd, path = tempfile.mkstemp(suffix=".py", prefix="colab-bootstrap-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(bootstrap)
        cmd = ["colab", "run", "--timeout", COLAB_RUN_TIMEOUT_S, path]
        proc = subprocess.run(cmd, check=False)
        return proc.returncode
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
