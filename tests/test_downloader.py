import os
from unittest.mock import MagicMock, patch

import pytest

from worker.downloader import download_to_file, validate_video, DownloadError, TELEGRAM_MAX, probe_range_support, download_chunk, assemble


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
        path, size = download_chunk("https://x/v.mp4", start=1024, dest_dir=str(tmp_path), chunk_size=20)
    assert size == 20
    assert path.endswith("video.part")
    assert (tmp_path / "video.part").read_bytes() == b"a" * 10 + b"b" * 10
    headers = g.call_args[1]["headers"]
    assert headers["Range"] == "bytes=1024-1043"


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


def test_assemble_renames_part(tmp_path):
    (tmp_path / "video.part").write_bytes(b"xyz")
    out = assemble(str(tmp_path))
    assert out.endswith("video.mp4") and (tmp_path / "video.mp4").exists()
