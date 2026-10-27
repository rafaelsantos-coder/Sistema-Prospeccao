# Sulnet Prospecta — CRM interno da Sulnet

Aplicação web interna para centralizar prospecção, CRM, follow-ups, campanhas e indicadores comerciais da Sulnet. Esta entrega foi projetada como uma evolução do repositório `zxmarketingdigital/prospeccao-automatizada`, preservando a integração com seus módulos de Apify, geração de copy e rate limiter quando executada dentro do repositório original.

## O que já funciona

- Login com perfis de Administrador, Gestor, Supervisor e Consultor.
- Visão por unidade, equipe e responsável.
- CRM obrigatório com funil completo da prospecção à instalação.
- Cadastro, deduplicação, score Sulnet, temperatura e MRR estimado.
- Follow-ups automáticos: primeira ação e régua D+3, D+10 e D+20.
- Metas diárias: Loja 40 atividades, Digital 60 atividades, PAP 20 novos leads e B2B 10 visitas.
- Tráfego pago separado e protegido da redistribuição automática.
- Histórico de atividades e trilha de auditoria.
- Busca por segmento/cidade via Apify, quando configurado.
- Campanhas em rascunho, com aprovação gerencial e limites por canal.
- Importação do `prospects.db` criado pelo software original.
- Exportação de leads em CSV.
- Interface responsiva nas cores Sulnet.

## Rodar localmente

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python -m app.cli init-db --demo
uvicorn app.main:app --reload
```

Acesse `http://localhost:8000`.

Ambiente de demonstração:

- E-mail: `admin@sulnet.local`
- Senha: `Sulnet@2026!`

A senha demonstrativa deve ser substituída antes de qualquer publicação.


## Publicação no Railway

O projeto está preparado para um serviço web conectado ao PostgreSQL gerenciado do Railway. O `Dockerfile` lê a variável dinâmica `PORT`, a aplicação converte a URL PostgreSQL para o driver Psycopg 3 e o healthcheck está disponível em `/api/health`.

A configuração completa está em [`docs/RAILWAY.md`](docs/RAILWAY.md).

Como alternativa pela linha de comando, o script abaixo cria o repositório privado, o projeto Railway, o serviço web, o PostgreSQL, as variáveis, o healthcheck e o primeiro deploy:

```bash
./scripts/publicar_github_railway.sh
```

O script exige `git`, GitHub CLI (`gh`) e Railway CLI autenticados. Ele gera os segredos apenas em tempo de execução e não os grava no repositório.

## Aproveitamento do repositório original

Quando estes arquivos forem adicionados à raiz do repositório já analisado, o adaptador `app/services/legacy_adapter.py` tenta reutilizar automaticamente:

- `scripts/apify_scraper.py` para score e normalização legados;
- `scripts/copy_generator.py` para mensagens por segmento;
- `scripts/rate_limiter.py` para os contadores de WhatsApp e e-mail.

O novo `score` não substitui silenciosamente o score antigo. O valor original é salvo em `legacy_score`, e a qualificação final usa critérios adequados à Sulnet: cobertura, contato válido, aderência B2B, origem e intenção.

## Importar os leads existentes

```bash
python -m app.cli import-legacy \
  --path ~/.operacao-ia/data/prospects.db \
  --user admin@sulnet.local \
  --branch SRO
```

A importação deduplica por telefone e e-mail e converte os estados antigos para o novo funil.

## Variáveis de integração

Preencha no `.env`:

- `APIFY_TOKEN`
- `EVOLUTION_BASE_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE`
- `RESEND_API_KEY`, `RESEND_FROM_EMAIL`

As chaves não são exibidas pela interface. Em produção, use PostgreSQL, HTTPS e um segredo de sessão forte.

## Subir com Docker

```bash
cp .env.example .env
# ajuste SECRET_KEY, senhas e tokens
POSTGRES_PASSWORD='senha-forte' docker compose up -d --build
```

## Decisões desta primeira versão

1. Nenhum disparo real é ativado automaticamente. Campanhas exigem aprovação gerencial e a integração de envio será habilitada após homologação.
2. A busca do Apify roda em tarefa de segundo plano simples. A fase seguinte deve usar uma fila persistente, como Redis/RQ ou Celery.
3. O banco local é SQLite para desenvolvimento; o `docker-compose.yml` usa PostgreSQL para o ambiente interno.
4. O login é próprio do MVP. A evolução recomendada é integrar SSO/Google Workspace ou Microsoft Entra ID.

## Testes

```bash
pytest
```
