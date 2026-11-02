#!/usr/bin/env bash
# 大陆侧发布：从 NAS 上常驻实例的库导出 cn/hk 订阅，提交回仓库。
# 只写 public/proxies-{cn,hk}.txt 与 public/clash-{cn,hk}.yaml，不碰 Actions 维护的文件。
set -euo pipefail

REPO="${PROXYGATE_REPO:-/root/proxygate-publish}"
PROJECT="${PROXYGATE_PROJECT:-/home/zaoshili/proxypool}"
TOKEN_FILE="${PROXYGATE_TOKEN_FILE:-/root/.proxygate-token}"
REMOTE="${PROXYGATE_REMOTE:-https://github.com/zowshili/proxygate.git}"
BRANCH="${PROXYGATE_BRANCH:-main}"

[ -r "$TOKEN_FILE" ] || { echo "missing token file: $TOKEN_FILE" >&2; exit 1; }
[ -x "$PROJECT/.venv/bin/python" ] || { echo "missing venv: $PROJECT/.venv" >&2; exit 1; }

TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
cd "$REPO"
git remote set-url origin "https://x-access-token:${TOKEN}@github.com/zowshili/proxygate.git"
git config user.name "zowshili"
git config user.email "zowshili@users.noreply.github.com"

cd "$PROJECT"
"$PROJECT/.venv/bin/python" "$PROJECT/scripts/publish_region.py" \
  -c "$PROJECT/config.yaml" --out "$REPO/public"

cd "$REPO"
for i in 1 2 3; do
  if git -c http.version=HTTP/1.1 fetch -q origin "$BRANCH"; then
    git reset --soft FETCH_HEAD
    git add public
    if git diff --cached --quiet; then
      echo "publish: no change"
      exit 0
    fi
    git commit -q -m "chore(data): refresh cn/hk from mainland $(date -u +%Y-%m-%dT%H:%MZ)"
    if git -c http.version=HTTP/1.1 push -q origin "HEAD:${BRANCH}"; then
      echo "publish: pushed"
      exit 0
    fi
  fi
  sleep 5
done

echo "publish: push failed after 3 attempts" >&2
exit 1
