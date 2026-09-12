#!/bin/bash
# 公開入口 php/kmorido.php を heteml (kurage.exbridge.jp) へ FTP 配置する。
# バックエンド設定 kmorido_config.php は同じ場所に置く（リポジトリには含めない）。
# 認証情報は aixec/.env の FTP_HOST / FTP_USER / FTP_PASS。
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . /home/kojima/work/aixec/.env; set +a
BACKEND="${KMORIDO_BACKEND_URL:-http://exbridge.ddns.net:18312}"
TMP=$(mktemp)
printf '<?php define("KMORIDO_BACKEND", "%s");\n' "$BACKEND" > "$TMP"
curl -sS -T php/kmorido.php "ftp://${FTP_USER}:${FTP_PASS}@${FTP_HOST}/web/kurage_exbridge_jp/kmorido.php"
curl -sS -T "$TMP" "ftp://${FTP_USER}:${FTP_PASS}@${FTP_HOST}/web/kurage_exbridge_jp/kmorido_config.php"
rm -f "$TMP"
echo "deployed: https://kurage.exbridge.jp/kmorido.php/"
curl -s -o /dev/null -w "public: %{http_code}\n" "https://kurage.exbridge.jp/kmorido.php/"
