# MP4 → Telegram Private Channel Pipeline

Hệ thống $0, không VPS: bot Telegram nhận request → GAS resolve + queue
(Google Sheet) → Colab VM (qua Colab CLI) tải MP4 và upload vào kênh riêng tư
qua Telethon (MTProto, 2GB/file); GitHub Actions chỉ làm controller/watchdog
(KHÔNG tải video).

## Kiến trúc

    Bạn ──/dl <link X>──▶ Bot Telegram ──▶ GAS doPost (webhook)
                                            │ resolve URL (logic sẵn có)
                                            │ INSERT job PENDING (Sheet)
                                            │ POST repository_dispatch (PAT)
                                            ▼
                              GitHub Actions controller (public repo, $0)
                                            │ claim job (verify-after-write)
                                            │ cấp Colab VM qua colab run bootstrap
                                            ▼
                                   Colab VM worker (ephemeral)
                                            │ streaming download chunk + checkpoint
                                            │ ffprobe validate
                                            │ Telethon send_file → kênh riêng tư
                                            │ Sheet: DONE + msg_id
                                            ▼
                              Controller notify → Bot thông báo ✅/❌
    Cron 2h/lần (Actions sweep): job treo → RETRY ×3 → FAILED + ❌
    Chunk checkpoint resume: session chết → VM mới đọc checkpoint → tiếp tục.

## Thành phần

| File | Vai trò |
| --- | --- |
| `worker/main.py` | Orchestrator worker: chunk download + ffprobe + upload; `--sweep` dọn job treo |
| `worker/sheet_queue.py` | Google Sheet queue: verify-after-write claim, retry, sweep, checkpoint col |
| `worker/downloader.py` | Chunked streaming download + Range probe + checkpoint hook |
| `worker/uploader.py` | Telethon MTProto upload, StringSession |
| `worker/notifier.py` | Bot API sendMessage kết quả |
| `controller/colab_dispatch.py` | Controller: claim job → materialize token → `colab run` bootstrap → exit map |
| `.github/workflows/worker.yml` | Controller workflow: repository_dispatch + cron sweep + workflow_dispatch |
| `gas/telegram-pipeline.gs` | GAS webhook: resolve URL, append PENDING, dispatch |
| `tools/make_session.py` | Tạo StringSession local 1 lần |

## Deploy

Xem `runbook.md` — setup secrets (`COLAB_TOKEN_JSON`, `WORKER_REPO`), Sheet
11 cột (`+ checkpoint`), service account, GAS, và checklist test tích hợp
(local smoke 100MB → Actions 1.5GB → E2E → chunk recovery).

## Chi tiết thiết kế

- Spec: `docs/superpowers/specs/2026-09-14-telegram-mp4-pipeline-design.md`
- Plan: `docs/superpowers/plans/2026-09-14-telegram-mp4-pipeline.md`
- Anti-abuse v3: session ngắn tự nhiên, KHÔNG keep-alive/hard-limit; checkpoint
  chunk resume trên Sheet; Colab CLI chính thức; `COLAB_TOKEN_JSON` KHÔNG xuống VM.
