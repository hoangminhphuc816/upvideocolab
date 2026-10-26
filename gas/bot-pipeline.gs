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
//      → Who has access: Anyone  (secret query token chặn người giả)
//   5. Đăng ký webhook — LƯU Ý: secret phải nằm TRONG URL (không dùng
//      secret_token= của Telegram vì GAS không đọc được request headers):
//        URL_EXEC_TOKEN = <URL_EXEC>%3Ftoken%3D<WEBHOOK_SECRET>
//        curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<URL_EXEC_TOKEN>&allowed_updates=%5B%22message%22%5D&drop_pending_updates=true"
//   6. Gửi thử 1 link X từ tài khoản của bạn → nhận "Đã tạo job ...".
//
// ROLLBACK: bot cũ không bị ảnh hưởng gì. Tắt bot mới:
//   curl "https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook"
// ============================================================================

var DAILY_JOB_CAP = 10;

function jobsSheet() {
  var props = PropertiesService.getScriptProperties();
  return SpreadsheetApp.openById(props.getProperty('SHEET_ID')).getSheetByName('Jobs');
}

function doGet() {
  return _ok200("Pipeline bot hoạt động tốt!");
}

function doPost(e) {
  var props = PropertiesService.getScriptProperties();
  try {
    // ---- Lớp 1: secret token truyền qua query URL webhook ----
    // Apps Script KHÔNG expose request headers cho doPost (e.headers undefined —
    // đã verify thực tế: hasHeaders=false). Dùng secret trong URL:
    //   setWebhook?url=<URL_EXEC_đã_encode>%3Ftoken%3D<WEBHOOK_SECRET>
    // Telegram giữ nguyên query khi POST → GAS đọc qua e.parameter.token.
    var expected = props.getProperty('WEBHOOK_SECRET');
    var got = (e && e.parameter && e.parameter.token) ? String(e.parameter.token) : '';
    var diag = '';
    try {
      diag = JSON.stringify({
        hasPostData: !!(e && e.postData),
        queryParams: e && e.parameter ? Object.keys(e.parameter) : [],
        secretMatch: !!(expected && got === expected),
        chatId: (e && e.postData) ? (function () {
          try { return JSON.parse(e.postData.contents).message?.chat?.id || null; }
          catch (err) { return 'parse-fail'; }
        })() : null,
        ownerSet: !!props.getProperty('OWNER_CHAT_ID')
      });
    } catch (diagErr) { diag = 'diag-fail: ' + diagErr; }
    console.log('PIPELINE-DIAG ' + diag);
    if (!expected || got !== expected) {
      console.warn('PIPELINE-BLOCK secret mismatch (got_len=' + got.length + ')');
      // Throttle: GAS trả 200 → Telegram KHÔNG retry → spam BLOCK = nhiều
      // update thật thiếu token (webhook sai URL/secret). Chỉ báo 1 lần/5p.
      try {
        var _bcache = CacheService.getScriptCache();
        if (!_bcache.get('blockwarn')) {
          _bcache.put('blockwarn', '1', 300);
          debugToOwner('BLOCK: secret mismatch (got_len=' + got.length + ') — webhook thiếu ?token= hoặc WEBHOOK_SECRET sai. Chạy getWebhookInfo để so URL.');
        }
      } catch (e2) { /* cache lỗi: bỏ qua throttle, vẫn im lặng */ }
      return _ok200('forbidden'); // im lặng, không chi tiết
    }

    // ---- Dedupe update_id: Telegram retry khi GAS chậm → tránh tạo job đôi ----
    var update = JSON.parse(e.postData.contents);
    if (update.update_id) {
      var cache = CacheService.getScriptCache();
      if (cache.get('upd:' + update.update_id)) {
        return _ok200('ok'); // retry cũ: nuốt im lặng
      }
      cache.put('upd:' + update.update_id, '1', 21600); // giữ 6h
    }

    // DIAG chỉ gửi sau dedupe — update retry không spam tin nhắn
    debugToOwner('DIAG ' + diag);

    if (!update.message || !update.message.text) {
      console.warn('PIPELINE-SKIP no message text');
      debugToOwner('SKIP: no message text');
      return _ok200('ok');
    }
    var chatId = update.message.chat.id;
    var text = update.message.text.trim();

    // ---- Lớp 2: WHITELIST — chỉ chủ sở hữu được xử lý ----
    var owner = String(props.getProperty('OWNER_CHAT_ID') || '');
    if (!owner || String(chatId) !== owner) {
      console.warn('PIPELINE-BLOCK whitelist: chatId=' + chatId + ' owner=' + owner);
      debugToOwner('BLOCK: whitelist chatId=' + chatId + ' owner=' + owner);
      return _ok200('ok'); // người lạ: im lặng tuyệt đối
    }

    // ---- /start hoặc câu không chứa link: hướng dẫn ngắn ----
    var match = text.match(/(https?:\/\/(?:twitter|x)\.com\/[^\s]+)/i);
    if (!match) {
      sendTelegramMessage(chatId,
          "🤖 Gửi link X (Twitter) chứa video — tôi sẽ tải và upload lên kênh.\n" +
          "Ví dụ: https://x.com/user/status/123");
      return _ok200('ok');
    }

    var statusId = '';
    var statusMatch = match[1].match(/status\/(\d+)/i);
    if (statusMatch) {
      statusId = statusMatch[1];
    }

    sendTelegramMessage(chatId, "⏳ Đang phân tích link X...");

    // ---- Resolve MP4 candidates: best + strictly lower bitrate alt ----
    var cand = getVideoCandidates(match[1]);
    console.log('PIPELINE-RESOLVE link=' + match[1] + ' → ' + (cand ? 'OK' : 'FAIL')
        + (cand && cand.altUrl ? ' (có bản dự phòng)' : ''));
    if (!cand) {
      sendTelegramMessage(chatId,
          "❌ Không resolve được video. Kiểm tra link (tweet có video?) rồi thử lại.");
      return _ok200('ok');
    }

    // ---- Chống trùng + cap + append: serialize để 2 webhook concurrent
    // không cùng đọc 9 job rồi cùng tạo, và không cùng lọt dedupe. ----
    var lock = LockService.getScriptLock();
    var gotLock = false;
    try {
      gotLock = lock.tryLock(10000);
    } catch (lockErr) {
      console.warn('LockService lỗi: ' + lockErr);
    }
    if (!gotLock) {
      sendTelegramMessage(chatId,
          '⏳ Hệ thống đang xử lý tin nhắn khác. Bạn thử lại sau ít phút.');
      return _ok200('busy');
    }

    var dupJob = null;
    var atCap = false;
    var jobId = '';
    try {
      dupJob = isQueuedOrRecent([cand.url, cand.altUrl], statusId);
      if (!dupJob) {
        atCap = jobsCreatedToday() >= DAILY_JOB_CAP;
        jobId = atCap
            ? pipelineCreateJob(cand.url, cand.altUrl, chatId, statusId, 'WAITING')
            : pipelineCreateJob(cand.url, cand.altUrl, chatId, statusId, 'PENDING');
      }
    } finally {
      try { lock.releaseLock(); } catch (releaseErr) {
        console.warn('releaseLock lỗi: ' + releaseErr);
      }
    }

    if (dupJob) {
      console.log('PIPELINE-DUP ' + cand.url + ' → job ' + dupJob);
      var dupMsg = 'ℹ️ Video này đã có trong hàng đợi hoặc đã upload (job ' + dupJob + '). Không tạo lại.';
      if (statusId) {
        dupMsg += ' (media_key=' + statusId + ')';
      }
      sendTelegramMessage(chatId, dupMsg);
      return _ok200('ok');
    }

    // Dispatch ngoài lock: repository_dispatch có thể chậm 15s, không giữ
    // lock của các message khác.
    if (!atCap) {
      dispatchToGitHub(jobId);
    }
    console.log('PIPELINE-JOB ' + jobId + ' chat=' + chatId
        + (atCap ? ' WAITING' : ' PENDING'));

    if (atCap) {
      sendTelegramMessage(chatId,
          '⏳ Đã lưu job ' + jobId + ' vào hàng đợi.\n' +
          'Hôm nay đã đủ ' + DAILY_JOB_CAP + ' job; sau 00:00 VN job sẽ vào hàng đợi xử lý.\n' +
          'Tuỳ số job đang chờ, có thể mất thêm vài ngày.\n' +
          'Bạn sẽ nhận thông báo ✅/❌ khi xong.');
    } else {
      var note = cand.altUrl
          ? '\n🔁 Nếu bản gốc quá 1.9GB, sẽ tự dùng bản chất lượng thấp hơn.'
          : '';
      sendTelegramMessage(chatId,
          "✅ Đã tạo job " + jobId + "\n" +
          "🎬 Video đang được tải và upload lên kênh (vài phút tuỳ dung lượng)." + note + "\n" +
          "Bạn sẽ nhận thông báo ✅/❌ khi xong.");
    }

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
  return _ok200('ok');
}

// ----------------------------------------------------------------------------
// Chọn 2 URL MP4: chất lượng cao nhất + một bậc thấp hơn (bitrate thấp hơn hẳn).
// getxbot trả list variant: { bitrate, content_type, url, size: "720x1280" }.
// `size` ở đây là ĐỘ PHÂN GIẢI, KHÔNG phải byte — không dùng để đo dung lượng.
// ----------------------------------------------------------------------------
function getVideoCandidates(xUrl) {
  // getxbot từ Google IPs có thể chậm/hang; mặc định 360s sẽ kẹt doPost
  // quá 60s update timeout của Telegram và lặng lẽ drop update.
  // 15s đủ dùng bình thường.
  var options = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({ videoId: xUrl, p: 1 }),
    muteHttpExceptions: true,
    timeoutSeconds: 15
  };
  try {
    var response = UrlFetchApp.fetch('https://www.getxbot.com/api/parse', options);
    var resData = JSON.parse(response.getContentText());
    if (resData.code !== 0 || !resData.data) return null;

    var videoKey = Object.keys(resData.data)[0];
    if (!videoKey) return null;
    var videoList = resData.data[videoKey] || [];

    var items = [];
    for (var i = 0; i < videoList.length; i++) {
      var v = videoList[i];
      if (!v || !v.url) continue;
      if (v.content_type && v.content_type !== 'video/mp4') continue;
      items.push({
        url: String(v.url).split('?tag')[0],
        bitrate: Number(v.bitrate) || 0
      });
    }

    var positive = [];
    for (var p = 0; p < items.length; p++) {
      if (items[p].bitrate > 0) positive.push(items[p]);
    }
    var pool = positive.length ? positive : items;  // GIF: chỉ có 1 entry bitrate 0
    pool.sort(function (a, b) { return b.bitrate - a.bitrate; });
    if (!pool.length) return null;

    var best = pool[0];
    var alt = '';
    for (var j = 1; j < pool.length; j++) {
      if (pool[j].url !== best.url && pool[j].bitrate < best.bitrate) {
        alt = pool[j].url;
        break;
      }
    }
    return { url: best.url, altUrl: alt };
  } catch (e) {
    return null;
  }
}

