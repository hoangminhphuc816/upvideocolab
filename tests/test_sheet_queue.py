"""Test SheetQueue logic với FakeSheet — KHÔNG chạm gspread/Sheet thật.

SheetQueue.__init__ dựng gspread client thật, nên các test dựng object qua
__new__ và gán thủ công các attribute cần thiết (cfg, sheet, _col, _job_cache).
"""
from unittest.mock import MagicMock

from worker.config import Config
from worker.sheet_queue import SheetQueue, Job, COLUMNS

# COLUMNS (đúng thứ tự): job_id, status, url, chat_id, created_at, updated_at,
#                        worker, msg_id, error, retry_count, checkpoint
HDR = list(COLUMNS)


def make_cfg():
    return Config(
        telethon_session="s", api_id=1, api_hash="h",
        target_channel=-100123, owner_chat_id=42,
        sa_json='{"type": "service_account"}', sheet_id="SID",
        bot_token="t", sheet_name="Jobs", dl_dir="/tmp",
    )


class FakeSheet:
    """Worksheet giả với đúng các method gspread mà SheetQueue dùng."""

    def __init__(self, rows):
        self.rows = [list(r) for r in rows]
        self.worksheet = MagicMock()

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def update_cell(self, r, c, value):
        self.rows[r - 1][c - 1] = str(value)

    def append_row(self, row):
        self.rows.append([str(v) for v in row])

    def insert_row(self, row, index):
        self.rows.insert(index - 1, [str(v) for v in row])


def q_with(rows, now_ts=None):
    q = SheetQueue.__new__(SheetQueue)
    q.cfg = make_cfg()
    q.sheet = FakeSheet(rows)
    q._col = {name: i + 1 for i, name in enumerate(COLUMNS)}
    q._job_cache = {}
    if now_ts is not None:
        q._now_ts = lambda: now_ts
    return q


def test_next_job_returns_oldest_pending():
    rows = [HDR,
            ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"],
            ["JOB-2", "PENDING", "u2", "42", "50", "", "", "", "", "0", "0"],
            ["JOB-3", "DONE", "u3", "42", "300", "301", "w", "9", "", "0", "0"]]
    q = q_with(rows)
    job = q.next_job()
    assert isinstance(job, Job)
    assert job.job_id == "JOB-2"  # created_at 50 < 100 — cũ nhất
    assert job.status == "PENDING"
    assert job.chat_id == 42


def test_next_job_retry_priority_over_pending():
    rows = [HDR,
            ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"],
            ["JOB-2", "RETRY", "u2", "42", "200", "", "", "", "", "1", "0"]]
    q = q_with(rows)
    job = q.next_job()
    assert job.job_id == "JOB-2"


def test_next_job_none_when_queue_empty():
    q = q_with([HDR])
    assert q.next_job() is None


def test_claim_success_writes_and_wins():
    rows = [HDR, ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows, now_ts=500)
    assert q.claim("JOB-1", "worker-a") is True
    row = q.sheet.rows[1]
    assert row[1] == "CLAIMED"      # status
    assert row[6] == "worker-a"     # worker
    assert row[5] == "500"          # updated_at = epoch


def test_claim_loses_when_rival_writes_last():
    rows = [HDR, ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows, now_ts=500)
    orig = q.sheet.update_cell

    def racing_update(r, c, value):
        orig(r, c, value)
        if c == q._col["worker"]:
            orig(r, c, "worker-b")  # worker khác ghi đè sau

    q.sheet.update_cell = racing_update
    assert q.claim("JOB-1", "worker-a") is False
    assert q.sheet.rows[1][6] == "worker-b"


def test_set_status_and_record_message_id():
    rows = [HDR, ["JOB-1", "UPLOADING", "u1", "42", "100", "101", "w", "", "", "0", "0"]]
    q = q_with(rows, now_ts=600)
    q.set_status("JOB-1", "DONE")
    q.record_message_id("JOB-1", 555)
    row = q.sheet.rows[1]
    assert row[1] == "DONE"
    assert row[7] == "555"          # msg_id
    assert row[5] == "600"          # updated_at
    assert row[8] == ""             # error không đổi khi không truyền


