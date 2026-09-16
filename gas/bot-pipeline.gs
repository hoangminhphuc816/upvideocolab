// ============================================================================
// BOT PIPELINE — bot MỚI chạy trong Apps Script project RIÊNG (không đụng bot cũ).
// Bot cũ (GetXBot) giữ nguyên làm backup; bot này chỉ làm một việc:
//   nhận link X/Twitter từ CHỦ SỞ HỞ → resolve MP4 → tạo job vào Sheet queue
//   → dispatch GitHub worker (tải video + upload lên kênh riêng tư).
//
// BẢO MẬT — 2 lớp (chỉ bạn dùng được, người lạ bị bỏ qua im lặng):
//   Lớp 1: WEBHOOK_SECRET — Telegram gửi header X-Telegram-Bot-Api-Secret-Token
//          trên MỖI update (set từ setWebhook). Ai POST giả vào URL /exec mà
//          không biết secret → bị loại ngay, không đọc nội dung.
//   Lớp 2: OWNER_CHAT_ID — chỉ tin nhắn từ chat_id của bạn được xử lý;
//          người khác nhắn bot → im lặng tuyệt đối (không phản hồi gì).
//
// SETUP (runbook §8 — phương án A):
//   1. @BotFather → /newbot → lấy BOT_TOKEN mới.
//   2. Tạo Apps Script project MỚI → dán file này.
//   3. Project Settings → Script Properties → thêm:
//        BOT_TOKEN        = token bot mới
//        OWNER_CHAT_ID    = chat_id của bạn (@userinfobot)
//        WEBHOOK_SECRET   = chuỗi ngẫu nhiên bất kỳ (vd: openssl rand -hex 32)
//        SHEET_ID         = id Google Sheet queue
//        WORKER_REPO      = owner/repo repo pipeline
//        GITHUB_PAT       = PAT (Contents: RW)
//   4. Deploy → New deployment → Web app → Execute as: Me
//      → Who has access: Anyone  (an toàn — có secret header chặn)
//   5. Đăng ký webhook (thay token + URL + secret):
//        curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<URL_EXEC>&secret_token=<WEBHOOK_SECRET>&allowed_updates=[\"message\"]"
//   6. Gửi thử 1 link X từ tài khoản của bạn → nhận "Đã tạo job ...".
//
// ROLLBACK: bot cũ không bị ảnh hưởng gì. Tắt bot mới:
//   curl "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook"
// ============================================================================

function doGet() {
  return ContentService.createTextOutput("Pipeline bot hoạt động tốt!");
}

