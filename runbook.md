# Runbook — Pipeline MP4 → Kênh Telegram riêng tư

> **Quy tắc bắt buộc khi làm việc với Python trên máy local:** KHÔNG pip install
> thẳng vào system Python. Luôn tạo môi trường ảo trước:
>
> ```bash
> python -m venv .venv
> source .venv/bin/activate
> pip install telethon   # hoặc: pip install -r requirements.txt
> ```
>
Mọi lệnh `pip install` trong runbook này hiểu là chạy trong `.venv` đã
activate. Thoát môi trường: `deactivate`.

## 0. Cài đặt local (venv + Colab pins)

> **Lưu ý**: `.venv` cũ có thể đang chứa `jupyter-kernel-client 1.0.2` (lệch với CI).
> Nếu lần đầu chạy, xoá `.venv` cũ và tạo lại để khớp chính xác môi trường CI.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-colab.txt
.venv/bin/python tools/check_colab_env.py   # phải in "OK: google-colab-cli==0.6.0, jupyter-kernel-client==0.15.0"
```

Version cặp Colab CLI + `jupyter-kernel-client` **chỉ khai ở `requirements-colab.txt`**.
CI và local đều dùng chung file này; không ghim inline nữa.

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

1. Local (trong `.venv` — quy tắc đầu file): `pip install telethon && python tools/make_session.py`
2. Nhập api_id/api_hash, số điện thoại, mã code Telegram gửi.
3. Copy chuỗi in ra → Actions secret `TELETHON_SESSION`.
   (Session bị thu hồi/thao tác đổi mật khẩu → chạy lại bước này + update secret.)


## 3a. Giữ session Telegram ổn định (giảm rủi ro bị thu hồi)

**Không chặn 100%** — Telegram luôn có quyền revoke session (cơ chế bảo mật).
Worker đã cấu hình sẵn các điểm có kiểm soát:

- **api_id/api_hash riêng** (§2): không dùng api public chung — Telegram đánh
  giá theo app id; api riêng giảm rủi ro flag.
- **Fingerprint client**: worker tạo client với tham số giả lập Telegram
  Desktop chính thức (`device_model="Desktop"`,
  `system_version="Windows 11 x64"`, `app_version="7.2.8"`,
  `lang_code="en"`) — tránh fingerprint Telethon mặc định
  ("PC 64bit"/kernel-release/"1.45.0") dễ bị nhận diện client không chính thống.
  Đã pin trong test (`test_uploader.py`).
- **Session chạy đúng 1 nơi**: concurrency group `video-worker` đảm bảo không
  bao giờ 2 client cùng session đồng thời. KHÔNG chạy worker local song song
  với Actions (`AuthKeyDuplicatedError`).
- **Disconnect đúng cách**: worker luôn `await client.disconnect()` trong
  `finally` (kể cả khi lỗi).
- **Không làm trên app Telegram khi session đang sống**: đổi mật khẩu /
  bật-tắt 2FA / "Kết thúc phiên" trong Cài đặt → Thiết bị — mọi thao tác này
  thu hồi session (bắt buộc re-mint `make_session.py` + update secret).

Session bị revoke bất cứ lúc nào (không có cờ "không bao giờ thu hồi") —
quy trình phục hồi: chạy lại `tools/make_session.py` → update secret
`TELETHON_SESSION`.

## 3b. Pre-mint COLAB_TOKEN_JSON (Colab CLI OAuth2)

> Mục này thực hiện **1 lần trên máy local** để tạo refresh token dùng headless
> trong Actions. Token được lưu tại `~/.config/colab-cli/token.json` trên runner
> khi controller bootstrap chạy; runner chỉ giữ ~30s rồi xoá.

1. Trên máy local:
   ```bash
   .venv/bin/pip install -r requirements.txt -r requirements-colab.txt
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
   `job_id  status  url  chat_id  created_at  updated_at  worker  msg_id  error  retry_count  checkpoint  media_key  url_alt`
