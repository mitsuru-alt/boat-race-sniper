#!/usr/bin/env bash
# 変更をコミットしてプッシュする。本当に main に載ったことを確認するまで成功扱いにしない。
# 使い方: bash pipeline/save.sh "コミットメッセージ" パス...
set -u
msg="$1"; shift

fail() {
  local enc
  enc=$(printf '%s' "$1" | sed ':a;N;$!ba;s/%/%25/g;s/\n/%0A/g')
  echo "::error::保存失敗: $msg%0A$enc"
  exit 1
}

git config user.name "sniper-bot"
git config user.email "sniper-bot@users.noreply.github.com"

paths=()
for p in "$@"; do [ -e "$p" ] && paths+=("$p"); done
[ ${#paths[@]} -eq 0 ] && { echo "保存するファイルなし"; exit 0; }

git add -- "${paths[@]}" || fail "git add に失敗"
if git diff --cached --quiet; then
  echo "変更なし"
  exit 0
fi
git commit -q -m "$msg" || fail "git commit に失敗"
mine=$(git rev-parse HEAD)

for i in 1 2 3 4 5; do
  if out=$(git push origin HEAD:main 2>&1); then
    git fetch -q origin main
    if git merge-base --is-ancestor "$mine" origin/main; then
      echo "::notice::保存しました: $msg"
      exit 0
    fi
    fail "プッシュは通ったが main にコミットが見当たらない%0A$out"
  fi
  echo "$out"
  # 他の実行が先にプッシュしていたら取り込む（データとコードは別の場所なので、ぶつかったら自分の版を優先）
  git fetch -q origin main || true
  if ! git -c merge.directoryRenames=false merge -q --no-edit -X ours origin/main; then
    git merge --abort 2>/dev/null
    fail "main の取り込みで衝突%0A$(git status --short | head -20)"
  fi
  sleep $((i * 5))
done
fail "プッシュを5回試して失敗%0A$out"