// ----------------------------------------------------------------------------
// Pipeline: tạo job vào Sheet queue + repository_dispatch (bản sao
// gas/telegram-pipeline.gs — hợp đồng cột PHẢI khớp worker/sheet_queue.py)
// ----------------------------------------------------------------------------
function pipelineCreateJob(mp4Url, mp4AltUrl, chatId, statusId, status) {
  var props = PropertiesService.getScriptProperties();
  var sheet = SpreadsheetApp.openById(props.getProperty('SHEET_ID'))
      .getSheetByName('Jobs');
  if (!sheet) {
    throw new Error('Không tìm thấy tab "Jobs" trong Sheet queue');
  }
  var now = Math.floor(Date.now() / 1000);
  var jobId = 'JOB-' + Utilities.formatDate(new Date(), 'UTC', 'yyyyMMdd-HHmmss')
      + '-' + Math.random().toString(36).slice(2, 8);
  var initialStatus = status || 'PENDING';
  // job_id, status, url, chat_id, created_at, updated_at, worker, msg_id,
  // error, retry_count, checkpoint, media_key, url_alt
  sheet.appendRow([
    jobId, initialStatus, mp4Url, String(chatId), String(now), '',
    '', '', '', '0', '0',
    statusId ? String(statusId) : '',
    mp4AltUrl ? String(mp4AltUrl) : ''
  ]);
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
      muteHttpExceptions: true,
      timeoutSeconds: 15
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
      muteHttpExceptions: true,
      timeoutSeconds: 10
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
      muteHttpExceptions: true,
      timeoutSeconds: 10
    });
  } catch (e) {
    // im lặng
  }
}