3. Ghi Sheet ID (trong URL giữa `/d/` và `/edit`) → Actions secret `SHEET_ID`
   và GAS Script Property `SHEET_ID`.
   > Sheet cũ 11/12 cột vẫn chạy: worker đọc ô thiếu thành rỗng. Chỉ cần thêm
   > tiêu đề `media_key` (cột L) và `url_alt` (cột M) cho dễ đọc — logic dùng
   > vị trí cột, không đọc tên.

> **WAITING (vượt cap 10 job/ngày):** GAS vẫn ghi job vào `Jobs` với
> `status=WAITING` (có `job_id`, `url`, `url_alt`, `media_key`) nhưng KHÔNG
> dispatch. Lần dispatch/sweep đầu tiên sau 00:00 VN sẽ chuyển các dòng
> `WAITING` có `created_at` trước 00:00 hôm nay sang `PENDING`. Giới hạn
> 4 upload/ngày vẫn áp dụng, nên job có thể chờ thêm vài ngày.
>
> **Thứ tự deploy:** push/deploy `worker/` + `controller/` trước, rồi mới
> deploy GAS. GAS mới + controller cũ sẽ tạo `WAITING` nhưng không có ai
> release — job kẹt im lặng.

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

## 6b. GitHub PAT riêng cho cron-job.org (dispatch sweep)

1. GitHub → Settings → Developer settings → Fine-grained tokens → Generate.
2. Chỉ repo worker; permission **Actions: Read and write** (KHÁC §6 —
   PAT §6 là Contents:RW cho repository_dispatch; PAT §6b là Actions:RW
   cho workflow_dispatch).
3. Token KHÔNG lưu vào repo/GAS — chỉ dán vào headers của job
   `github-sweep-dispatch` trên console.cron-job.org.
4. Rotate: token hết hạn (mặc định 90 ngày) → tạo token mới → PATCH job
   trên cron-job.org (sửa header Authorization) → xong. Cron-job.org có
   email onFailure — email đến = PAT chết là khả năng số 1.

## 7. Repo + secrets + workflow

