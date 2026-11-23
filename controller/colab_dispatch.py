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
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from worker.config import Config
from worker.sheet_queue import SheetQueue, STATUSES

TOKEN_PATH = Path.home() / ".config" / "colab-cli" / "token.json"
HISTORY_DIR = Path.home() / ".config" / "colab-cli" / "history"
# `--timeout` của `colab run` là HẠN TUYỆT ĐỐI cho cả cell (jupyter_kernel_client
# wsclient.py: deadline = monotonic() + timeout) — in log KHÔNG gia hạn nó.
# 7200s là chính sách đã chốt: VM vẫn được CLI release ngay khi cell xong, nên hạn
# dài không giữ VM idle. Đổi bằng env COLAB_RUN_TIMEOUT_S.
COLAB_RUN_TIMEOUT_S = os.environ.get("COLAB_RUN_TIMEOUT_S", "7200")

# `timeout-minutes` của job trong .github/workflows/worker.yml phải LỚN HƠN hạn cell
# cộng biên teardown: runner cancel = CLI không kịp teardown = VM bị bỏ rơi = 2 IP cùng
# auth key (AuthKeyDuplicatedError). Đổi 1 trong 2 số phải đổi cả cặp.
JOB_TIMEOUT_MIN = 140
TEARDOWN_MARGIN_S = 600


def check_timeout_budget(run_timeout_s: int, job_timeout_min: int) -> None:
    if run_timeout_s + TEARDOWN_MARGIN_S > job_timeout_min * 60:
        raise ValueError(
            f"ngân sách thời gian không an toàn: colab run --timeout={run_timeout_s}s + "
            f"biên {TEARDOWN_MARGIN_S}s > timeout-minutes={job_timeout_min} phút. "
            f"Runner sẽ cancel giữa cell và bỏ rơi VM (AuthKeyDuplicatedError). "
            f"Sửa .github/workflows/worker.yml hoặc COLAB_RUN_TIMEOUT_S."
        )

TZ_VN = "Asia/Ho_Chi_Minh"  # UTC+7, không có DST
DAILY_UPLOAD_LIMIT = int(os.environ.get("DAILY_UPLOAD_LIMIT", "4"))
# Retry chỉ khi VM CHƯA claim job (cột worker != worker_id). Assign timeout của
# google-auth là 120s/lần; 3 lần + backoff 20/40s đủ vượt spike mà không đốt
# hạn job (JOB_TIMEOUT_MIN). Không retry sau khi worker đã claim: tránh 2 VM cùng
# xử lý một job.
DISPATCH_ATTEMPTS = 3
DISPATCH_BACKOFF_S = 20
_sleep = time.sleep


