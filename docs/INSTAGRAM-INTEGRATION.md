# Instagram Prospecta — fase 1: entrada segura

Escopo: webhook separado, autenticação de assinatura HMAC-SHA256, persistência
criptografada, deduplicação atômica por evento e diagnóstico administrativo.
Não há envio de Direct, acionamento de SulIA, CRM, transferência ou sincronização
Manychat nesta fase. Não ligar a automação comercial antes de validar o roteamento.

Variáveis do serviço web: INSTAGRAM_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN,
INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET, INSTAGRAM_CONFIG_ID e
INSTAGRAM_WEBHOOK_VERIFY_TOKEN. Nunca gravar os valores no GitHub.

Callback: https://sulnet-prospecta.up.railway.app/webhooks/instagram

Depois de o deploy e a migration 0039 estarem confirmados, configurar o callback
com o valor de INSTAGRAM_WEBHOOK_VERIFY_TOKEN na Meta. Iniciar com testes
controlados; não ativar tráfego comercial até a caixa de atendimento estar
disponível. A Meta assina POST com X-Hub-Signature-256 e o app valida a conta
Instagram no payload. Eventos de outras contas não são armazenados.
Mensagens, mudanças e standby ficam na tabela instagram_webhook_events,
com conteúdo criptografado pelo secret_store já usado no Prospecta. Nenhum
telefone artificial é criado nem contatos são mesclados entre canais.
Antes da ativação comercial, implementar processamento e política de retenção
dos eventos temporários, inclusive exclusão de dados e auditoria LGPD.

Diagnóstico (login de ADMIN/GESTOR com permissão de conexões):
/settings/instagram/status
/settings/instagram/status?check=true (consulta Meta em leitura, sem enviar mensagens).

Validação: pytest -q tests/test_instagram_webhook_ingress.py
e migrações SQLite/PostgreSQL no CI.
A validação de conta não substitui testes das permissões de mensagens,
assinaturas, análise/publicação do aplicativo ou conversation routing.
