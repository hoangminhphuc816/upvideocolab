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

## 3b. Pre-mint COLAB_TOKEN_JSON (Colab CLI OAuth2)

> Mục này thực hiện **1 lần trên máy local** để tạo refresh token dùng headless
> trong Actions. Token được lưu tại `~/.config/colab-cli/token.json` trên runner
> khi controller bootstrap chạy; runner chỉ giữ ~30s rồi xoá.

1. Trên máy local:
   ```bash
   pip install google-colab-cli
   colab version
   colab whoami
   ```
2. `colab whoami` hiện URL + mã → copy URL vào browser → đăng nhập Google →
   copy mã paste vào terminal → xác nhận.
3. Sau khi xác nhận, CLI ghi token vào:
   ```
   ~/.config/colab-cli/token.json
   ```
   Mở file, copy **toàn bộ nội dung JSON**.
4. GitHub repo → Settings → Secrets and variables → Actions → Secrets →
   tạo `COLAB_TOKEN_JSON` → paste nội dung file trên.
5. **Rotate**: xoá `~/.config/colab-cli/token.json` + xoá secret cũ → lặp lại
   bước 1–4. Free tier đủ; không cần Google Cloud billing.

## 4. Google Sheet queue

1. Tạo Google Sheet mới, đặt tên tab là `Jobs` (chính xác).
2. Dòng 1 paste header đúng thứ tự:
   `job_id  status  url  chat_id  created_at  updated_at  worker  msg_id  error  retry_count  checkpoint`
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
2. Settings → Secrets and variables → Actions → tạo 9 secrets:
   `TELETHON_SESSION`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TARGET_CHANNEL`,
   `OWNER_CHAT_ID`, `GCP_SA_JSON`, `SHEET_ID`, `BOT_TOKEN`, `COLAB_TOKEN_JSON`.
   - `OWNER_CHAT_ID`: chat_id cá nhân của bạn (nhắn tin cho @userinfobot để lấy).
   - `BOT_TOKEN`: token bot Telegram hiện có (cùng bot đang nhận request X).
   - `COLAB_TOKEN_JSON`: nội dung file `~/.config/colab-cli/token.json` từ §3b.
3. Mở **Variables** (cùng trang Secrets and variables → Variables) → thêm:
   `WORKER_REPO` = `owner/repo` (repo chứa pipeline này).
4. Lưu ý: workflow `schedule` tự tắt nếu repo không hoạt động 60 ngày —
   thỉnh thoảng push commit hoặc chấp nhận bật lại tay.
5. Lưu ý pipeline secrets: các secret chỉ tồn tại trong môi trường runner
   ~30 giây, sau đó controller bootstrap truyền env xuống VM qua prelude
   `os.environ` trong `colab_bootstrap.py` (session ephemeral, tự teardown).
   Riêng `COLAB_TOKEN_JSON` **không** được truyền
   xuống VM; controller chỉ dùng nội dung JSON để materialize runner-local
   `~/.config/colab-cli/token.json` rồi chạy `colab run`.

## 8. Gắn GAS vào flow hiện tại

1. Mở project Apps Script hiện tại → New file → dán toàn bộ
   `gas/telegram-pipeline.gs`.
2. Project Settings → Script Properties → thêm: `SHEET_ID`, `WORKER_REPO`
   (`owner/repo`), `GITHUB_PAT`.
3. Trong `doPost`, sau khi resolve ra MP4 URL, thêm:
   `pipelineCreateJobAndDispatch(mp4Url, chatId);`
4. Deploy → Manage deployments → Edit → New version → Deploy (URL /exec giữ nguyên).

## 9. Test tích hợp thật (theo thứ tự)

### a) Smoke 100MB qua local controller

> Yêu cầu local: `pip install -r requirements.txt` + `google-colab-cli` đã mint
> `COLAB_TOKEN_JSON`. Worker chạy trên Colab VM — máy local chỉ cần gspread
> (controller) + `google-colab-cli`.

```bash
export TELEGRAM_API_ID=... TELEGRAM_API_HASH=... TARGET_CHANNEL=-100... \
       OWNER_CHAT_ID=... GCP_SA_JSON='...' SHEET_ID=... BOT_TOKEN=... \
       TELETHON_SESSION=... DL_DIR=/tmp COLAB_TOKEN_JSON='...' \
       WORKER_REPO='owner/repo'
