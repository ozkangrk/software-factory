#!/usr/bin/env bash
# Multica'yı upstream'e bağlı tutarak çalıştır:
#   1) upstream/main'i fetch et, ~/multica/multica'yı güncelle
#   2) compose'u 'latest' tag ile ayağa kaldır/köprü kur
#   3) köprüyü systemd-ish döngüde değil, `factory-multica --interval` ile sür
# Kullanım: ./scripts/multica-follow.sh [--restart]
set -euo pipefail
REPO="${MULTICA_REPO:-$HOME/multica/multica}"
FACTORY="${FACTORY_REPO:-$HOME/Desktop/software-factory}"
cd "$REPO"

echo "== upstream fetch =="
git remote set-url origin https://github.com/multica-ai/multica.git 2>/dev/null || true
git fetch --depth 1 origin main
BEFORE=$(git rev-parse HEAD); git reset --hard origin/main; AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" != "$AFTER" ]; then
  echo "güncellendi: ${BEFORE:0:8} -> ${AFTER:0:8}"
  # image tag'ı upstream ile sabit tutmak için build etmek isteyebilirsin:
  # docker compose -f docker-compose.selfhost.build.yml up -d --build
  docker compose -f docker-compose.selfhost.yml pull || true
  NEEDS_RESTART=1
else
  echo "upstream değişmedi (${AFTER:0:8})"
  NEEDS_RESTART=0
fi

if [ "${1:-}" = "--restart" ] || [ "${NEEDS_RESTART}" = "1" ]; then
  docker compose -f docker-compose.selfhost.yml up -d
  sleep 5
fi
curl -sf http://127.0.0.1:8080/healthz >/dev/null && echo "backend: OK" || { echo "backend: ÇÖKTÜ"; exit 1; }

echo "== köprü =="
cd "$FACTORY"
if [ -z "${MULTICA_TOKEN:-}" ]; then
  echo "MULTICA_TOKEN/MULTICA_WORKSPACE_ID env'lerini ver (UI: Settings → API Token)" >&2
  exit 2
fi
.venv/bin/python -m factory.multica "$FACTORY/ws" --interval 30
