# OpenWA no Sulnet Prospecta - gate de seguranca (27/11/2026)

## Escopo implementado

**Somente homologacao de leitura.** O Prospecta nao instala/importa o OpenWA
na mesma aplicacao, nao recebe webhooks e nao envia mensagens pelo OpenWA.
O terceiro provedor e visivel em Configuracoes > OpenWA. Por padrao
OPENWA_ENABLED=false e nao faz chamadas de rede em background.

A integracao Z-API e a Cloud API oficial da Meta nao foram alteradas.

## Configuracao de teste

As variaveis sao fornecidas apenas no servidor do Prospecta, nunca na interface:

- OPENWA_ENABLED=false (padrao; altere somente em staging para teste)
- OPENWA_BASE_URL=http://openwa.railway.internal:3000 (servico isolado, exemplo)
- OPENWA_ALLOWED_HOST=openwa.railway.internal (host literal identico ao da URL)
- OPENWA_API_KEY=(chave OPERATOR/VIEWER de leitura com allowedSessions restrito)
- OPENWA_SESSION_ID=(ID da sessao de teste, letras/numeros/hifens)

URLs arbitrarias, IPs literais, localhost, credenciais embutidas, redirecionamentos,
caminhos/query/fragmentos extras e HTTP fora da rede privada Railway sao recusados.
Cliente HTTP sem proxies de ambiente, sem follow_redirects, timeout curto e resposta
limitada a 16 KiB. O token nao e persistido no banco, UI ou logs.

## Achados de seguranca que impedem colocar em producao

Fonte upstream: https://github.com/rmyndharis/OpenWA/blob/main/SECURITY.md
e https://github.com/rmyndharis/OpenWA/blob/main/docs/04-security-design.md

- A documentacao OpenWA admite **ausencia de criptografia em repouso** para
  determinadas credenciais e estados. Exigir volume com controle de acesso,
  isolamento no Railway, backup criptografado e politica de retencao.
- Plugins rodando em worker threads nao sao barreira de seguranca contra
  plugin malicioso. **Proibir instalacao/execucao de plugins de terceiros**.
- Nao montar Docker socket nem instalar sidecar Docker proxy; nao habilitar
  orquestracao de containers pelo gateway.
- O gateway e um cliente WhatsApp nao oficial; existe risco permanente de
  banimento, falha de entrega e desconformidade com regras do WhatsApp.
  **Nao utilizar numeros oficiais ou essenciais da Sulnet** em testes.

## Gate obrigatorio antes de liberar envio, recebimento e campanhas

1. Congelar commit e imagem por **digest**. Verificar origem/autenticidade e
   SBOM, licencas e scripts pos-instalacao.
2. Executar varredura independente de vulnerabilidades da imagem e lockfiles
   (Trivy, npm audit, malware scan) e revisao manual de auth, SSRF, RCE,
   shell/process, dados de sessao, middleware, plugins e webhooks.
3. Exigir container separado, non-root, filesystem readonly onde possivel,
   sem capabilities adicionais, sem Docker socket, limites de CPU/RAM,
   egress controlado e volume somente para dados proprios.
4. Desativar ALLOW_DEV_API_KEY, ENABLE_SWAGGER, importacao/execucao de plugins,
   endpoints publicos de administracao e outras rotas nao necessarias.
   Criar chave de escopo minimo: role VIEWER para status, allowedSessions
   so da sessao de teste, expiracao, rotacao e revogacao.
5. API protegida por rede privada; nao expor IP publico; sem permissao de
   conectar aos bancos, Redis e variaveis do Prospecta. O gateway nao recebe
   DATABASE_URL, SECRET_KEY, tokens Z-API/Meta ou chaves OpenAI.
6. Projetar webhooks com assinatura HMAC verificada em bytes brutos,
   protecao de replay/idempotencia, limite de corpo, schema estrito,
   autenticacao de origem e vinculacao exata sessao->fila->conversa.
   Nenhuma mensagem de sessao diferente pode ser importada.
7. Executar testes de integracao (filas, permissoes, CPF, LGPD,
   blacklist, deduplicacao, importacao de midia, carga e falhas).
8. Teste de intrusao em staging, revisao independente do PR,
   backup/rollback comprovados, politica de atualizacao e monitoramento de CVEs.

**Nenhum desses itens e considerado aprovado apenas pela existencia deste
documento.** Este PR nao aprova uso comercial e nao entrega garantia de
ausencia absoluta de malware ou vulnerabilidades.