1. Push repo này lên GitHub, đặt **public** (bắt buộc để free unlimited minutes).
2. Settings → Secrets and variables → Actions → tạo 9 secrets:
   `TELETHON_SESSION`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TARGET_CHANNEL`,
   `OWNER_CHAT_ID`, `GCP_SA_JSON`, `SHEET_ID`, `BOT_TOKEN`, `COLAB_TOKEN_JSON`.
   - `OWNER_CHAT_ID`: chat_id cá nhân của bạn (nhắn tin cho @userinfobot để lấy).
   - `BOT_TOKEN`: token **bot pipeline mới** (§8 — bot riêng cho pipeline; bot
     cũ GetXBot không dùng vào pipeline).
   - `COLAB_TOKEN_JSON`: nội dung file `~/.config/colab-cli/token.json` từ §3b.
3. Mở **Variables** (cùng trang Secrets and variables → Variables) → thêm:
   `WORKER_REPO` = `owner/repo` (repo chứa pipeline này).
4. Sweep 2h/lần KHÔNG còn dùng `schedule` GitHub (bị tự tắt sau 60 ngày
   repo im tiếng). Thay bằng cron-job.org job `github-sweep-dispatch`
   gọi `workflow_dispatch` (mode=sweep) mỗi 2h lúc :07. Nếu sweep ngừng:
   - Kiểm tra console.cron-job.org → History job (status phải OK/1xx-2xx).
   - Email failure từ cron-job.org = PAT hết hạn/token sai → rotate PAT
     (Task PAT trong §6b) rồi PATCH job qua console (headers → Authorization).
   - Workflow KHÔNG bị tự tắt nữa vì không còn trigger `schedule`.
5. Run đỏ của `video-controller` đọc thế nào (từ 16-10-2026):
   - Các step khác đỏ thường là lỗi hạ tầng CI (checkout, pip, sweep); riêng 2
     step dispatch (`Xử lý job PENDING/RETRY còn lại (watchdog → Colab VM)` /
     `Dispatch job lên Colab VM`) có thể đỏ theo nghĩa mô tả ở dưới.
   - **Đỏ = dispatch hỏng thật**: worker của lượt này không claim được job
     (Sheet vẫn PENDING/RETRY, cột `worker` khác `WORKER_ID`) → job không
     chạy, cần xem log + annotation `::error::` của run.
   - **Xanh kèm `::warning::` = job lỗi ở tầng job** (Sheet FAILED, hoặc VM
     dừng giữa chừng để sweep RETRY). Chi tiết lỗi nằm ở cột `error` trên
     Sheet + tin `[FAIL]` Telegram — không phải trên GitHub.
6. Lưu ý pipeline secrets: các secret chỉ tồn tại trong môi trường runner
   ~30 giây, sau đó controller bootstrap truyền env xuống VM qua prelude
   `os.environ` trong `colab_bootstrap.py` (session ephemeral, tự teardown).
   Riêng `COLAB_TOKEN_JSON` **không** được truyền
   xuống VM; controller chỉ dùng nội dung JSON để materialize runner-local
   `~/.config/colab-cli/token.json` rồi chạy `colab run`.

## 8. Bot Telegram mới (bot pipeline riêng — bot cũ giữ nguyên làm backup)

Bot mới (bot pipeline) tách hẳn Apps Script project riêng — bot cũ (GetXBot)
không bị đụng tới, vẫn chạy làm backup. Bot mới chỉ chấp nhận tin nhắn từ bạn:

- **Lớp 1 — Secret trong URL webhook**: GAS **không đọc được request
  headers** (doPost `e.headers` không tồn tại — verify thực tế), nên secret
  truyền qua query: URL webhook là
  `<URL_EXEC>%3Ftoken%3D<WEBHOOK_SECRET>`. Ai POST giả tới URL `/exec` mà
  thiếu/không biết token → bị loại ngay.

- **Lớp 2 — Whitelist `OWNER_CHAT_ID`**: chỉ tin nhắn từ chat_id của bạn được
  xử lý; người khác nhắn → im lặng tuyệt đối, không phản hồi gì (chống spam,
  không lộ bot có sống).
- Token bot + mọi credential nằm trong Script Properties (không hardcode).
- Dedupe `update_id` (Cache 6h) — Telegram retry không tạo job đôi.

1. @BotFather → `/newbot` → tạo **bot mới** → copy `BOT_TOKEN`.
2. Tạo **Apps Script project MỚI** (script.google.com → New project) → dán
   toàn bộ `gas/bot-pipeline.gs` (file này tự chứa logic resolve getxbot +
   pipeline — không cần file cũ).
3. Project Settings → Script Properties → thêm 6 biến:
   `BOT_TOKEN`, `OWNER_CHAT_ID`, `WEBHOOK_SECRET` (chuỗi ngẫu nhiên, vd
   `openssl rand -hex 32`), `SHEET_ID`, `WORKER_REPO`, `GITHUB_PAT`.
4. Deploy → New deployment → Web app → Execute as: **Me** → Who has access:
   **Anyone** (an toàn — có secret query token chặn) → copy URL `/exec`.
5. Đăng ký webhook — secret nằm TRONG URL (GAS không đọc được headers):
   ⚠️ KHÔNG dùng `secret_token=` của setWebhook — Telegram gửi token đó qua
   header `X-Telegram-Bot-Api-Secret-Token` mà GAS không đọc được → mọi
   update bị BLOCK (`got_len=0`). Token phải nằm trong `?token=` của URL.
   ```bash
   # Nối secret vào URL rồi encode cả khối:
   python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1] + '?token=' + sys.argv[2], safe=''))" '<URL_EXEC>' '<WEBHOOK_SECRET>'
   # → chuỗi đã encode, dán vào url=
   curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<CHUỖI_đã_encode>&allowed_updates=%5B%22message%22%5D&drop_pending_updates=true"
   # phải trả {"ok":true,...}
   ```
6. Test: từ tài khoản của bạn gửi 1 link X có video vào bot mới → nhận
   `✅ Đã tạo job JOB-...`; từ tài khoản KHÁC nhắn bot → im lặng (verify whitelist).
7. Tắt tạm / rollback bot mới (bot cũ không bị ảnh hưởng):
   `curl "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook"`.

> `gas/telegram-pipeline.gs` (module chỉ có pipeline) giữ lại cho phương án B:
> muốn bot CŨ trực tiếp tạo job — dán module đó vào project bot cũ rồi gọi
> `pipelineCreateJobAndDispatch(mp4Url, mp4AltUrl, chatId, statusId)` trong `doPost` (nhưng bot cũ
> hiện KHÔNG có whitelist — ai biết bot là nhắn được, chưa khắc phục). Module này cần
> host project cung cấp `getVideoCandidates`.

## 9. Test tích hợp thật (theo thứ tự)

### a) Smoke 100MB qua local controller

> Yêu cầu local: `.venv` + `pip install -r requirements.txt -r requirements-colab.txt` (quy tắc đầu file) +
> `COLAB_TOKEN_JSON` đã mint
> (runbook §3b). Worker chạy trên Colab VM — máy local chỉ cần gspread
> (controller) + colab CLI.

```bash
# Điền .env từ template: cp .env.example .env (biến liệt kê đầy đủ kèm hướng dẫn từng biến)
# rồi nạp vào shell trước khi chạy:
set -a; source .env; set +a
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