def day_start_epoch(now: float | None = None) -> int:
    """Epoch (giây) của 00:00 hôm nay theo giờ VN."""
    tz = ZoneInfo(TZ_VN)
    cur = datetime.fromtimestamp(now, tz) if now is not None else datetime.now(tz)
    return int(cur.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

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

# Lỗi session Telegram vĩnh viễn + cấu hình sai: retry không cứu được, cần sửa tay.
AUTH_KEY_MARKERS = ("AuthKeyDuplicated", "AuthKeyUnregistered", "AuthKeyInvalid",
                    "SessionRevoked", "UserDeactivated",
                    "Not a valid string", "Incorrect padding",
                    "unpack requires a buffer", "ApiIdInvalid",
                    "TELETHON_SESSION không hợp lệ")


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


def _stop_orphan_assignments() -> int:
    """Unassign mọi VM còn sống trên tài khoản, ngay trước khi cấp VM mới.

    Concurrency group chỉ serialize các run; run bị cancel ở `timeout-minutes`
    KHÔNG teardown được VM (CLI 0.6.0 không có signal handler) ⇒ VM mồ côi vẫn giữ
    kernel đang upload với cùng TELETHON_SESSION. Dọn trước là điều kiện để giữ
    bất biến "1 session = 1 client".

    PHẠM VI: trên runner, `sync_sessions()` không thấy `sessions.json` local nên trả
    về TOÀN BỘ assignment của tài khoản Google đang cấu hình (`COLAB_TOKEN_JSON`) —
    không phân biệt VM do pipeline tạo hay do người dùng tự mở. Vì vậy tài khoản đó
    phải DÀNH RIÊNG cho pipeline; VM thủ công trên cùng account sẽ bị unassign ở mọi
    lượt dispatch (kể cả sweep chạy `if: always()`). Xem runbook §10d.
    """
    from colab_cli.common import state

    try:
        _sessions, assignments = state.sync_sessions()
    except SystemExit:
        _annotate("warning", "colab sessions: auth lỗi — bỏ qua bước dọn VM mồ côi")
        return 0
    stopped = 0
    for a in assignments:
        try:
            state.client.unassign(a.endpoint)
            stopped += 1
        except Exception as e:  # best-effort: không được chặn dispatch
            _annotate("warning", f"không unassign được {a.endpoint}: {e}")
    if stopped:
        _annotate("warning", f"đã unassign {stopped} Colab assignment mồ côi trước khi dispatch")
    return stopped


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
                            f"worker={job.worker} != {worker_id} — dispatch thất bại; "
                            f"job vẫn PENDING/RETRY, sweep sau sẽ thử lại")
        return 1
    if job.error and any(m in job.error for m in AUTH_KEY_MARKERS):
        _annotate("error", f"{job_id}: session Telegram đã chết ({job.error[:180]}). "
                           f"Retry vô ích — chạy tools/make_session.py, cập nhật secret "
                           f"TELETHON_SESSION (runbook §3), rồi dispatch lại.")
        return 1
    if job.status in ("DONE", "FAILED"):
        detail = f" ({job.error})" if job.error else ""
        _annotate("warning", f"{job_id}: worker kết thúc exit {returncode}, "
                             f"Sheet={job.status}{detail}")
        return 0
    if job.status not in STATUSES:
        _annotate("warning", f"{job_id}: colab run exit {returncode}, "
                             f"Sheet status lạ '{job.status}' — bỏ qua ghi error; "
                             f"cần sửa tay cột status (sweep chỉ xử lý "
                             f"CLAIMED/DOWNLOADING/UPLOADING)")
        return 0
    if not job.error:
        queue.set_status(
            job.job_id,
            job.status,
            error=f"worker dừng giữa chừng (exit {returncode}) — có thể vượt hạn "
                  f"COLAB_RUN_TIMEOUT_S={COLAB_RUN_TIMEOUT_S}s",
        )
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


print("[bootstrap] python", sys.version, flush=True)

deps = ["gspread>=6.0,<7", "telethon==1.45.0",
       "cryptg==0.5.2", "requests"]
if sys.version_info >= (3, 15):
    # cryptg 0.5.2 ships wheels through cp314; on 3.15+ there is no wheel
    # yet, and a source build needs Rust on the VM (unavailable). Skip it and
    # let Telethon fall back via its normal chain cryptg -> libssl ->
    # pyaes (Colab/Ubuntu with OpenSSL stays fast via libssl; pure-Python
    # pyaes only if libssl is also missing).
    deps.remove("cryptg==0.5.2")
    print("[bootstrap] Python >= 3.15: cryptg khong có wheel — bỏ qua, "
          "Telethon dung fallback cryptg -> libssl -> pyaes "
          "(Colab/Ubuntu co OpenSSL thi libssl, van nhanh)",
          flush=True)
run([sys.executable, "-m", "pip", "install", "-q", *deps])
run(["git", "clone", "--depth", "1",
     "https://github.com/{repo}.git", "/content/app"])
