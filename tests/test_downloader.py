import os
import subprocess as sp
from unittest.mock import MagicMock, patch

import pytest
import requests

from worker.downloader import download_to_file, validate_video, DownloadError, TELEGRAM_MAX, probe_range_support, download_chunk, assemble, probe_size_curl, choose_source, OversizeError


def fake_response(status=200, chunks=(b"a" * 1024,), content_length=None, iter_raises=False):
    r = MagicMock()
    r.status_code = status
    r.headers = {"Content-Length": str(content_length)} if content_length else {}
    if iter_raises:
        def boom(_size):
            raise ConnectionError("mất kết nối giữa chừng")
        r.iter_content = boom
    else:
        r.iter_content = lambda _size: iter(chunks)
    return r


def test_download_ok(tmp_path):
    data = b"abcd" * 1000
    with patch("worker.downloader.requests.get", return_value=fake_response(chunks=[data])):
        path, size = download_to_file("https://x/v.mp4", str(tmp_path))
    assert size == len(data)
    assert os.path.getsize(path) == len(data)
    assert path.startswith(str(tmp_path))


def test_download_http_error_raises(tmp_path):
    with patch("worker.downloader.requests.get", return_value=fake_response(status=404)):
        with pytest.raises(DownloadError, match="HTTP 404"):
            download_to_file("https://x/v.mp4", str(tmp_path))


def test_download_declared_oversize_raises(tmp_path):
    with patch("worker.downloader.requests.get",
               return_value=fake_response(content_length=TELEGRAM_MAX + 1)):
        with pytest.raises(DownloadError, match="vượt giới hạn"):
            download_to_file("https://x/v.mp4", str(tmp_path))


def test_download_stream_error_raises_and_cleans_up(tmp_path):
    with patch("worker.downloader.requests.get",
               return_value=fake_response(chunks=[b"x"], iter_raises=True)):
        with pytest.raises(DownloadError, match="mất kết nối"):
            download_to_file("https://x/v.mp4", str(tmp_path))
    assert not (tmp_path / "video.mp4").exists()


def test_download_empty_file_raises(tmp_path):
    with patch("worker.downloader.requests.get", return_value=fake_response(chunks=[])):
        with pytest.raises(DownloadError, match="rỗng"):
            download_to_file("https://x/v.mp4", str(tmp_path))