### e) Fallback chất lượng vượt 1.9GB

1. Gửi link X có video vượt ngưỡng (bản 1080p >= 1.9GB, bản 720p dưới).
2. Kỳ vọng: Sheet có `url` = bản 1080p, `url_alt` = bản 720p; worker chọn bản 720p; job DONE.
   Probes `curl -r 0-0` không ghi log — xác minh qua `url_alt` trên Sheet,
   kết quả DONE với bản thấp hơn, hoặc lý do FAILED.
3. Cả 2 bản >= 1.9GB: FAILED sớm (0 byte tải) với «vượt giới hạn 1.90 GB:
   bản cao nhất …, bản kế …»; bot báo ❌.
4. Chỉ có 1 bản duy nhất (không có url_alt) và bản đó >= 1.9GB: FAILED sớm với «chỉ có 1 bản».
5. File đúng 1,900,000,000 byte cũng phải FAILED (strictly `<`).
6. Kiểm tra tay trên Colab:
   `curl -sS -L -r 0-0 -D - -o /dev/null --max-time 20 '<url>'`
   → `206` + `content-range: bytes 0-0/<total>`.

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
| Burst tin nhắn → run bị `cancelled`, chỉ 1 job chạy | `concurrency.queue` mặc định `single` chỉ giữ 1 run pending và huỷ run pending cũ → đã đặt `queue: max` cho phép tối đa 100 run pending xử lý FIFO |
| GitHub báo lỗi validation ở `concurrency` | `queue: max` không dùng chung được với `cancel-in-progress: true` ⇒ kiểm tra workflow có vô tình bật `cancel-in-progress: true` |
| Bootstrap `TypeError: json.loads() arg 1 must be str, bytes or bytearray, not dict` | Đã fix: bootstrap encode dict thành JSON string trước khi nhét env; KHÔNG double-encode |
| `KeyError: WORKER_REPO` | Chưa set variable `WORKER_REPO` trong Settings → Variables |
| HTTP 416 / chunk rỗng ở cuối file | File đúng bội số 256MB → EOF guard: worker tự assemble; nếu part non-empty ở EOF → ghép; nếu part rỗng → transient retry |
| Runtime Colab chết giữa chừng | VM mới đọc `checkpoint` → thấy part rỗng (`video.part` không xuyên session) → mismatch → reset `checkpoint` về `dl:0` → tải lại từ đầu. Checkpoint chống mất state trong-session và giúp chẩn đoán điểm chết; xuyên-session chỉ khi dùng Drive mount (không dùng) |
| Colab CLI wheel 0.6.0 không có `--env` | Dùng bootstrap mode (`colab run bootstrap`) — đã fix trong controller |
| Exact-multiple 256MB EOF | Worker ghi checkpoint `dl:<bytes>` sau mỗi chunk; EOF guard xử lý boundary 416 |
| Bot trả "đã upload gần đây" cho link MỚI | Ca tin nhắn đến **không** trích được `statusId` (link không có `/status/<số>`) ⇒ dedup so **URL**, nên khớp nhầm nếu getxbot từng trả URL sai/trùng (cache IP). Đã fix: khi tin nhắn đến **có** `statusId` thì dedup theo cột L `media_key` lấy từ link gốc. Nếu tái diễn: kiểm cột L đã điền khi tạo job + link gửi có kèm `/status/<số>` không |
| Video resolve "OK" nhưng bot bảo DUP link khác | Cùng nhánh so URL ở trên: khớp nhầm một hàng chưa-FAILED có cùng `url` (getxbot từng trả URL sai cho link khác). Chữa: xoá hàng bị khớp nhầm trong Sheet rồi gửi lại link |
| Job FAILED «vượt giới hạn 1.90 GB: …» | Cả 2 variant đều >= 1.9GB — không bản nào upload được. Không retry: gửi link khác hoặc chấp nhận không có video. |
| Job dùng nhầm bản thấp dù bản gốc <1.9GB | Kiểm tra cột `url`/`url_alt`; xem log `PIPELINE-RESOLVE`; sửa `getVideoCandidates` nếu getxbot đổi format. |
| curl probe trả `None` (không đo được) | Worker vẫn tải bản gốc; guard giữa luồng chặn. Kiểm tra `curl --version` trên Colab, URL hết hạn (403/404), hoặc CDN bỏ Range. |
| Telegram vẫn từ chối file <1.9GB | Hạ `TELEGRAM_MAX` trong `worker/downloader.py` (một dòng decimal bytes); chạy lại test. |

