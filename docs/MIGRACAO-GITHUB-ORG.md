# Migração GitHub → SULNET-TELECOM/sulnet-prospecta

Repositório canônico a partir de 27/11/2026:

**https://github.com/SULNET-TELECOM/sulnet-prospecta**

O histórico veio de `rafaelsantos-coder/Sistema-Prospeccao`. Novos PRs, CI e
deploys devem usar apenas o repositório da organização.

## Checklist operacional

1. **GitHub**
   - Confirmar que `main` no org tem o mesmo tip do espelho.
   - Recriar secrets de Actions (`RAILWAY_TOKEN` ou `RAILWAY_DEPLOY_HOOK_URL`,
     CodeQL, etc.) no repo novo.
   - Ajustar branch protection / required checks.

2. **Railway**
   - Serviço **web** (e workers): Settings → Source → apontar para
     `SULNET-TELECOM/sulnet-prospecta`, branch `main`.
   - Gerar novo Deploy Hook e gravar no secret do **novo** repo.

3. **Cursor Cloud Agent**
   - Ambiente do agente deve usar o repo
     `https://github.com/SULNET-TELECOM/sulnet-prospecta`
     (não o antigo pessoal).

4. **Variáveis de app**
   - `AI_ENGINEER_GITHUB_REPOSITORY=SULNET-TELECOM/sulnet-prospecta`
   - Token GitHub do Engineer IA com acesso ao org, se usado.

5. **Repo antigo**
   - Após validar produção no org: arquivar
     `rafaelsantos-coder/Sistema-Prospeccao` e deixar README apontando para o
     novo endereço.
