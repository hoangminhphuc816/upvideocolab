from tools.check_colab_env import check, parse_pins

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
