import worker.downloader as dl
from worker.downloader import _total_bytes


class _FakeResp:
    def __init__(self, chunks, status=206, headers=None):
        self._chunks = chunks
        self.status_code = status
        self.headers = headers or {}

    def iter_content(self, size):
        yield from self._chunks

    def raise_for_status(self):
        pass


def test_total_bytes_parse_content_range():
    class R:
        headers = {"Content-Range": "bytes 0-1048575/10485760"}
    assert _total_bytes(R()) == 10485760


def test_total_bytes_fallback_content_length():
    class R:
        headers = {"Content-Length": "42"}
    assert _total_bytes(R()) == 42


def test_total_bytes_invalid_returns_none():
    class R:
        headers = {"Content-Range": "bytes 0-5/abc"}
    assert _total_bytes(R()) is None


def test_on_progress_duoc_goi_moi_chunk_voi_so_byte_tang_dan(monkeypatch, tmp_path):
    resp = _FakeResp([b"a" * 10, b"b" * 20], headers={
        "Content-Range": "bytes 0-29/30",
    })
    monkeypatch.setattr(dl.requests, "get", lambda *a, **k: resp)
    calls = []
    dl.download_chunk("http://x/v.mp4", start=0, dest_dir=str(tmp_path),
                      on_progress=lambda done, total, elapsed: calls.append((done, total)))
    assert [c[0] for c in calls] == [10, 30]
    assert calls[-1][1] == 30


def test_on_progress_loi_khong_lam_hong_download(monkeypatch, tmp_path):
    resp = _FakeResp([b"x" * 5], headers={"Content-Length": "5"})
    monkeypatch.setattr(dl.requests, "get", lambda *a, **k: resp)

    def boom(*_a):
        raise RuntimeError("callback hỏng")

    part, got = dl.download_chunk("http://x/v.mp4", start=0, dest_dir=str(tmp_path),
                                  on_progress=boom)
    assert got == 5


def test_download_to_file_on_progress_called(monkeypatch, tmp_path):
    chunks = [b"a" * 10, b"b" * 20]
    resp = _FakeResp(chunks, status=200, headers={"Content-Length": "30"})
    monkeypatch.setattr(dl.requests, "get", lambda *a, **k: resp)
    calls = []
    path, got = dl.download_to_file("http://x/v.mp4", str(tmp_path),
                                    on_progress=lambda done, total, elapsed: calls.append((done, total)))
    assert [c[0] for c in calls] == [10, 30]
    assert got == 30
