"""Test SheetQueue logic với FakeSheet — KHÔNG chạm gspread/Sheet thật.

SheetQueue.__init__ dựng gspread client thật, nên các test dựng object qua
__new__ và gán thủ công các attribute cần thiết (cfg, sheet, _col, _job_cache).
"""
from unittest.mock import MagicMock

from worker.config import Config
from worker.sheet_queue import SheetQueue, Job, COLUMNS

# COLUMNS (đúng thứ tự): job_id, status, url, chat_id, created_at, updated_at,
#                        worker, msg_id, error, retry_count, checkpoint,
#                        media_key, url_alt
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
            ["JOB-OLD", "CLAIMED", "u1", "42", "100", "115", "w", "", "", "0", "0"],
            ["JOB-NEW", "PENDING", "u2", "42", "9999", "", "", "", "", "0", "0"],
            ["JOB-MAX", "CLAIMED", "u3", "42", "100", "115", "w", "", "", "3", "0"],
            ["JOB-DL", "DOWNLOADING", "u4", "42", "100", "100", "w", "", "", "1", "0"],
            ["JOB-OLD-PENDING", "PENDING", "u5", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows, now_ts=2000)
    failed = q.sweep_stale(older_than_min=15, max_retries=3)
    # JOB-OLD: stale CLAIMED, retries 0 < 3 → RETRY; JOB-NEW: PENDING → giữ nguyên
    # JOB-MAX: stale CLAIMED, retries 3 ≥ 3 → FAILED; JOB-DL: stale DOWNLOADING → RETRY (retries 1+1=2)
    # JOB-OLD-PENDING: PENDING cũ → KHÔNG bị sweep
    assert q.sheet.rows[1][1] == "RETRY"
    assert q.sheet.rows[1][9] == "1"
    assert q.sheet.rows[2][1] == "PENDING"
    assert q.sheet.rows[3][1] == "FAILED"
    assert q.sheet.rows[4][1] == "RETRY"
    assert q.sheet.rows[4][9] == "2"
    assert q.sheet.rows[5][1] == "PENDING"
    assert q.sheet.rows[5][9] == "0"
    assert failed == ["JOB-MAX"]


def test_sweep_ignores_terminal_states():
    rows = [HDR,
            ["JOB-D", "DONE", "u1", "42", "1", "1", "w", "9", "", "0", "0"],
            ["JOB-F", "FAILED", "u2", "42", "1", "1", "w", "", "x", "3", "0"]]
    q = q_with(rows, now_ts=99999)
    assert q.sweep_stale() == []


def test_sweep_uses_created_at_when_updated_at_empty():
    rows = [HDR, ["JOB-1", "CLAIMED", "u1", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows, now_ts=100 + 16 * 60)
    failed = q.sweep_stale(older_than_min=15)
    assert q.sheet.rows[1][1] == "RETRY"
    assert failed == []


def test_touch_lam_moi_updated_at_va_khong_doi_status():
    rows = [HDR, ["JOB-1", "DOWNLOADING", "u1", "42", "100", "100", "w", "", "", "0", "0"]]
    q = q_with(rows, now_ts=1000)
    q.touch("JOB-1")
    row = q.sheet.rows[1]
    assert row[1] == "DOWNLOADING"
    assert row[5] == "1000"
    assert row[9] == "0"


def test_sweep_khong_cuop_job_vua_duoc_touch():
    now = 2000000
    rows = [HDR, ["JOB-1", "DOWNLOADING", "u1", "42", "100", str(now), "w", "", "", "0", "0"]]
    q = q_with(rows, now_ts=now)
    assert q.sweep_stale(older_than_min=15) == []
    assert q.sheet.rows[1][1] == "DOWNLOADING"


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


def test_columns_contract_13_va_thu_tu_media_key_url_alt():
    assert len(COLUMNS) == 13
    assert COLUMNS[10] == "checkpoint"
    assert COLUMNS[11] == "media_key"
    assert COLUMNS[12] == "url_alt"


def test_append_job_ghi_du_13_o():
    q = q_with([HDR], now_ts=1234)
    q.append_job("https://x/v.mp4", 42, job_id="JOB-X", url_alt="https://x/lo.mp4")
    row = q.sheet.rows[1]
    assert len(row) == 13
    assert row[11] == ""                       # media_key
    assert row[12] == "https://x/lo.mp4"       # url_alt


def test_parse_row_doc_url_alt_va_tuong_thich_dong_cu():
    rows = [HDR,
            ["JOB-1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0",
             "1882872996031066248", "https://x/lo.mp4"]]
    q = q_with(rows)
    assert q._parse_row(2, rows[1]).url_alt == "https://x/lo.mp4"

    legacy = ["JOB-2", "PENDING", "u2", "42", "100", "", "", "", "", "0", "0"]
    assert q._parse_row(3, legacy).url_alt == ""


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


def test_get_job_returns_fresh_values_after_mutation():
    rows = [HDR, ["JOB-1", "PENDING", "u", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows)
    first = q.get_job("JOB-1")
    assert first.status == "PENDING"
    assert first.worker == ""
    q.sheet.rows[1][1] = "DOWNLOADING"
    q.sheet.rows[1][6] = "worker-a"
    q.sheet.rows[1][8] = "timeout"
    second = q.get_job("JOB-1")
    assert second.status == "DOWNLOADING"
    assert second.worker == "worker-a"
    assert second.error == "timeout"


def test_count_done_since_chi_dem_done_trong_khoang():
    rows = [HDR,
            ["J1", "DONE", "u1", "42", "100", "200", "w", "9", "", "0", "0"],
            ["J2", "DONE", "u2", "42", "100", "90", "w", "9", "", "0", "0"],
            ["J3", "FAILED", "u3", "42", "100", "200", "w", "", "x", "3", "0"],
            ["J4", "DONE", "u4", "42", "100", "", "w", "9", "", "0", "0"]]
    q = q_with(rows, now_ts=300)
    # chỉ J1: DONE + updated_at 200 >= 150. J4 rơi về created_at 100 < 150.
    assert q.count_done_since(150) == 1


def test_pending_khong_bi_sweep_thanh_retry():
    rows = [HDR, ["J1", "PENDING", "u1", "42", "100", "", "", "", "", "0", "0"]]
    q = q_with(rows, now_ts=10000)
    assert q.sweep_stale(older_than_min=15) == []
    assert q.sheet.rows[1][1] == "PENDING"      # KHÔNG bị đổi thành RETRY
    assert q.sheet.rows[1][9] == "0"            # KHÔNG bị đốt retry_count


def test_in_flight_cu_van_bi_sweep():
    rows = [HDR, ["J1", "DOWNLOADING", "u1", "42", "100", "100", "w", "", "", "0", "0"]]
    q = q_with(rows, now_ts=10000)
    assert q.sweep_stale(older_than_min=15) == []
    assert q.sheet.rows[1][1] == "RETRY"


def test_checkpoint_invalid_returns_zero():
    from worker.sheet_queue import checkpoint_dl_bytes
    job = Job("J", "PENDING", "u", 42, 1, None, "", None, "", 0, "rubbish", 2)
    assert checkpoint_dl_bytes(job) == 0
    job2 = Job("J", "PENDING", "u", 42, 1, None, "", None, "", 0, "0", 2)
    assert checkpoint_dl_bytes(job2) == 0