function doPost(e) {
  var props = PropertiesService.getScriptProperties();
  try {
    // ---- Log chẩn đoán: thấy ngay rẽ nhánh nào trong Executions ----
    var diag = '';
    try {
      diag = JSON.stringify({
        hasPostData: !!(e && e.postData),
        hasHeaders: !!(e && e.headers),
        headerKeys: e && e.headers ? Object.keys(e.headers) : [],
        secretMatch: e && e.headers ? (e.headers['X-Telegram-Bot-Api-Secret-Token'] === props.getProperty('WEBHOOK_SECRET')) : false,
        chatId: (e && e.postData) ? (function () {
          try { return JSON.parse(e.postData.contents).message?.chat?.id || null; }
          catch (err) { return 'parse-fail'; }
        })() : null,
        ownerSet: !!props.getProperty('OWNER_CHAT_ID')
      });
    } catch (diagErr) { diag = 'diag-fail: ' + diagErr; }
    console.log('PIPELINE-DIAG ' + diag);
    // Tự báo cáo log về Telegram cho owner — không phụ thuộc UI Executions
    debugToOwner('DIAG ' + diag);

    // ---- Lớp 1: chỉ nhận update thật từ Telegram (secret header) ----
    var expected = props.getProperty('WEBHOOK_SECRET');
    var got = '';
    if (e && e.headers) {
      got = e.headers['X-Telegram-Bot-Api-Secret-Token'] ||
            e.headers['x-telegram-bot-api-secret-token'] || '';
    }
    if (!expected || got !== expected) {
      console.warn('PIPELINE-BLOCK secret mismatch (got_len=' + got.length + ')');
      debugToOwner('BLOCK: secret mismatch (got_len=' + got.length + ')');
      return ContentService.createTextOutput('forbidden'); // im lặng, không chi tiết
    }

    // ---- Dedupe update_id: Telegram retry khi GAS chậm → tránh tạo job đôi ----
    var update = JSON.parse(e.postData.contents);
    if (update.update_id) {
      var cache = CacheService.getScriptCache();
      if (cache.get('upd:' + update.update_id)) {
        return ContentService.createTextOutput('ok');
      }
      cache.put('upd:' + update.update_id, '1', 21600); // giữ 6h
    }

    if (!update.message || !update.message.text) {
      console.warn('PIPELINE-SKIP no message text');
      debugToOwner('SKIP: no message text');
      return ContentService.createTextOutput('ok');
    }
    var chatId = update.message.chat.id;
    var text = update.message.text.trim();

    // ---- Lớp 2: WHITELIST — chỉ chủ sở hữu được xử lý ----
    var owner = String(props.getProperty('OWNER_CHAT_ID') || '');
    if (!owner || String(chatId) !== owner) {
      console.warn('PIPELINE-BLOCK whitelist: chatId=' + chatId + ' owner=' + owner);
      debugToOwner('BLOCK: whitelist chatId=' + chatId + ' owner=' + owner);
      return ContentService.createTextOutput('ok'); // người lạ: im lặng tuyệt đối
    }

    // ---- /start hoặc câu không chứa link: hướng dẫn ngắn ----
    var match = text.match(/(https?:\/\/(?:twitter|x)\.com\/[^\s]+)/i);
    if (!match) {
      sendTelegramMessage(chatId,
          "🤖 Gửi link X (Twitter) chứa video — tôi sẽ tải và upload lên kênh.\n" +
          "Ví dụ: https://x.com/user/status/123");
      return ContentService.createTextOutput('ok');
    }

    sendTelegramMessage(chatId, "⏳ Đang phân tích link X...");

    // ---- Resolve MP4 (cùng logic getxbot như bot cũ) ----
    var mp4Url = getHighestQualityVideo(match[1]);
    console.log('PIPELINE-RESOLVE link=' + match[1] + ' → ' + (mp4Url ? 'OK' : 'FAIL'));
    if (!mp4Url) {
      sendTelegramMessage(chatId,
          "❌ Không resolve được video. Kiểm tra link (tweet có video?) rồi thử lại.");
      return ContentService.createTextOutput('ok');
    }

    // ---- Tạo job + dispatch worker ngay ----
    var jobId = pipelineCreateJobAndDispatch(mp4Url, chatId);
    console.log('PIPELINE-JOB ' + jobId + ' chat=' + chatId);
    sendTelegramMessage(chatId,
        "✅ Đã tạo job " + jobId + "\n" +
        "🎬 Video đang được tải và upload lên kênh (vài phút tuỳ dung lượng).\n" +
        "Bạn sẽ nhận thông báo ✅/❌ khi xong.");

  } catch (error) {
    // Lỗi sớm (trước whitelist) → không bắn gì ra ngoài.
    var cid = null;
    try {
      var u = JSON.parse(e.postData.contents);
      if (u && u.message && u.message.chat) cid = u.message.chat.id;
    } catch (ignored) {}
    if (cid && String(cid) === String(props.getProperty('OWNER_CHAT_ID'))) {
      sendTelegramMessage(cid, "🚨 LỖI HỆ THỐNG (bot pipeline): " + error.toString());
    }
  }
  return ContentService.createTextOutput('ok');
}

// ----------------------------------------------------------------------------
// Resolve video bitrate cao nhất từ getxbot.com/api/parse
// (giữ nguyên logic bot cũ, lược bớt tin nhắn debug trung gian)
// ----------------------------------------------------------------------------
function getHighestQualityVideo(xUrl) {
  var options = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ videoId: xUrl, p: 1 }),
    muteHttpExceptions: true
  };
  try {
    var response = UrlFetchApp.fetch('https://www.getxbot.com/api/parse', options);
    var resData = JSON.parse(response.getContentText());
    if (resData.code !== 0 || !resData.data) return null;

    var videoKey = Object.keys(resData.data)[0];
    if (!videoKey) return null;
    var videoList = resData.data[videoKey];
    if (!videoList || videoList.length === 0) return null;

    var maxBitrate = -1;
    var bestVideoUrl = '';
    for (var i = 0; i < videoList.length; i++) {
      if (videoList[i].bitrate > maxBitrate) {
        maxBitrate = videoList[i].bitrate;
        bestVideoUrl = videoList[i].url;
      }
    }
    if (!bestVideoUrl) return null;
    return bestVideoUrl.split('?tag')[0];
  } catch (e) {
    return null;
  }
}