def test_set_status_rejects_unknown_status():
    rows = [HDR, ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows)
    try:
        q.set_status("JOB-1", "BOGUS")
        assert False, "phải raise"
    except ValueError:
        pass


def test_sweep_stale_retries_then_fails_and_reports():
    # now = 2000; cutoff = 2000 - 15*60 = 1100
    rows = [HDR,
            ["JOB-OLD", "PENDING", "u1", "42", "100", "115", "w", "", "", "0", "0"],
            ["JOB-NEW", "PENDING", "u2", "42", "9999", "", "", "", "", "0", "0"],
            ["JOB-MAX", "CLAIMED", "u3", "42", "100", "115", "w", "", "", "3", "0"],
            ["JOB-DL", "DOWNLOADING", "u4", "42", "100", "100", "w", "", "", "1", "0"]]
    q = q_with(rows, now_ts=2000)
    failed = q.sweep_stale(older_than_min=15, max_retries=3)
    # JOB-OLD: stale, retries 0 < 3 → RETRY; JOB-NEW: mới → giữ nguyên
    # JOB-MAX: stale, retries 3 ≥ 3 → FAILED; JOB-DL: stale (updated_at 100) → RETRY (retries 1+1=2)
    assert q.sheet.rows[1][1] == "RETRY"
    assert q.sheet.rows[1][9] == "1"
    assert q.sheet.rows[2][1] == "PENDING"
    assert q.sheet.rows[3][1] == "FAILED"
    assert q.sheet.rows[4][1] == "RETRY"
    assert q.sheet.rows[4][9] == "2"
    assert failed == ["JOB-MAX"]


def test_sweep_ignores_terminal_states():
    rows = [HDR,
            ["JOB-D", "DONE", "u1", "42", "1", "1", "w", "9", "", "0", "0"],
            ["JOB-F", "FAILED", "u2", "42", "1", "1", "w", "", "x", "3", "0"]]
    q = q_with(rows, now_ts=99999)
    assert q.sweep_stale() == []


def test_sweep_uses_created_at_when_updated_at_empty():
    rows = [HDR, ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows, now_ts=100 + 16 * 60)
    failed = q.sweep_stale(older_than_min=15)
    assert q.sheet.rows[1][1] == "RETRY"
    assert failed == []


def test_append_job_creates_pending_row():
    q = q_with([HDR], now_ts=1234)
    job_id = q.append_job("https://x/v.mp4", 42, job_id="JOB-X")
    assert job_id == "JOB-X"
    row = q.sheet.rows[1]
    assert row[0] == "JOB-X"
    assert row[1] == "PENDING"
    assert row[2] == "https://x/v.mp4"
    assert row[3] == "42"
    assert row[4] == "1234"
    assert row[10] == "0"


def test_checkpoint_column_appended():
    from worker.sheet_queue import COLUMNS
    assert COLUMNS[-1] == "checkpoint"
    assert len(COLUMNS) == 11


def test_checkpoint_parse_roundtrip():
    rows = [HDR,
            ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows)
    q.set_checkpoint("JOB-1", "dl:268435456")
    row = q.sheet.rows[1]
    assert row[10] == "dl:268435456"
    from worker.sheet_queue import checkpoint_dl_bytes, Job
    job = q._parse_row(2, row)
    assert isinstance(job, Job)
    assert checkpoint_dl_bytes(job) == 268435456


def test_checkpoint_invalid_returns_zero():
    from worker.sheet_queue import checkpoint_dl_bytes
    job = Job("J", "PENDING", "u", 42, 1, None, "", None, "", 0, "rubbish", 2)
    assert checkpoint_dl_bytes(job) == 0
    job2 = Job("J", "PENDING", "u", 42, 1, None, "", None, "", 0, "0", 2)
    assert checkpoint_dl_bytes(job2) == 0