// Trả về job_id nếu cùng video đang chờ/đang chạy (status != FAILED) HOẶC đã DONE trong 24h.
// Trả về null nếu chưa có gì ⇒ được tạo job mới. Job FAILED thì cho gửi lại.
function isQueuedOrRecent(urls, statusId) {
  var wanted = urls || [];
  try {
    var rows = jobsSheet().getDataRange().getValues();
    var now = Math.floor(Date.now() / 1000);
    for (var i = rows.length - 1; i >= 1; i--) {
      var status = String(rows[i][1]);
      if (status === 'FAILED') continue;
      if (status === 'DONE') {
        var updated = Number(rows[i][5]) || 0;
        if (now - updated >= 24 * 3600) continue;
      }
      var rowMediaKey = String(rows[i][11] || '');
      if (statusId && rowMediaKey && rowMediaKey === statusId) return String(rows[i][0]);
      if (!statusId) {
        var rowUrls = [String(rows[i][2] || ''), String(rows[i][12] || '')];
        for (var u = 0; u < wanted.length; u++) {
          if (wanted[u] && rowUrls.indexOf(wanted[u]) >= 0) return String(rows[i][0]);
        }
      }
    }
  } catch (e) {
    console.warn('isQueuedOrRecent lỗi (bỏ qua, cho phép tạo job): ' + e);
  }
  return null;
}

// VN = UTC+7, không DST ⇒ cố định +0700
function startOfTodayVnEpoch() {
  var ymd = Utilities.formatDate(new Date(), 'Asia/Ho_Chi_Minh', 'yyyy/MM/dd');
  return Math.floor(new Date(ymd + ' 00:00:00 GMT+0700').getTime() / 1000);
}

function jobsCreatedToday() {
  try {
    var rows = jobsSheet().getDataRange().getValues();
    var start = startOfTodayVnEpoch();
    var n = 0;
    for (var i = 1; i < rows.length; i++) {
      if ((Number(rows[i][4]) || 0) >= start) n++;
    }
    return n;
  } catch (e) {
    console.warn('jobsCreatedToday lỗi (bỏ qua, cho phép tạo job): ' + e);
    return 0;
  }
}
/**
 * Response 200 trực tiếp KHÔNG redirect — Telegram webhook coi 302 (mà
 * ContentService.createTextOutput sinh ra) là "Wrong response" → retry vô
 * hạn rồi drop update (im lặng tuyệt đối). HtmlService.createHtmlOutput
 * trả 200 thẳng. Dùng cho MỌI return của doPost/doGet.
 */
function _ok200(body) {
  return HtmlService.createHtmlOutput(body || 'ok');
}
