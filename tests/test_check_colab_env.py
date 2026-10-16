from tools.check_colab_env import check, parse_pins, main
from unittest.mock import patch

PINS = """
# comment
jupyter-kernel-client==0.15.0
google-colab-cli==0.6.0   # comment đuôi
"""


def test_parse_pins_bo_comment_va_lay_dung_cap():
    assert parse_pins(PINS) == {
        "jupyter-kernel-client": "0.15.0",
        "google-colab-cli": "0.6.0",
    }


def test_check_bao_lech_khi_thieu_package():
    installed = {"jupyter-kernel-client": "1.0.2"}
    assert check(installed, parse_pins(PINS)) == [
        "google-colab-cli: chưa cài (cần 0.6.0)",
        "jupyter-kernel-client: 1.0.2 != 0.15.0",
    ]


def test_check_rong_khi_khop_hoan_toan():
    assert check({"jupyter-kernel-client": "0.15.0", "google-colab-cli": "0.6.0"},
                 parse_pins(PINS)) == []


def test_main_in_loi_va_chua_error_annotation(capsys):
    with patch("tools.check_colab_env.installed_versions", return_value={"jupyter-kernel-client": "1.0.2"}):
        assert main() == 1
    captured = capsys.readouterr()
    assert "::error::jupyter-kernel-client: 1.0.2 != 0.15.0" in captured.out
    assert "::error::google-colab-cli: chưa cài (cần 0.6.0)" in captured.out