def test_validate_video_ok(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\x00" * 1024)
    with patch("worker.downloader.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="video\n", stderr="")
        validate_video(str(f))
        assert "ffprobe" in run.call_args[0][0]


def test_validate_video_corrupt_raises(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"\x00" * 1024)
    with patch("worker.downloader.subprocess.run") as run:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="moov atom not found")
        with pytest.raises(DownloadError, match="ffprobe"):
            validate_video(str(f))


def test_probe_range_support_true_false():
    r_ok = MagicMock(); r_ok.status_code = 200; r_ok.headers = {"Accept-Ranges": "bytes"}
    r_no = MagicMock(); r_no.status_code = 200; r_no.headers = {}
    with patch("worker.downloader.requests.head", return_value=r_ok):
        assert probe_range_support("https://x/v.mp4") is True
    with patch("worker.downloader.requests.head", return_value=r_no):
        assert probe_range_support("https://x/v.mp4") is False


def test_download_chunk_writes_part_file(tmp_path):
    r = fake_response(chunks=[b"a" * 10, b"b" * 10])
    r.status_code = 206
    with patch("worker.downloader.requests.get", return_value=r) as g:
        path, size = download_chunk("https://x/v.mp4", start=0, dest_dir=str(tmp_path), chunk_size=20)
    assert size == 20
    assert path.endswith("video.part")
    assert (tmp_path / "video.part").read_bytes() == b"a" * 10 + b"b" * 10
    headers = g.call_args[1]["headers"]
    assert headers["Range"] == "bytes=0-19"


def test_download_chunk_appends(tmp_path):
    (tmp_path / "video.part").write_bytes(b"abc")
    r = fake_response(chunks=[b"def"])
    r.status_code = 206
    with patch("worker.downloader.requests.get", return_value=r):
        path, size = download_chunk("https://x/v.mp4", start=3, dest_dir=str(tmp_path), chunk_size=3)
    assert size == 3
    assert (tmp_path / "video.part").read_bytes() == b"abcdef"


def test_download_chunk_rejects_200_full_body(tmp_path):
    r = fake_response(chunks=[b"FULL"])
    r.status_code = 200
    with patch("worker.downloader.requests.get", return_value=r):
        with pytest.raises(DownloadError, match="206"):
            download_chunk("https://x/v.mp4", start=0, dest_dir=str(tmp_path))
    assert not (tmp_path / "video.part").exists()


def test_assemble_renames_part(tmp_path):
    (tmp_path / "video.part").write_bytes(b"xyz")
    out = assemble(str(tmp_path))
    assert out.endswith("video.mp4") and (tmp_path / "video.mp4").exists()


def test_download_chunk_stream_error_raises(tmp_path):
    r = fake_response(chunks=[b"a"], iter_raises=True)
    r.status_code = 206
    with patch("worker.downloader.requests.get", return_value=r):
        with pytest.raises(DownloadError, match="mất kết nối"):
            download_chunk("https://x/v.mp4", start=0, dest_dir=str(tmp_path))


def test_probe_range_support_non_200_is_false():
    r = MagicMock(); r.status_code = 404; r.headers = {}
    with patch("worker.downloader.requests.head", return_value=r):
        assert probe_range_support("https://x/v.mp4") is False


def test_probe_range_support_request_exception_is_false():
    with patch("worker.downloader.requests.head", side_effect=requests.RequestException("boom")):
        assert probe_range_support("https://x/v.mp4") is False


def test_download_chunk_part_checkpoint_mismatch_raises(tmp_path):
    (tmp_path / "video.part").write_bytes(b"abc")
    with patch("worker.downloader.requests.get", return_value=fake_response(chunks=[b"x"])) as g:
        with pytest.raises(DownloadError, match="checkpo"):
            download_chunk("https://x/v.mp4", start=100, dest_dir=str(tmp_path))
    g.assert_not_called()


def _curl_proc(stdout, returncode=0):
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


def test_probe_curl_206_dung_content_range():
    out = ("HTTP/2 206 \r\n"
           "content-range: bytes 0-0/157286400\r\n"
           "content-length: 1\r\n\r\n")
    with patch("worker.downloader.subprocess.run",
               return_value=_curl_proc(out)) as run:
        assert probe_size_curl("https://x/v.mp4") == 157286400
    cmd = run.call_args.args[0]
    assert "-r" in cmd and "0-0" in cmd and os.devnull in cmd


def test_probe_curl_200_dung_content_length():
    out = "HTTP/2 200 \r\ncontent-length: 157286400\r\n\r\n"
    with patch("worker.downloader.subprocess.run", return_value=_curl_proc(out)):
        assert probe_size_curl("https://x/v.mp4") == 157286400


def test_probe_curl_follow_redirect_lay_block_cuoi():
    out = ("HTTP/2 302 \r\nlocation: https://cdn/x\r\n\r\n"
           "HTTP/2 206 \r\ncontent-range: bytes 0-0/42\r\n\r\n")
    with patch("worker.downloader.subprocess.run", return_value=_curl_proc(out)):
        assert probe_size_curl("https://x/v.mp4") == 42


def test_probe_curl_content_range_star_returns_none():
    out = "HTTP/2 206 \r\ncontent-range: bytes 0-0/*\r\n\r\n"
    with patch("worker.downloader.subprocess.run", return_value=_curl_proc(out)):
        assert probe_size_curl("https://x/v.mp4") is None


def test_probe_curl_403_returns_none():
    out = "HTTP/2 403 \r\ncontent-length: 0\r\n\r\n"
    with patch("worker.downloader.subprocess.run", return_value=_curl_proc(out)):
        assert probe_size_curl("https://x/v.mp4") is None


def test_probe_curl_missing_binary_returns_none():
    with patch("worker.downloader.subprocess.run", side_effect=FileNotFoundError):
        assert probe_size_curl("https://x/v.mp4") is None


def test_probe_curl_timeout_returns_none():
    with patch("worker.downloader.subprocess.run",
               side_effect=sp.TimeoutExpired(cmd="curl", timeout=20)):
        assert probe_size_curl("https://x/v.mp4") is None


def test_probe_curl_nonzero_van_parse_header():
    out = "HTTP/2 200 \r\ncontent-length: 42\r\n\r\n"
    with patch("worker.downloader.subprocess.run",
               return_value=_curl_proc(out, returncode=63)):
        assert probe_size_curl("https://x/v.mp4") == 42


def test_choose_source_primary_fits():
    assert choose_source("a", "b", 100, probe=lambda u: {"a": 50, "b": 90}[u]) == ("a", None)


def test_choose_source_primary_oversize_alt_fits():
    assert choose_source("a", "b", 100, probe=lambda u: {"a": 200, "b": 90}[u]) == ("b", None)


def test_choose_source_both_oversize_rejects():
    url, reason = choose_source("a", "b", 1_900_000_000,
                                probe=lambda u: 2_500_000_000)
    assert url is None
    assert "vượt giới hạn" in reason
    assert "2.50 GB" in reason


def test_choose_source_dung_bang_nguong_cung_tu_choi():
    url, reason = choose_source("a", "", 1_900_000_000,
                                probe=lambda u: 1_900_000_000)
    assert url is None
    assert "chỉ có 1 bản" in reason


def test_choose_source_unknown_primary_prefers_primary():
    assert choose_source("a", "b", 100, probe=lambda u: None) == ("a", None)


def test_choose_source_primary_oversize_alt_unknown_uses_alt():
    assert choose_source("a", "b", 100, probe=lambda u: {"a": 200, "b": None}[u]) == ("b", None)


def test_choose_source_single_fits():
    assert choose_source("a", "", 100, probe=lambda u: 50) == ("a", None)


def test_choose_source_single_oversize_rejects():
    url, reason = choose_source("a", "", 100, probe=lambda u: 200)
    assert url is None
    assert "chỉ có 1 bản" in reason


def test_download_oversize_raises_oversize_error(tmp_path):
    with patch("worker.downloader.requests.get",
               return_value=fake_response(content_length=TELEGRAM_MAX + 1)):
        with pytest.raises(OversizeError):
            download_to_file("https://x/v.mp4", str(tmp_path))


def test_download_declared_equal_limit_raises_oversize_error(tmp_path):
    with patch("worker.downloader.requests.get",
               return_value=fake_response(content_length=TELEGRAM_MAX)):
        with pytest.raises(OversizeError):
            download_to_file("https://x/v.mp4", str(tmp_path))


def test_download_chunk_oversize_raises_oversize_error(tmp_path):
    r = fake_response(chunks=[b"x" * 10])
    r.status_code = 206
    with patch("worker.downloader.TELEGRAM_MAX", 5), \
         patch("worker.downloader.requests.get", return_value=r):
        with pytest.raises(OversizeError):
            download_chunk("https://x/v.mp4", start=0, dest_dir=str(tmp_path))


def test_download_chunk_equal_limit_raises_oversize_error(tmp_path):
    r = fake_response(chunks=[b"x" * 5])
    r.status_code = 206
    with patch("worker.downloader.TELEGRAM_MAX", 5), \
         patch("worker.downloader.requests.get", return_value=r):
        with pytest.raises(OversizeError):
            download_chunk("https://x/v.mp4", start=0, dest_dir=str(tmp_path))