### WAITING không chạy

- Bot báo “Đã lưu job … vào hàng đợi” nhưng không thấy chạy:
  1. Kiểm tra row có `status=WAITING` và `created_at` là hôm qua/chưa.
  2. Tin nhắn mới chỉ giúp nếu hôm nay chưa đủ 10 job (lúc đó controller
     nhận PENDING mới → release + dispatch). Nếu đã đủ 10 job, tin nhắn
     mới sẽ tạo thêm WAITING thay vì giải phóng.
  3. Row WAITING tạo hôm nay chỉ được release sau 00:00 VN ngày mai.
     `mode=sweep` chỉ flip row có `created_at` trước 00:00 hôm nay; chạy
     sweep trong ngày không giải phóng được row vừa tạo.
  4. Xem log controller có `::notice::Đã giải phóng … job WAITING`.
  5. Nếu đã đủ 4 `DONE` hôm nay, job đã thành `PENDING` nhưng phải chờ mai.
- Nếu sweep ngừng: `WAITING` không tự chuyển `PENDING`; sửa cron-job.org
  trước khi chờ job chạy.

## 10b. Kiểm tra GAS

Thực hiện sau mỗi lần sửa `gas/bot-pipeline.gs` để xác minh thủ công.

1. **Deploy phiên bản mới**: mở Apps Script project → Save → Deploy → New
   deployment → cấu hình giống bản đang chạy → deploy.
