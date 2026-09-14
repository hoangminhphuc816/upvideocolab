# Runbook — Pipeline MP4 → Kênh Telegram riêng tư

## 1. Kênh Telegram riêng tư + ID

1. Tạo kênh riêng tư trong app Telegram (Create channel → Private).
2. Lấy channel ID: forward 1 tin bất kỳ từ kênh sang **@userinfobot** →
   mục "Forwarded from chat" hiện `-100xxxxxxxxxx` → đó là `TARGET_CHANNEL`.
3. Tự đăng ký làm admin kênh.

## 2. Telegram API credentials

1. Đăng nhập https://my.telegram.org → *API development tools* → tạo app.
2. Ghi lại `api_id`, `api_hash` → Actions secrets `TELEGRAM_API_ID`,
   `TELEGRAM_API_HASH`.

## 3. StringSession (đăng nhập Telethon 1 lần)

1. Local: `pip install telethon && python tools/make_session.py`
2. Nhập api_id/api_hash, số điện thoại, mã code Telegram gửi.
3. Copy chuỗi in ra → Actions secret `TELETHON_SESSION`.
   (Session bị thu hồi/thao tác đổi mật khẩu → chạy lại bước này + update secret.)

## 4. Google Sheet queue

1. Tạo Google Sheet mới, đặt tên tab là `Jobs` (chính xác).
2. Dòng 1 paste header đúng thứ tự:
   `job_id  status  url  chat_id  created_at  updated_at  worker  msg_id  error  retry_count`
3. Ghi Sheet ID (trong URL giữa `/d/` và `/edit`) → Actions secret `SHEET_ID`
   và GAS Script Property `SHEET_ID`.

## 5. GCP service account (worker đọc/ghi Sheet)

1. console.cloud.google.com → tạo project mới (miễn phí).
2. *APIs & Services* → enable **Google Sheets API**.
3. *IAM & Admin → Service Accounts* → tạo SA → tạo key JSON → tải về.
4. Trong JSON lấy `client_email` → Share Sheet (nút Share) cho email đó, quyền **Editor**.
5. Toàn bộ nội dung file JSON → Actions secret `GCP_SA_JSON`.

## 6. GitHub PAT (GAS gọi repository_dispatch)

1. GitHub → Settings → Developer settings → Fine-grained tokens → Generate.
2. Chỉ chọn repo worker; permission **Contents: Read and write**
   (nếu 403/404 khi dispatch: thêm **Actions: Read and write**).
3. Token → GAS Script Property `GITHUB_PAT`.

## 7. Repo + secrets + workflow

1. Push repo này lên GitHub, đặt **public** (bắt buộc để free unlimited minutes).
2. Settings → Secrets and variables → Actions → tạo 8 secrets:
   `TELETHON_SESSION`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TARGET_CHANNEL`,
   `OWNER_CHAT_ID`, `GCP_SA_JSON`, `SHEET_ID`, `BOT_TOKEN`.
   - `OWNER_CHAT_ID`: chat_id cá nhân của bạn (nhắn tin cho @userinfobot để lấy).
   - `BOT_TOKEN`: token bot Telegram hiện có (cùng bot đang nhận request X).
3. Lưu ý: workflow `schedule` tự tắt nếu repo không hoạt động 60 ngày —
   thỉnh thoảng push commit hoặc chấp nhận bật lại tay.

## 8. Gắn GAS vào flow hiện tại

1. Mở project Apps Script hiện tại → New file → dán toàn bộ
   `gas/telegram-pipeline.gs`.
2. Project Settings → Script Properties → thêm: `SHEET_ID`, `WORKER_REPO`
   (`owner/repo`), `GITHUB_PAT`.
3. Trong `doPost`, sau khi resolve ra MP4 URL, thêm:
   `pipelineCreateJobAndDispatch(mp4Url, chatId);`
4. Deploy → Manage deployments → Edit → New version → Deploy (URL /exec giữ nguyên).

## 9. Test tích hợp thật (theo thứ tự)

### a) Smoke 100MB (chạy local, không qua Actions)

> Yêu cầu local: `pip install -r requirements.txt` + `ffmpeg` có sẵn
> (`ffprobe -version`). Thiếu `ffprobe` sẽ khiến job bị `FAILED` vĩnh viễn
> do trùng substring kiểm tra trong worker.

```bash
export TELETHON_SESSION=... TELEGRAM_API_ID=... TELEGRAM_API_HASH=... \
       TARGET_CHANNEL=-100... OWNER_CHAT_ID=... GCP_SA_JSON='...' \
       SHEET_ID=... BOT_TOKEN=... DL_DIR=/tmp
python -m worker.main
# Trước đó: tự append 1 hàng PENDING vào Sheet với URL MP4 ~100MB công khai.
# Kỳ vọng: video xuất hiện trong kênh; hàng Sheet = DONE + msg_id; tin nhắn ✅.
```

### b) 1.5GB qua Actions
1. Append hàng PENDING với URL video 1.5GB.
2. GitHub → Actions → video-worker → Run workflow → mode `worker`.
3. Kỳ vọng: job hoàn thành trong ~10–30 phút; video stream được trong kênh
   (bấm play trực tiếp, không phải tải về mới xem); Sheet DONE.

### c) End-to-end đầy đủ
1. Nhắn `/dl <link bài X>` cho bot như bình thường.
2. Kỳ vọng: bot vẫn trả lời link MP4 như cũ; vài giây sau Actions run mới
   xuất hiện (repository_dispatch); video vào kênh; tin ✅ tới bạn.

### d) Recovery
1. Đặt 1 hàng: status `DOWNLOADING`, `updated_at` = epoch hiện tại − 20 phút.
2. GitHub → Actions → Run workflow → mode `sweep`.
3. Kỳ vọng: hàng chuyển `RETRY` và `retry_count` tăng; sweep job sau đó tự
   chạy step xử lý PENDING/RETRY và worker step xử lý tiếp.
4. Lặp để `retry_count` đạt 3 → sweep lần nữa → `FAILED` + tin ❌.

## 10. Sự cố thường gặp

| Hiện tượng | Nguyên nhân / xử lý |
| --- | --- |
| Actions run không xuất hiện sau /dl | PAT hết hạn/sai scope; kiểm tra log GAS (console.warn) |
| `AuthKeyDuplicatedError` Telethon | 1 session dùng 2 nơi — chắc chắn không chạy worker local song song với Actions |
| `FloodWaitError` | Worker đã set flood_sleep_threshold=120; nếu vẫn chờ lâu hơn, để job treo → sweep RETRY |
| Sheet `PERMISSION_DENIED` | SA chưa được Share Editor Sheet |
| Video không stream được trong kênh | ffprobe chặn file không có video stream; codec lạ (vd HEVC/mkv) vẫn qua ffprobe nhưng kênh có thể không stream được |
| `ffprobe`/`ffmpeg` thiếu | Kiểm tra workflow job có step `Cài ffprobe` |
| Exit code 1 (Actions đỏ) | Lỗi tạm thời — không can thiệp, sweep tự RETRY |
| Exit code 2 (Actions đỏ) | Lỗi vĩnh viễn — xem cột error trên Sheet |
