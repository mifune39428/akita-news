#!/bin/bash
# ダブルクリックでニュースを手動更新し、GitHubへ反映する。
cd "$(dirname "$0")" || exit 1
echo "=== 秋田ニュース 手動更新 ==="
python3 collect.py || { echo "収集に失敗しました"; read -r -p "Enterで閉じる"; exit 1; }
if git diff --quiet docs/articles.json; then
  echo "新着はありませんでした。"
else
  git add docs/articles.json
  git commit -m "ニュース手動更新 $(date '+%Y-%m-%d %H:%M')" && git push && echo "公開サイトに反映しました。"
fi
read -r -p "Enterで閉じる"
