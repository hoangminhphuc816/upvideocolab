"""Google Sheet làm job queue: append, claim verify-after-write, sweep stale."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import gspread

from worker.config import Config

COLUMNS = ["job_id", "status", "url", "chat_id", "created_at", "updated_at",
           "worker", "msg_id", "error", "retry_count", "checkpoint"]
STATUSES = ["PENDING", "CLAIMED", "DOWNLOADING", "UPLOADING", "DONE", "FAILED", "RETRY"]
SWEEPABLE = ("PENDING", "CLAIMED", "DOWNLOADING", "UPLOADING")


@dataclass
class Job:
    job_id: str
    status: str
    url: str
    chat_id: int
    created_at: int
    updated_at: int | None
    worker: str
    msg_id: int | None
    error: str
    retry_count: int
    checkpoint: str
    row_index: int  # 1-based, tính cả dòng header


class SheetQueue:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        client = gspread.service_account_from_dict(json.loads(cfg.sa_json))
        self.sheet = client.open_by_key(cfg.sheet_id).worksheet(cfg.sheet_name)
        self._col = {name: i + 1 for i, name in enumerate(COLUMNS)}
        self._job_cache: dict[str, int] = {}

    # --- plumbing ---
    def _now_ts(self) -> int:
        return int(time.time())

    def _row_by_job_id(self, job_id: str) -> int | None:
        if cached := self._job_cache.get(job_id):
            return cached
        for i, row in enumerate(self.sheet.get_all_values(), start=1):
            if i >= 2 and row[self._col["job_id"] - 1] == job_id:
                self._job_cache[job_id] = i
                return i
        return None

    @staticmethod
    def _to_int(s: str) -> int:
        try:
            return int(s)
        except (TypeError, ValueError):
            return 0

    # --- lifecycle ---
    def create_table_if_missing(self) -> None:
        if not self.sheet.get_all_values():
            self.sheet.insert_row(COLUMNS, 1)

    @staticmethod
    def new_job_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return f"JOB-{stamp}-{uuid.uuid4().hex[:6]}"

    def append_job(self, url: str, chat_id: int, job_id: str | None = None) -> str:
        job_id = job_id or self.new_job_id()
        row = [job_id, "PENDING", url, str(chat_id), str(self._now_ts()),
               "", "", "", "", "0", "0"]
        self.sheet.append_row(row)
        return job_id

    def next_job(self) -> Job | None:
        """RETRY ưu tiên trước PENDING; cùng mức ưu tiên thì created_at nhỏ nhất."""
        candidates: list[tuple[int, int, int, list[str]]] = []
        for i, row in enumerate(self.sheet.get_all_values(), start=1):
            if i < 2:
                continue
            status = row[self._col["status"] - 1]
            if status == "RETRY":
                priority = 0
            elif status == "PENDING":
                priority = 1
            else:
                continue
            created = self._to_int(row[self._col["created_at"] - 1])
            candidates.append((priority, created, i, row))
        if not candidates:
            return None
        _, _, i, row = min(candidates, key=lambda c: (c[0], c[1]))
        return self._parse_row(i, row)

    def _parse_row(self, i: int, row: list[str]) -> Job:
        def cell(name: str) -> str:
            return row[self._col[name] - 1]

        msg_id = self._to_int(cell("msg_id"))
        return Job(
            job_id=cell("job_id"),
            status=cell("status"),
            url=cell("url"),
            chat_id=self._to_int(cell("chat_id")),
            created_at=self._to_int(cell("created_at")),
            updated_at=self._to_int(cell("updated_at")) or None,
            worker=cell("worker"),
            msg_id=msg_id or None,
            error=cell("error"),
            retry_count=self._to_int(cell("retry_count")),
            checkpoint=cell("checkpoint") or "0",
            row_index=i,
        )

    def claim(self, job_id: str, worker_id: str) -> bool:
        """Verify-after-write: ghi CLAIMED rồi đọc lại cột worker — khớp mới thắng."""
        row = self._row_by_job_id(job_id)
        if row is None:
            return False
        self.sheet.update_cell(row, self._col["status"], "CLAIMED")
        self.sheet.update_cell(row, self._col["worker"], worker_id)
        self.sheet.update_cell(row, self._col["updated_at"], str(self._now_ts()))
        check = self.sheet.get_all_values()[row - 1][self._col["worker"] - 1]
        return check == worker_id

    def set_status(self, job_id: str, status: str, error: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"status không hợp lệ: {status}")
        row = self._row_by_job_id(job_id)
        if row is None:
            return
        self.sheet.update_cell(row, self._col["status"], status)
        self.sheet.update_cell(row, self._col["updated_at"], str(self._now_ts()))
        if error:
            self.sheet.update_cell(row, self._col["error"], str(error)[:500])

    def record_message_id(self, job_id: str, msg_id: int) -> None:
        row = self._row_by_job_id(job_id)
        if row is not None:
            self.sheet.update_cell(row, self._col["msg_id"], str(msg_id))

    def set_checkpoint(self, job_id: str, value: str) -> None:
        row = self._row_by_job_id(job_id)
        if row is not None:
            self.sheet.update_cell(row, self._col["checkpoint"], value)

    def sweep_stale(self, older_than_min: int = 15, max_retries: int = 3) -> list[str]:
        """Job non-terminal quá cũ → RETRY (tăng retry_count) hoặc FAILED.

        Trả về danh sách job_id vừa bị FAILED để caller notify.
        Tuổi tính bằng updated_at (fallback created_at) so với now.
        """
        cutoff = self._now_ts() - older_than_min * 60
        newly_failed: list[str] = []
        for i, row in enumerate(self.sheet.get_all_values(), start=1):
            if i < 2:
                continue
            status = row[self._col["status"] - 1]
            if status not in SWEEPABLE:
                continue
            job_id = row[self._col["job_id"] - 1]
            age_ref = self._to_int(row[self._col["updated_at"] - 1]) \
                or self._to_int(row[self._col["created_at"] - 1])
            if age_ref > cutoff:
                continue  # còn mới
            retries = self._to_int(row[self._col["retry_count"] - 1])
            if retries >= max_retries:
                self.set_status(job_id, "FAILED", error="quá số lần retry")
                newly_failed.append(job_id)
            else:
                self.set_status(job_id, "RETRY")
                self.sheet.update_cell(i, self._col["retry_count"], str(retries + 1))
        return newly_failed


def checkpoint_dl_bytes(job: Job) -> int:
    raw = (job.checkpoint or "").strip()
    if not raw.startswith("dl:"):
        return 0
    try:
        return max(0, int(raw[3:]))
    except ValueError:
        return 0
