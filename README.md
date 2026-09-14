# MP4 → Telegram Private Channel Pipeline

Hệ thống $0, không VPS: bot Telegram nhận request → GAS resolve + queue
(Google Sheet) → GitHub Actions worker tải MP4 và upload vào kênh riêng tư
qua Telethon (MTProto, 2GB/file).

## Kiến trúc

    Bạn ──/dl <link X>──▶ Bot Telegram ──▶ GAS doPost (webhook)
                                            │ resolve URL (logic sẵn có)
                                            │ INSERT job PENDING (Sheet)
                                            │ POST repository_dispatch (PAT)
                                            ▼
                              GitHub Actions worker (public repo, $0)
                                            │ claim job (verify-after-write)
                                            │ streaming download + ffprobe
                                            │ Telethon send_file → kênh riêng tư
                                            │ Sheet: DONE + msg_id
                                            ▼
                                   Bot thông báo ✅/❌
    Cron 2h/lần (Actions job sweep): job treo → RETRY ×3 → FAILED + ❌

## Thành phần

| File | Vai trò |
| --- | --- |
| `worker/main.py` | Orchestrator: claim → download → upload → DONE; `--sweep` dọn job treo |
| `worker/sheet_queue.py` | Google Sheet làm queue: verify-after-write claim, retry |
| `worker/downloader.py` | Streaming download, cap 2GB, ffprobe validate |
| `worker/uploader.py` | Telethon MTProto upload, StringSession |
| `worker/notifier.py` | Bot API sendMessage kết quả |
| `.github/workflows/worker.yml` | Trigger repository_dispatch + cron sweep |
| `gas/telegram-pipeline.gs` | Copy vào GAS hiện tại: tạo job + dispatch |
| `tools/make_session.py` | Tạo StringSession local 1 lần |

## Deploy

Xem `runbook.md` — toàn bộ bước setup secrets, Sheet, service account, GAS,
và checklist test tích hợp (100MB → 1.5GB → E2E → recovery).

## Chi tiết thiết kế

- Spec: `docs/superpowers/specs/2026-09-14-telegram-mp4-pipeline-design.md`
- Plan: `docs/superpowers/plans/2026-09-14-telegram-mp4-pipeline.md`
