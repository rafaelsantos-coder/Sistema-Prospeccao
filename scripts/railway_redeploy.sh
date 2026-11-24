#!/usr/bin/env bash
# Dispara redeploy do Sulnet Prospecta no Railway sem abrir o painel.
# Uso (Cloud Agent / CI / máquina local autenticada):
#   RAILWAY_DEPLOY_HOOK_URL=https://... ./scripts/railway_redeploy.sh
#   ./scripts/railway_redeploy.sh --wait   # espera /api/health + rota nova (opcional)
set -euo pipefail

HOOK_URL="${RAILWAY_DEPLOY_HOOK_URL:-}"
APP_URL="${APP_PUBLIC_URL:-https://sulnet-prospecta.up.railway.app}"
WAIT=0

for arg in "$@"; do
  case "$arg" in
    --wait) WAIT=1 ;;
    -h|--help)
      sed -n '2,6p' "$0"
      exit 0
      ;;
  esac
done

if [ -z "$HOOK_URL" ]; then
  echo "Defina RAILWAY_DEPLOY_HOOK_URL (Deploy Hook do serviço web no Railway)." >&2
  exit 1
fi

echo "Disparando redeploy Railway…"
curl -fsS -X POST "$HOOK_URL" >/dev/null
echo "Redeploy aceito."

if [ "$WAIT" -eq 1 ]; then
  echo "Aguardando health em ${APP_URL}/api/health…"
  for _ in $(seq 1 60); do
    if curl -fsS "${APP_URL}/api/health" | grep -q '"status":"ok"'; then
      echo "Produção saudável."
      exit 0
    fi
    sleep 10
  done
  echo "Timeout aguardando health após redeploy." >&2
  exit 2
fi
