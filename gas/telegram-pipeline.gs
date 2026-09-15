// gas/telegram-pipeline.gs — copy toàn bộ vào dự án Apps Script hiện tại
// (cùng project với doPost đang chạy).
// Script Properties bắt buộc: SHEET_ID, WORKER_REPO (dạng "owner/repo"), GITHUB_PAT.
// Sau khi thêm file: deploy lại Web App version mới.

/**
 * Gọi từ doPost() sau khi đã resolve ra link MP4.
 * Ví dụ trong doPost:
 *   var mp4Url = resolveXToMp4(xUrl);       // logic sẵn có của bạn
 *   pipelineCreateJobAndDispatch(mp4Url, chatId);
 * @param {string} mp4Url URL trực tiếp tới file .mp4
 * @param {number} chatId chat_id của người gửi request (để ghi vết)
 * @return {string} job_id đã tạo
 */
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
  // Thứ tự cột PHẢI khớp COLUMNS của worker/sheet_queue.py:
  // job_id, status, url, chat_id, created_at, updated_at, worker, msg_id, error, retry_count, checkpoint
  sheet.appendRow([jobId, 'PENDING', mp4Url, String(chatId), String(now), '', '', '', '', '0', '0']);
  dispatchToGitHub(jobId);
  return jobId;
}

/**
 * POST repository_dispatch để kích hoạt worker ngay (không đợi cron).
 * Nếu thất bại: KHÔNG throw — job vẫn PENDING; cron sweep của Actions
 * sẽ đánh dấu RETRY trong ≤ ~2h và job được xử lý ở lần chạy worker kế tiếp.
 */
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
