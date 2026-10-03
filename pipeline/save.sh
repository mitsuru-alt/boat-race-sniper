#!/usr/bin/env bash
# 変更をコミットしてプッシュする（失敗したら理由をActionsの画面に出す）
# 使い方: bash pipeline/save.sh "コミットメッセージ" パス...
set -u
msg="$1"; shift
git config user.name "sniper-bot"
git config user.email "sniper-bot@users.noreply.github.com"
git add "$@"
if git diff --cached --quiet; then
  echo "変更なし"
  exit 0
fi
git commit -q -m "$msg"
for i in 1 2 3; do
  if out=$(git push origin HEAD:main 2>&1); then
    echo "$out"
    echo "::notice::保存しました: $msg"
    exit 0
  fi
  echo "$out"
  # 他の実行が先にプッシュしていたら取り込んでやり直す
  git fetch -q origin main && git rebase -q origin/main || true
  sleep $((i * 5))
done
enc=$(printf '%s' "$out" | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
echo "::error::プッシュ失敗%0A$enc"
exit 1