2. **Kiểm tra dedup + hàng đợi**: thêm tay 1 row `DONE` với `created_at` /
   `updated_at` = hôm nay, `media_key` = 1 status id thật → gửi link ứng
   với status đó → kỳ vọng nhận “đã có trong hàng đợi/đã upload”, Sheet
   không tạo row mới.
   - **Giới hạn cần biết**: `isQueuedOrRecent` khớp theo cột L `media_key`
     **chỉ khi tin nhắn đến có `statusId`** (`/status/<số>`); chỉ khi tin
     nhắn đến KHÔNG trích được `statusId` mới so URL — bất kể row có cột L
     hay không. Hệ quả: row legacy (cột L trống) + tin nhắn đến **có**
     `statusId` ⇒ không nhánh nào khớp ⇒ **chắc chắn** tạo job trùng (không
     có fallback URL cho ca này). Cách chữa: điền cột L của row cũ = status
     id của video đó — gửi lại link không giúp gì vì cột L vẫn trống.
3. **Kiểm tra cap 10 job/ngày**: thêm tay 10 row có `created_at` = hôm nay →
   gửi 1 link mới → kỳ vọng nhận “⏳ Đã lưu job … vào hàng đợi”, Sheet có
   thêm 1 row `status=WAITING` (đủ 13 cột), và **không** có GitHub run mới.
   Chạy `mode=sweep` sau khi sửa `created_at` của row đó thành hôm qua →
   release được xác nhận qua log `::notice::Đã giải phóng … job WAITING`.
   Row có thể chỉ thoáng qua `PENDING` nếu `done_today < 4`; cùng lúc sweep
   sẽ claim/upload nên kỳ vọng thấy `CLAIMED`/`DOWNLOADING`/`DONE`. Nếu đã
   đủ 4 `DONE` hôm nay, row giữ `PENDING` đến mai. Lưu ý: test có thể
   khởi động upload thật — dùng link disposable hoặc sẵn sàng xem kết quả.
4. **Dọn dẹp**: xoá các row test sau khi kiểm tra.
5. **Ghi lại kết quả**: lưu snapshot kết quả kiểm tra vào phiên làm việc
   hoặc ghi chú nội bộ; không ghi công khai vì bảo mật.

## 10c. Chính sách anti-abuse v3

- Colab session ngắn tự nhiên, tự teardown — **KHÔNG** keep-alive, **KHÔNG** hard-limit giả lập.
- Worker log tiến độ thật, tối đa ~1 dòng/15s và chỉ khi có byte mới (không spam iopub).
- `COLAB_TOKEN_JSON` chỉ tồn tại runner-local (`~/.config/colab-cli/token.json`)
  rồi xoá; không truyền xuống VM.
- File đúng bội số 256MB → HTTP 416 → worker EOF guard tự assemble; Sheet
  `checkpoint` dùng `dl:<bytes>`: hữu ích trong-session + chẩn đoán — xuyên-session
  part file không còn → VM mới mismatch → reset `dl:0` → tải lại từ đầu.

## 10d. Chính sách ngân sách thời gian Colab

- **Mặc định đang dùng**: `COLAB_RUN_TIMEOUT_S=7200` (~2 giờ) — hạn tuyệt đối
  của `colab run --timeout` cho cả cell (jupyter_kernel_client tính deadline một
  lần; log không reset nó).
- **Tại sao không dài hơn vô hạn**: mục tiêu là job thường xuyên hoàn thành
  trong hạn; chính sách giữ session ngắn để giảm rủi ro anti-abuse.
- **Cách đổi**: chỉ đổi env `COLAB_RUN_TIMEOUT_S`; **không sửa code**.
  Chạy local: đặt biến trong `.env`/shell trước khi chạy
  `.venv/bin/python -m controller.colab_dispatch`.
  Trên GitHub Actions: thêm 1 dòng vào block `env:` của job `worker` (và `sweep`
  nếu muốn) trong `.github/workflows/worker.yml`, ví dụ `COLAB_RUN_TIMEOUT_S: "7200"`
  — workflow lấy env từ block này, không đọc `.env`.
