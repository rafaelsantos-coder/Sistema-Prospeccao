#!/usr/bin/env bash
set -euo pipefail

# Publica o Sulnet Prospecta em um repositório privado do GitHub e cria um
# projeto Railway com serviço web + PostgreSQL. Requer git, gh e railway.

GITHUB_OWNER="${GITHUB_OWNER:-rafaelsantos-coder}"
GITHUB_REPO="${GITHUB_REPO:-sulnet-prospecta}"
REPO_SLUG="${GITHUB_OWNER}/${GITHUB_REPO}"
RAILWAY_PROJECT_NAME="${RAILWAY_PROJECT_NAME:-Sulnet Prospecta}"
RAILWAY_SERVICE_NAME="${RAILWAY_SERVICE_NAME:-web}"
ADMIN_NAME="${BOOTSTRAP_ADMIN_NAME:-Administrador Sulnet}"
ADMIN_EMAIL="${BOOTSTRAP_ADMIN_EMAIL:-rafael.santos@sulnet.com.br}"
ADMIN_PASSWORD="${BOOTSTRAP_ADMIN_PASSWORD:-$(python3 - <<'PY'
import secrets
print("Sulnet-" + secrets.token_urlsafe(18))
PY
)}"
SECRET_KEY="${SECRET_KEY:-$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(64))
PY
)}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Erro: o comando '$1' não está instalado." >&2
    exit 1
  fi
}

require_command git
require_command gh
require_command railway
require_command python3

gh auth status >/dev/null
railway whoami --json >/dev/null

if [[ ! -d .git ]]; then
  git init -b main
fi

git config user.name >/dev/null 2>&1 || git config user.name "Sulnet"
git config user.email >/dev/null 2>&1 || git config user.email "rafael.santos@sulnet.com.br"
git add -A
if ! git diff --cached --quiet; then
  git commit -m "feat: publica Sulnet Prospecta v0.1.1"
fi

if gh repo view "$REPO_SLUG" >/dev/null 2>&1; then
  REPO_URL="https://github.com/${REPO_SLUG}.git"
  if git remote get-url origin >/dev/null 2>&1; then
    git remote set-url origin "$REPO_URL"
  else
    git remote add origin "$REPO_URL"
  fi
  git push -u origin main
else
  gh repo create "$REPO_SLUG" --private --source=. --remote=origin --push \
    --description "CRM e prospecção automatizada para uso interno da Sulnet"
fi

railway init --name "$RAILWAY_PROJECT_NAME"
railway environment production

APP_JSON="$(railway add --service "$RAILWAY_SERVICE_NAME" --json)"
APP_SERVICE="$(printf '%s' "$APP_JSON" | python3 -c '
import json, sys
obj = json.load(sys.stdin)
print(obj.get("serviceName") or obj.get("name") or "web")
')"

DB_JSON="$(railway add --database postgres --json)"
DB_SERVICE="$(printf '%s' "$DB_JSON" | python3 -c '
import json, sys
obj = json.load(sys.stdin)
print(obj.get("serviceName") or obj.get("name") or "Postgres")
')"

DATABASE_REFERENCE="\${{${DB_SERVICE}.DATABASE_URL}}"

railway variable set \
  APP_NAME="Sulnet Prospecta" \
  APP_ENV=production \
  DATABASE_URL="$DATABASE_REFERENCE" \
  SECRET_KEY="$SECRET_KEY" \
  SESSION_COOKIE=sulnet_prospecta_session \
  DEFAULT_TIMEZONE=America/Sao_Paulo \
  BOOTSTRAP_ADMIN_NAME="$ADMIN_NAME" \
  BOOTSTRAP_ADMIN_EMAIL="$ADMIN_EMAIL" \
  BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PASSWORD" \
  --service "$APP_SERVICE" \
  --environment production \
  --skip-deploys \
  --json

railway environment edit --service-config "$APP_SERVICE" source.repo "https://github.com/${REPO_SLUG}"
railway environment edit --service-config "$APP_SERVICE" source.branch main
railway environment edit --service-config "$APP_SERVICE" deploy.healthcheckPath "/api/health"

railway up \
  --service "$APP_SERVICE" \
  --environment production \
  --ci \
  -m "Deploy inicial do Sulnet Prospecta v0.1.1"

DOMAIN_JSON="$(railway domain --service "$APP_SERVICE" --environment production --json)"

cat <<OUT

Publicação concluída.

GitHub: https://github.com/${REPO_SLUG}
Serviço Railway: ${APP_SERVICE}
Banco Railway: ${DB_SERVICE}
Domínio: ${DOMAIN_JSON}

Acesso inicial:
E-mail: ${ADMIN_EMAIL}
Senha temporária: ${ADMIN_PASSWORD}

A senha deverá ser alterada no primeiro acesso.
OUT
