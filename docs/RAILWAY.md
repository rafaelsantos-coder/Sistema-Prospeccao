# Publicação no Railway

## Arquitetura

- Serviço **Sulnet Prospecta**: aplicação FastAPI construída pelo `Dockerfile`.
- Serviço **Postgres**: banco gerenciado pelo Railway.
- O serviço web lê a porta dinâmica em `PORT`, escuta em `0.0.0.0` e expõe o healthcheck em `/api/health`.
- Na primeira inicialização, a aplicação cria as tabelas, unidades, metas e o administrador definido nas variáveis de ambiente.

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

Integrações opcionais:

```text
APIFY_TOKEN=
APIFY_ACTOR_ID=nwua9Gu5YrADL7ZDj
EVOLUTION_BASE_URL=
EVOLUTION_API_KEY=
EVOLUTION_INSTANCE=
RESEND_API_KEY=
RESEND_FROM_EMAIL=
```

## Configuração do serviço

1. Conectar o repositório GitHub e a branch `main`.
2. Adicionar PostgreSQL ao mesmo projeto.
3. Referenciar `DATABASE_URL` do serviço Postgres.
4. Configurar o healthcheck `/api/health`.
5. Gerar um domínio público no serviço web.
6. Entrar com a senha temporária e alterá-la no primeiro acesso.

O repositório não utiliza `railway.json`, pois o Config as Code legado foi descontinuado para novos serviços. As definições operacionais ficam no serviço Railway e o runtime permanece versionado no `Dockerfile`.

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

A senha temporária e a URL criada são mostradas apenas ao final da execução. Não versione a saída do terminal nem arquivos `.env`.