- **Residual**: job quá lớn không xong trong hạn vẫn sẽ tốn tối đa **4 session**
  Colab rồi FAILED (1 lượt đầu + 3 retry, `max_retries=3`). Nếu gặp thường xuyên: cân nhắc giảm
  chất lượng đầu vào, cắt video nhỏ hơn, hoặc chốt nguồn URL thân thiện
  throughput thấp hơn. Nếu muốn giới hạn mạnh hơn nữa, thêm bộ đếm
  session/ngày riêng — ngoài phạm vi hiện tại.

## 11. Luật Colab & chính sách dùng

Quote ngày lấy: **2026-10-16**. Nguồn:
- Colab Terms v6 (2026-08-13): `https://colab.research.google.com/terms`
- FAQ: `https://research.google.com/colaboratory/faq.html`
- CLI README v0.6.0: `https://github.com/googlecolab/google-colab-cli`
- Google Developers Blog (2026-06-05): `https://developers.googleblog.com/introducing-the-google-colab-cli/`
- Google Colab Tools issue #4979: `https://github.com/googlecolab/colabtools/issues/4979`

### 1. Điều khoản ràng buộc

Nguồn: `https://colab.research.google.com/terms` (bản v6, sửa 2026-08-13)

> "Our FAQ contains general information about Colab, including restricted
> activities, resource limits and supported browsers. **You agree to those
> policies and usage limitations.**"

> "In addition, you may not: share access to your account or use someone else's
> account to access Colab; **access Colab other than as authorized by Google**;
> or sell, rent, or otherwise provide direct or indirect access to Colab to any
> third party."

### 2. Danh sách bị cấm theo FAQ

Nguồn: `https://research.google.com/colaboratory/faq.html`

**Cấm ở MỌI tier managed runtime:**

> "Colab managed runtimes prohibit abusive actions that negatively impact others
> and actions associated with bypassing our policies. The following are
> **disallowed from all managed Colab runtimes**: file hosting, media serving,
> or other web service offerings not related to interactive compute with Colab;
> downloading torrents or engaging in peer-to-peer file-sharing; connecting to
> remote proxies; mining cryptocurrency; running denial-of-service attacks;
> password cracking; using multiple accounts to work around access or resource
> usage restrictions; creating deepfakes; employing techniques such as
> containerization to circumvent anti-abuse policies."

**Cấm riêng ở free tier (no positive Colab compute unit balance):**

> "The following are disallowed from managed Colab runtimes running free of
> charge, without a positive Colab compute unit balance, and **may be terminated
> at any time without warning**: remote control such as SSH shells, remote
> desktops; **bypassing the notebook UI to interact primarily via a web UI**;
> chess training; running distributed computing workers."

> "Users in our free of charge tier commonly experience runtime terminations
> when attempting to bypass the notebook UI and using a web UI on a Colab managed
> runtime **for content generation**."

Quan hệ với điều khoản ở mục 1:

> "You agree to those policies and usage limitations."

### 3. Tại sao phần tự động hoá của ta KHÔNG phải lách

- **Headless automation được hỗ trợ chính chủ:** README `googlecolab/google-colab-cli`
  @v0.6.0 (`https://github.com/googlecolab/google-colab-cli`) nêu: "Provision high-performance CPU, GPU, and TPU runtimes, execute
  local code, manage remote files, and **orchestrate automated cloud pipelines**
  — directly from your terminal. Designed to support seamless developer
  productivity, **headless automation, and AI agent integrations**."
- **Teardown đúng cách trong `finally`:** `docs/05_run_command.md` @v0.6.0 (`https://github.com/googlecolab/google-colab-cli/blob/main/docs/05_run_command.md`): "**Tear down**: In a `finally` block, unless `--keep` was passed, the CLI: Sends `runtime.stop(shutdown_kernel=True)` (best-effort). Calls `state.client.unassign(endpoint)` **to free the billable VM**. Removes the session from `StateStore`. Kills the keep-alive daemon."
- **Keep-alive daemon không phải bypass của ta:** `docs/01_session_management.md`
  @v0.6.0 (`https://github.com/googlecolab/google-colab-cli/blob/main/docs/01_session_management.md`): "This matches the official `colab-vscode` extension's
  `sendKeepAlive`." `COLAB_SKILL.md` @v0.6.0: "**Always `colab stop -s <name>`
  when done** — idle VMs burn compute units."