// ----------------------------------------------------------------------------
// Pipeline: tạo job vào Sheet queue + repository_dispatch (bản sao
// gas/telegram-pipeline.gs — hợp đồng cột PHẢI khớp worker/sheet_queue.py)
// ----------------------------------------------------------------------------
function pipelineCreateJobAndDispatch(mp4Url, chatId) {
  var props = PropertiesService.getScriptProperties();
  var sheet = SpreadsheetApp.openById(props.getProperty('SHEET_ID'))
      .getSheetByName('Jobs');
  if (!sheet) {
    throw new Error('Không tìm thấy tab "Jobs" trong Sheet queue');
  }
  var now = Math.floor(Date.now() / 1000);
  var jobId = 'JOB-' + Utilities.formatDate(new Date(), 'UTC', 'yyyyMMdd-HHmmss')
      + '-' + Math.random().toString(36).slice(2, 8);
  // job_id, status, url, chat_id, created_at, updated_at, worker, msg_id, error, retry_count, checkpoint
  sheet.appendRow([jobId, 'PENDING', mp4Url, String(chatId), String(now), '', '', '', '', '0', '0']);
  dispatchToGitHub(jobId);
  return jobId;
}

function dispatchToGitHub(jobId) {
  var props = PropertiesService.getScriptProperties();
  var pat = props.getProperty('GITHUB_PAT');
  var repo = props.getProperty('WORKER_REPO');
  if (!pat || !repo) {
    console.warn('Thiếu GITHUB_PAT hoặc WORKER_REPO trong Script Properties');
    return -1;
  }
  try {
    var resp = UrlFetchApp.fetch('https://api.github.com/repos/' + repo + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      headers: {
        'Authorization': 'token ' + pat,
        'Accept': 'application/vnd.github+json'
      },
      payload: JSON.stringify({
        event_type: 'video-job',
        client_payload: { job_id: jobId }
      }),
      muteHttpExceptions: true
    });
  } catch (err) {
    console.warn('repository_dispatch lỗi transport: ' + err);
    return -1;
  }
  if (resp.getResponseCode() !== 204) {
    console.warn('repository_dispatch != 204: ' + resp.getResponseCode()
        + ' ' + resp.getContentText());
  }
  return resp.getResponseCode();
}

// ----------------------------------------------------------------------------
// Gửi tin nhắn qua BOT MỚI — token nằm trong Script Properties
// (bot cũ hardcode token trong code — không lặp lại cách đó ở đây)
// ----------------------------------------------------------------------------
function sendTelegramMessage(chatId, text) {
  var token = PropertiesService.getScriptProperties().getProperty('BOT_TOKEN');
  if (!token) return;
  try {
    UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ chat_id: chatId, text: text }),
      muteHttpExceptions: true
    });
  } catch (e) {
    // Không gửi được thì thôi — không có kênh nào khác để báo
  }
}

/**
 * Gửi log chẩn đoán thẳng tới OWNER qua Telegram (bypass whitelist — chỉ chạy
 * khi OWNER_CHAT_ID đã set). Mục đích: debug không cần mở UI Executions.
 * Tắt bằng cách set Script Property DEBUG = "off".
 */
function debugToOwner(text) {
  var props = PropertiesService.getScriptProperties();
  if (String(props.getProperty('DEBUG') || 'on').toLowerCase() === 'off') return;
  var owner = props.getProperty('OWNER_CHAT_ID');
  if (!owner) return;
  try {
    UrlFetchApp.fetch('https://api.telegram.org/bot' + props.getProperty('BOT_TOKEN') + '/sendMessage', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ chat_id: owner, text: '🔧 ' + String(text).slice(0, 3800) }),
      muteHttpExceptions: true
    });
  } catch (e) {
    // im lặng
  }
}