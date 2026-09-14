import pytest

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
