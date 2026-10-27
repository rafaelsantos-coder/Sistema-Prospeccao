# Sulnet Prospecta v0.1.1

Versão preparada para publicação interna da Sulnet no Railway.

## Incluído nesta versão

- Aplicação FastAPI com CRM, funil, atividades, campanhas e prospecção.
- Compatibilidade com PostgreSQL do Railway por `DATABASE_URL`.
- Inicialização com repetição de conexão enquanto o banco sobe.
- Healthcheck com validação real do banco em `/api/health`.
- Dockerfile com usuário não privilegiado e suporte à variável `PORT`.
- Administrador inicial criado por variáveis de ambiente.
- Alteração obrigatória da senha no primeiro acesso em produção.
- Publicador de contingência em `scripts/publicar_github_railway.sh`.
- 13 testes automatizados aprovados e smoke test HTTP aprovado.

## Destino recomendado

- Repositório privado: `rafaelsantos-coder/sulnet-prospecta`
- Projeto Railway: `Sulnet Prospecta`
- Serviço de aplicação: `web`
- Banco de dados: `Postgres`
- Branch de produção: `main`
- Healthcheck: `/api/health`

Nenhuma credencial, chave de API ou arquivo `.env` está incluído neste pacote.