- **Workflow agent chuẩn được Google công bố:** Google Developers Blog,
  "Introducing the Google Colab CLI" (2026-06-05, `https://developers.googleblog.com/introducing-the-google-colab-cli/`): workflow agent kết thúc bằng
  `colab stop` ("…save the notebook log, and **cleanup**").

### 4. Điểm KHÔNG thể biện minh

> "**disallowed from all managed Colab runtimes**: file hosting, media serving,
> or other web service offerings not related to interactive compute with Colab"

Workload của ta — **tải media rồi phát sang Telegram** — nằm sát nhóm
"media serving". Google không công bố ngưỡng rõ ràng ("serve bao nhiêu MB/ngày
bị coi là media serving?"), nên đây là **điểm xét đoán**, không thể biện minh
tuyệt đối. Ta chấp nhận rủi ro này bằng cách giữ session ngắn, 1 tài khoản,
không keep-alive, giới hạn throughput (4 upload/ngày, 10 job/ngày).

### 5. Hậu quả nếu bị flag + đường khiếu nại

Nguồn: `https://github.com/googlecolab/colabtools/issues/4979`

Thông báo in-product:

> "This account has been blocked from accessing Colab runtimes due to suspected
> abusive activity. **This does not impact access to other Google products.** If
> you believe this has been done in error, review the usage limitations and file
> an appeal."

**False positive có thật:** @cperry-goog (Google) cùng issue:

> "We hit an issue where our abuse detection was **picking up false positives**.
> We've unbanned all affected accounts and updated our abuse rules which should
> address this."

Cụ thể vụ ban nhầm liên quan package `ultralytics` 8.3.41: Google đã gỡ ban
cho tất cả tài khoản bị ảnh hưởng.

Khiếu nại: mở Colab → Help → Send Feedback (in-product). Kèm issue:
`https://github.com/googlecolab/colabtools/issues/4979` đã ghi nhận lỗi hệ
thống.

### 6. Ta làm / Ta KHÔNG làm

| Làm | Không làm |
| --- | --- |
| Dùng CLI chính chủ `google-colab-cli`; `colab run` để VM tự teardown trong `finally` | Không truyền `--keep`, không tự viết keep-alive/heartbeat cho Colab |
| Runtime CPU mặc định | Không GPU/TPU |
| 1 tài khoản Google duy nhất | Không SSH/remote desktop/remote proxy; không share/resell quyền truy cập |
| 4 upload/ngày: chặn ở controller TRƯỚC khi tạo session Colab (`controller/colab_dispatch.py:160-166`); GAS chỉ dispatch tối đa 10 job mới/ngày, phần vượt cap lưu `WAITING` (không dispatch) và được thả dần sau 00:00 VN (`gas/bot-pipeline.gs:152-166`, `worker/sheet_queue.py:110-134`) | Không containerize/lách anti-abuse; không nhiều tài khoản |
| Mỗi job 1 session ngắn; release VM ngay khi xong | Không giữ VM idle khi "no active executions" |
| Giới hạn throughput đủ thấp để tránh nhóm "media serving" | Không dùng Colab làm CDN/file host |
| Worker log tiến độ thật, tối đa ~1 dòng/15s và chỉ khi có byte mới (không spam iopub) | Không gửi heartbeat thừa; không toggle keep-alive |
| Worker đọc checkpoint `dl:<bytes>` để resume trong-session | Không dùng Drive mount để persist xuyên session |

Mốc kiểm chứng: **Toàn bộ quote lấy ngày 2026-10-16**; trang ToS có thể đổi —
nếu **>3 tháng**, đọc lại `colab.research.google.com/terms` trước khi kết luận.
