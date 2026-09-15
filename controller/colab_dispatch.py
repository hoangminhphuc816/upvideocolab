"""Controller: claim job từ Sheet, cấp Colab VM qua colab run, map exit code."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from worker.config import Config
from worker.sheet_queue import SheetQueue

TOKEN_PATH = Path.home() / ".config" / "colab-cli" / "token.json"
# Timeout kernel im lặng của colab run — iopub liên tục sẽ reset; 1h an toàn
COLAB_RUN_TIMEOUT_S = "3600"


def build_env_args(worker_env: dict[str, str], worker_id: str) -> list[str]:
    args: list[str] = []
    for key, value in worker_env.items():
        args.append(f"--env {key}={value}")
    args.append(f"--env WORKER_ID={worker_id}")
    return args


def _materialize_token(colab_token_json: str) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(colab_token_json)


def dispatch_to_colab(cfg: Config, worker_env: dict[str, str], colab_token_json: str) -> int:
    """Claim 1 job; nếu không có job → 0 (không cấp VM). Chạy colab run, trả exit code."""
    _materialize_token(colab_token_json)
    queue = SheetQueue(cfg)
    job = queue.next_job()
    if job is None:
        return 0
    worker_id = os.environ.get("GITHUB_RUN_ID", "local")
    worker_id = f"colab-{worker_id}"
    if not queue.claim(job.job_id, worker_id):
        return 0  # worker khác thắng race

    cmd = ["colab", "run"]
    cmd.extend(build_env_args(worker_env, worker_id))
    cmd.extend(["--timeout", COLAB_RUN_TIMEOUT_S, "worker.py"])
    proc = subprocess.run(cmd, check=False)
    return proc.returncode