# Trước đó: append 1 hàng PENDING vào Sheet với URL MP4 ~100MB công khai.
python -m controller.colab_dispatch
# Kỳ vọng: controller peek queue → `colab run` bootstrap → worker TRÊN VM tự
# claim (verify-after-write) → tải + upload
# Lần chạy đầu với session mới, worker tự warm entity cache (get_dialogs).
# Nếu TARGET_CHANNEL sai:
# - lỗi `PeerIdInvalid` → job FAILED ngay, kiểm tra lại TARGET_CHANNEL + account phải là admin kênh;
# - lỗi `ValueError: Could not find the input entity` → job bị coi tạm thời, sweep RETRY×3 rồi FAILED 'quá số lần retry' — cũng kiểm tra TARGET_CHANNEL.
```

### b) 1.5GB qua Actions

1. Append hàng PENDING với URL video 1.5GB.
2. GitHub → Actions → video-controller → Run workflow → mode `worker`.
3. Kỳ vọng: controller peek queue trên runner (~30s) → cấp Colab VM → worker
   trên VM tự claim → job hoàn thành trong ~10–30 phút; video stream được
   trong kênh (bấm play trực tiếp, không phải tải về mới xem); Sheet DONE.

### c) End-to-end đầy đủ

1. Nhắn `/dl <link bài X>` cho bot như bình thường.
2. Kỳ vọng: bot vẫn trả lời link MP4 như cũ; vài giây sau Actions run mới
   xuất hiện (`repository_dispatch`); controller cấp Colab VM; video vào
   kênh; tin ✅ tới bạn.

### d) Recovery (checkpoint chunk resume)

1. Append hàng PENDING với URL MP4 bất kỳ.
2. Khi worker đang `DOWNLOADING` ở chunk giữa file, giả lập treo:
   đặt `status = DOWNLOADING`, `checkpoint = dl:<bytes>` (ví dụ `dl:268435456`),
   `updated_at` = epoch hiện tại − 20 phút.
3. GitHub → Actions → video-controller → Run workflow → mode `sweep`.
4. Kỳ vọng: sweep đổi hàng sang `RETRY`, `retry_count` tăng; watchdog cron
   hoặc run workflow kế tiếp claim lại → controller cấp VM mới → worker đọc
   `checkpoint` → thấy part rỗng (`video.part` không xuyên session) → mismatch
   → reset `checkpoint` về `dl:0` → tải lại từ đầu. Checkpoint chống mất state
   trong-session và giúp chẩn đoán điểm chết; xuyên-session chỉ khi dùng Drive
   mount (không dùng trong pipeline này).
5. Lặp để `retry_count` đạt 3 → sweep lần nữa → `FAILED` + tin ❌.

## 10. Sự cố thường gặp

| Hiện tượng | Nguyên nhân / xử lý |
| --- | --- |
| Actions run không xuất hiện sau /dl | PAT hết hạn/sai scope; kiểm tra log GAS (console.warn) |
| `AuthKeyDuplicatedError` Telethon | 1 session dùng 2 nơi — chắc chắn không chạy worker local song song với Actions |
| `FloodWaitError` | Worker đã set flood_sleep_threshold=120; nếu vẫn chờ lâu hơn, để job treo → sweep RETRY |
| Sheet `PERMISSION_DENIED` | SA chưa được Share Editor Sheet |
| Video không stream được trong kênh | ffprobe chặn file không có video stream; codec lạ (vd HEVC/mkv) vẫn qua ffprobe nhưng kênh có thể không stream được |
| `ffprobe`/`ffmpeg` thiếu trên VM | VM Colab có sẵn `ffprobe`; validate fail → kiểm tra source file có video stream không |
| Exit code 1 (Actions đỏ) | Lỗi tạm thời — không can thiệp, sweep tự RETRY |
| Exit code 2 (Actions đỏ) | Lỗi vĩnh viễn — xem cột error trên Sheet |
| Bootstrap `TypeError: json.loads() arg 1 must be str, bytes or bytearray, not dict` | Đã fix: bootstrap encode dict thành JSON string trước khi nhét env; KHÔNG double-encode |
| `KeyError: WORKER_REPO` | Chưa set variable `WORKER_REPO` trong Settings → Variables |
| HTTP 416 / chunk rỗng ở cuối file | File đúng bội số 256MB → EOF guard: worker tự assemble; nếu part non-empty ở EOF → ghép; nếu part rỗng → transient retry |
| Runtime Colab chết giữa chừng | VM mới đọc `checkpoint` → thấy part rỗng (`video.part` không xuyên session) → mismatch → reset `checkpoint` về `dl:0` → tải lại từ đầu. Checkpoint chống mất state trong-session và giúp chẩn đoán điểm chết; xuyên-session chỉ khi dùng Drive mount (không dùng) |
| Colab CLI wheel 0.6.0 không có `--env` | Dùng bootstrap mode (`colab run bootstrap`) — đã fix trong controller |
| Exact-multiple 256MB EOF | Worker ghi checkpoint `dl:<bytes>` sau mỗi chunk; EOF guard xử lý boundary 416 |

## 10b. Chính sách anti-abuse v3

- Colab session ngắn tự nhiên, tự teardown — **KHÔNG** keep-alive, **KHÔNG** hard-limit giả lập.
- Worker log theo chunk/chặng lớn, không spam iopub.
- `COLAB_TOKEN_JSON` chỉ tồn tại runner-local (`~/.config/colab-cli/token.json`)
  rồi xoá; không truyền xuống VM.
- File đúng bội số 256MB → HTTP 416 → worker EOF guard tự assemble; Sheet
  `checkpoint` dùng `dl:<bytes>`: hữu ích trong-session + chẩn đoán — xuyên-session
  part file không còn → VM mới mismatch → reset `dl:0` → tải lại từ đầu.
