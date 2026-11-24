import pytest

from pathlib import Path

from worker.config import load_config, Config, SWEEP_REQUIRED


def _set_env(monkeypatch, **over):
    env = {
        "TELETHON_SESSION": "1ApWapzMBX0mX0",
        "TELEGRAM_API_ID": "123456",
        "TELEGRAM_API_HASH": "deadbeefcafe",
        "TARGET_CHANNEL": "-1001234567890",
        "OWNER_CHAT_ID": "987654321",
        "GCP_SA_JSON": '{"type": "service_account"}',
        "SHEET_ID": "abc123sheetid",
        "BOT_TOKEN": "111:AAA",
    }
    env.update(over)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SHEET_NAME", raising=False)
    monkeypatch.delenv("DL_DIR", raising=False)


def test_load_config_ok(monkeypatch):
    _set_env(monkeypatch)
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.api_id == 123456
    assert cfg.api_hash == "deadbeefcafe"
    assert cfg.target_channel == -1001234567890
    assert cfg.owner_chat_id == 987654321
    assert cfg.sheet_id == "abc123sheetid"
    assert cfg.bot_token == "111:AAA"
    assert cfg.sa_json == '{"type": "service_account"}'
    assert cfg.telethon_session == "1ApWapzMBX0mX0"
    assert cfg.sheet_name == "Jobs"
    assert cfg.dl_dir == "/tmp"


def test_load_config_missing_var_fails_with_name(monkeypatch):
    _set_env(monkeypatch)
    monkeypatch.delenv("TELETHON_SESSION")
    with pytest.raises(ValueError, match="TELETHON_SESSION"):
        load_config()


def test_target_channel_duong_bi_chan_truoc_khi_thue_vm(monkeypatch):
    """id dương/0 → Telethon map PeerUser → send_file chết SAU khi tải xong video.

    Phải chặn ngay ở load_config để không đốt một session Colab.
    """
    for bad in ("1234567890", "0"):
        _set_env(monkeypatch, TARGET_CHANNEL=bad)
        with pytest.raises(ValueError, match="TARGET_CHANNEL"):
            load_config()


def test_target_channel_am_hop_le(monkeypatch):
    """Kênh (-100…) và nhóm cũ (-123…) đều phải qua."""
    for good in ("-1001234567890", "-123456789"):
        _set_env(monkeypatch, TARGET_CHANNEL=good)
        assert load_config().target_channel == int(good)


def test_sweep_khong_crash_khi_target_channel_duong(monkeypatch):
    """Job `sweep` mang TARGET_CHANNEL trong env nhưng không dùng nó.

    Validation dấu là chuyện của worker; nếu áp cho cả sweep thì `python -m
    worker.main --sweep` crash mỗi lượt và bước dispatch (`if: always()`) vẫn
    thuê VM ⇒ đốt session.
    """
    _set_env(monkeypatch, TARGET_CHANNEL="1234567890")
    cfg = load_config(required=SWEEP_REQUIRED)
    assert cfg.sheet_id == "abc123sheetid"
    assert cfg.target_channel == 1234567890   # thô, không kiểm dấu


def test_worker_van_chan_target_channel_0(monkeypatch):
    """0 cũng map PeerUser ⇒ phải chặn khi TARGET_CHANNEL là biến bắt buộc."""
    _set_env(monkeypatch, TARGET_CHANNEL="0")
    with pytest.raises(ValueError, match="TARGET_CHANNEL"):
        load_config()


def test_load_config_invalid_int_fails(monkeypatch):
    _set_env(monkeypatch, TELEGRAM_API_ID="khong-phai-so")
    with pytest.raises(ValueError, match="số nguyên"):
        load_config()


def test_load_config_invalid_sa_json_fails(monkeypatch):
    _set_env(monkeypatch, GCP_SA_JSON="not-json")
    with pytest.raises(ValueError, match="GCP_SA_JSON"):
        load_config()


def test_load_config_subset_for_sweep(monkeypatch):
    # sweep không cần TELETHON_*; chỉ subset bắt buộc
    _set_env(monkeypatch)
    for k in ("TELETHON_SESSION", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TARGET_CHANNEL"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(required=SWEEP_REQUIRED)
    assert cfg.sheet_id == "abc123sheetid"
    assert cfg.api_id == 0
    assert cfg.telethon_session == ""


def test_requirements_cryptg_pin_matches_bootstrap():
    req = Path("requirements.txt").read_text()
    assert "cryptg==0.5.2" in req
    assert 'python_version < "3.15"' in req

def test_diagnose_required_chi_can_bon_bien(monkeypatch):
    """Tool chẩn đoán không được đòi Sheet/GCP/bot — chỉ 4 biến Telegram."""
    from worker.config import DIAGNOSE_REQUIRED, load_config
    for k in DIAGNOSE_REQUIRED:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELETHON_SESSION", "s")
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TARGET_CHANNEL", "-100123")
    # Không set GCP_SA_JSON/SHEET_ID/BOT_TOKEN/OWNER_CHAT_ID — vẫn phải qua.
    cfg = load_config(required=DIAGNOSE_REQUIRED)
    assert cfg.target_channel == -100123

