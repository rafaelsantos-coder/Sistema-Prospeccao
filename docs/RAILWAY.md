# Publicação no Railway

## Arquitetura

- Serviço **web**: aplicação FastAPI construída pelo `Dockerfile`.
- Serviço **worker** (recomendado): mesmo imagem, comando
  `python -m app.workers.ai_sdr_worker`.
- Serviço **Postgres**: banco gerenciado pelo Railway.
- O serviço web lê a porta dinâmica em `PORT`, escuta em `0.0.0.0` e expõe o
  healthcheck em `/api/health`.
- Schema de produção via Alembic (`python -m app.migrate`). O seed cria
  administrador, unidades e perfis base da SulIA.

## Variáveis obrigatórias do serviço web

```text
APP_NAME=Sulnet Prospecta
APP_ENV=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=<valor aleatório com no mínimo 32 caracteres>
SESSION_COOKIE=sulnet_prospecta_session
DEFAULT_TIMEZONE=America/Sao_Paulo
BOOTSTRAP_ADMIN_NAME=Administrador Sulnet
BOOTSTRAP_ADMIN_EMAIL=<e-mail interno>
BOOTSTRAP_ADMIN_PASSWORD=<senha temporária forte com no mínimo 12 caracteres>
```

Integrações comuns:

```text
APP_PUBLIC_URL=https://<dominio-publico>
APIFY_TOKEN=
APIFY_ACTOR_ID=nwua9Gu5YrADL7ZDj
ZAPI_BASE_URL=https://api.z-api.io
ZAPI_INSTANCE_ID=
ZAPI_INSTANCE_TOKEN=
ZAPI_CLIENT_TOKEN=
ZAPI_WEBHOOK_SECRET=<≥24 caracteres>
OPENAI_API_KEY=
AI_SDR_ENABLED=true
AI_SELLER_ENABLED=false
AI_AUTONOMOUS_ENABLED=false
RESEND_API_KEY=
RESEND_FROM_EMAIL=
REDIS_URL=
```

Vendedor IA (somente após checklist em
[`VENDEDOR-IA-ATIVACAO.md`](VENDEDOR-IA-ATIVACAO.md)):

```text
AI_SELLER_ENABLED=true
AI_AUTONOMOUS_ENABLED=true
SULNET_CRM_BASE_URL=https://...
SULNET_AI_INTEGRATION_KEY=<≥24 caracteres>
ZAPSIGN_WEBHOOK_SECRET=<≥24 caracteres>
```

`AI_ENGINEER_AUTONOMOUS_PR_ENABLED` em produção também exige
`AI_ENGINEER_ALLOW_PRODUCTION_PR=true`.

## Configuração do serviço

1. Conectar o repositório GitHub e a branch `main`.
2. Adicionar PostgreSQL ao mesmo projeto.
3. Referenciar `DATABASE_URL` do serviço Postgres.
4. Configurar o healthcheck `/api/health`.
5. Gerar um domínio público no serviço web e definir `APP_PUBLIC_URL`.
6. Subir o worker com as mesmas variáveis sensíveis do web.
7. Entrar com a senha temporária e alterá-la no primeiro acesso.

O repositório não utiliza `railway.json`, pois o Config as Code legado foi
descontinuado para novos serviços. As definições operacionais ficam no serviço
Railway e o runtime permanece versionado no `Dockerfile`.

## Publicação automatizada pela CLI

Com GitHub CLI e Railway CLI autenticados, execute na raiz do projeto:

```bash
./scripts/publicar_github_railway.sh
```

Valores opcionais podem ser definidos antes da execução, por exemplo:

```bash
GITHUB_REPO=sulnet-prospecta \
BOOTSTRAP_ADMIN_EMAIL=usuario@sulnet.com.br \
./scripts/publicar_github_railway.sh
```

A senha temporária e a URL criada são mostradas apenas ao final da execução. Não
versione a saída do terminal nem arquivos `.env`.
