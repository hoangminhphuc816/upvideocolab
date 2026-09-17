#!/usr/bin/env bash
# Setup sweep dispatcher: PAT GitHub + cron-job.org → job gọi workflow_dispatch 2h/lần
# Chạy: bash setup_cronjob_sweep.sh
set -euo pipefail

REPO="hoangminhphuc816/upvideocolab"
WF="worker.yml"

echo "=== Bước 1: PAT GitHub (fine-grained, Actions:RW) ==="
echo "Tạo tại: https://github.com/settings/personal-access-tokens/new"
echo "  - Token name: cronjob-sweep-dispatch"
echo "  - Expiration: 90 days"
echo "  - Repository access: Only select repositories → upvideocolab"
echo "  - Repository permissions: Actions = Read and write"
echo
read -rp "Dán PAT vừa tạo (github_pat_...): " GH_PAT
if [[ ! "$GH_PAT" =~ ^github_pat_ ]]; then
  echo "⚠️  Token không bắt đầu bằng github_pat_ — kiểm tra lại (fine-grained token mới có định dạng này)"; exit 1
fi

echo
echo "→ Smoke test PAT (gọi dispatch API 1 lần, expected HTTP 204)..."
CODE=$(curl -s -o /tmp/smoke_resp.txt -w "%{http_code}" -X POST \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $GH_PAT" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/repos/$REPO/actions/workflows/$WF/dispatches" \
  -d '{"ref":"main","inputs":{"mode":"sweep"}}')

if [[ "$CODE" == "204" ]]; then
  echo "✓ HTTP 204 — PAT ĐÚNG quyền. Kiểm tra thêm: GitHub → repo → Actions → có run 'video-controller' mới (event workflow_dispatch, job sweep)."
elif [[ "$CODE" == "403" || "$CODE" == "404" ]]; then
  echo "✗ HTTP $CODE — PAT THIẾU QUYỀN. Nội dung lỗi:"
  cat /tmp/smoke_resp.txt; echo
  echo "→ Vào GitHub → Developer settings → Fine-grained tokens → token vừa tạo → Edit →"
  echo "  Repository permissions: Actions = 'Read and write' → Update. Rồi chạy lại script này."
  exit 1
else
  echo "✗ HTTP $CODE (bất thường). Nội dung:"; cat /tmp/smoke_resp.txt; exit 1
fi

echo
echo "=== Bước 2: API key cron-job.org ==="
echo "Lấy tại: https://console.cron-job.org → avatar → Settings → REST API → Create API key"
echo "(chưa có tài khoản → đăng ký free bằng email)"
echo
read -rp "Dán API key cron-job.org: " CJ_KEY

echo
echo "→ Tạo job 'github-sweep-dispatch' (mỗi 2h lúc :07, mode=sweep)..."
RESP=$(curl -s -X PUT https://api.cron-job.org/jobs \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $CJ_KEY" \
  -d "$(cat <<EOF
{"job":{"title":"github-sweep-dispatch","url":"https://api.github.com/repos/$REPO/actions/workflows/$WF/dispatches","enabled":true,"requestMethod":1,"requestTimeout":30,"extendedData":{"headers":{"Accept":"application/vnd.github+json","Authorization":"Bearer $GH_PAT","X-GitHub-Api-Version":"2022-11-28"},"body":"{\"ref\":\"main\",\"inputs\":{\"mode\":\"sweep\"}}"},"schedule":{"timezone":"Asia/Ho_Chi_Minh","minutes":[7],"hours":[0,2,4,6,8,10,12,14,16,18,20,22],"mdays":[-1],"months":[-1],"wdays":[-1]},"notification":{"onFailure":true,"onFailureCount":2}}}
EOF
)")

JOB_ID=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('jobId',''))" 2>/dev/null || true)

if [[ -n "$JOB_ID" ]]; then
  echo "✓ Đã tạo job — jobId = $JOB_ID"
  echo
  echo "=== Bước 3: Verify ==="
  echo "1. console.cron-job.org → Cronjobs → job 'github-sweep-dispatch' → nút ▶ Run now"
  echo "   → History hiện entry status OK (1xx/2xx)"
  echo "2. GitHub → repo → Actions → run 'video-controller' MỚI thứ 2 xuất hiện (trigger: workflow_dispatch)"
  echo "3. Xong! Sweep giờ chạy tự động mỗi 2h lúc :07 do cron-job.org điều phối."
  echo
  echo "Lưu ý: PAT nằm trong job config (headers). PAT hết hạn sau 90 ngày → cron-job.org"
  echo "gửi email onFailure → tạo PAT mới → console → sửa job → headers → Authorization."
else
  echo "✗ Tạo job thất bại. Response thô:"
  echo "$RESP"
  echo "(401 = sai API key; 429 = quá rate limit PUT /jobs — đợi 1 phút chạy lại)"
  exit 1
fi