sys.path.insert(0, "/content/app")
from worker.config import load_config
from worker.main import main as worker_main
sys.exit(worker_main())
"""


def _job_claimed(queue: SheetQueue, job_id: str, worker_id: str) -> bool:
    """True nếu VM của run này đã claim job (cột worker đã ghi)."""
    job = queue.get_job(job_id)
    return job is not None and job.worker == worker_id


def _run_colab_with_retry(cmd: list[str], queue: SheetQueue, job_id: str,
                          worker_id: str, attempts: int = DISPATCH_ATTEMPTS,
                          backoff: float = DISPATCH_BACKOFF_S) -> int:
    """Chạy `colab run`, retry khi thất bại TRƯỚC lúc VM claim job.

    Assign timeout / kernel-startup failure đều để cột worker rỗng ⇒ retry an toàn
    (chưa có job nào bị xử lý dở). Khi worker đã claim thì trả nguyên exit code để
    `_classify_run` áp semantics cũ (giữa chừng → xanh + warning, sweep RETRY).
    """
    returncode = 1
    for attempt in range(1, attempts + 1):
        returncode = subprocess.run(cmd, check=False).returncode
        if returncode == 0:
            return 0
        if _job_claimed(queue, job_id, worker_id):
            return returncode
        if attempt == attempts:
            break
        nxt = queue.next_job()
        if nxt is None or nxt.job_id != job_id:
            return returncode
        wait = backoff * (2 ** (attempt - 1))
        _annotate("warning", f"{job_id}: colab run exit {returncode} trước khi worker "
                             f"claim job (lần {attempt}/{attempts}) — thử lại sau {wait:.0f}s")
        _sleep(wait)
    return returncode


def dispatch_to_colab(cfg: Config, worker_env: dict[str, str], colab_token_json: str) -> int:
    _materialize_token(colab_token_json)
    queue = SheetQueue(cfg)
    try:
        released = queue.release_waiting(day_start_epoch())
    except Exception as e:  # release là best-effort; không được giết dispatch
        _annotate("warning", f"release_waiting lỗi — bỏ qua: {e}")
        released = 0
    if released:
        print(f"::notice::Đã giải phóng {released} job WAITING sang PENDING "
              f"(sau 00:00 VN).")
    job = queue.next_job()
    if job is None:
        return 0

    done_today = queue.count_done_since(day_start_epoch())
    if done_today >= DAILY_UPLOAD_LIMIT:
        print(
            f"::notice::Đã đủ {done_today}/{DAILY_UPLOAD_LIMIT} upload hôm nay (giờ VN) — "
            f"không tạo session Colab; job {job.job_id} ở lại hàng đợi cho mai."
        )
        return 0

    worker_id = os.environ.get("WORKER_ID", os.environ.get("GITHUB_RUN_ID", "local"))

    # Dọn VM mồ côi NGAY TRUOC khi thuê VM — không phải trước khi biết có job.
    # Chạy sớm hơn sẽ unassign một VM đang upload khoẻ (heartbeat giữ job UPLOADING
    # nên next_job() không thấy nó), biến một lượt upload sắp xong thành session mới
    # phải tải lại từ đầu — đúng thứ branch này chống.
    try:
        _stop_orphan_assignments()
    except Exception as e:  # phòng hờ import/attr sai ở version CLI khác
        _annotate("warning", f"dọn VM mồ côi lỗi — bỏ qua: {e}")

    bootstrap = _write_bootstrap(worker_env, worker_id)
    fd, path = tempfile.mkstemp(suffix=".py", prefix="colab-bootstrap-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(bootstrap)
        cmd = ["colab", "run", "--timeout", COLAB_RUN_TIMEOUT_S, path]
        try:
            returncode = _run_colab_with_retry(cmd, queue, job.job_id, worker_id)
        except FileNotFoundError:
            _annotate("error", f"{job.job_id}: không tìm thấy binary 'colab' trên runner — dispatch thất bại")
            return 1
        return _classify_run(queue, job.job_id, worker_id, returncode)
    finally:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        shutil.rmtree(HISTORY_DIR, ignore_errors=True)


def main() -> int:
    missing = [k for k in (*_REQUIRED_ENV_KEYS, "COLAB_TOKEN_JSON", "WORKER_REPO")
               if not os.environ.get(k)]
    if missing:
        raise ValueError(f"thiếu biến môi trường bắt buộc: {', '.join(sorted(missing))}")
    check_timeout_budget(int(COLAB_RUN_TIMEOUT_S), JOB_TIMEOUT_MIN)
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
