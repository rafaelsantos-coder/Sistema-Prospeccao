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


## Fase 2A — prévia administrativa da prospecção (sem disparos)

GET /settings/instagram/prospecting/preview?mode=commercial&limit=100

O mesmo controle ADMIN/GESTOR + settings.connections.view da fase 1 protege
a prévia. A leitura de até 200 alterações recentes é restrita à conta
configurada, e retorna apenas contagens e textos estáticos sugeridos, sem
expor identificadores ou comentários de clientes. mode=all inclui sugestões
genéricas; media_id permite filtrar uma publicação. O resultado não comprova
elegibilidade de envio e não altera banco, filas, CRM ou WhatsApp.

A triagem prioriza recusa, reclamações e atendimento antes de interesse,
valor ou cobertura. Classificação por palavras-chave é inicial e precisa de
calibração; elogios recebem apenas sugestão não comercial. Entradas
desconhecidas ficam em revisão por padrão. Eventos duplicados pelo ID do
comentário são ignorados na amostra. Lives ficam em revisão manual.

Próximas etapas: confirmar comentários e Direct reais (HTTP 200 sozinho não
prova persistência), permissões e assinatura da conta; construir cadastro
de regras/publicações, outbox com unicidade por conta/comentário, auditoria,
supressão por pessoa e opt-out, validação da data real de criação do comentário
e da janela de mensagens, controle de erros ambíguos sem reenvio automático,
modo desligado por padrão, caixa de conversas Instagram independente e acesso
por fila Digital, registro CRM autorizado e indicadores. Não enviar mensagens
a clientes sem validar essas etapas e confirmar a configuração da campanha.

Não há evento de novos seguidores autorizado nesta fase. Sem Manychat,
scraping ou credenciais alternativas. Sem alteração do canal WhatsApp.

Testes: pytest -q tests/test_instagram_prospecting.py tests/test_instagram_webhook_ingress.py


## Fase 2B — Instagram – Digital (caixa de entrada inicial)

Flag INSTAGRAM_INBOX_ENABLED desligada por padrão. Não habilitar em produção
até confirmar eventos reais, política de retenção/exclusão, testes de
autorização e revisão LGPD. Quando habilitada, mensagens Direct válidas
da conta configurada recebidas pelo webhook são registradas em três tabelas
exclusivas; sem criar telefone, contato, fila ou lead do WhatsApp.
A caixa inicial recebe também Direct orgânico; ainda não atribui origem
comercial a campanhas. O identificador da conversa e o ID da mensagem são
hashes, e o texto é criptografado. Não há reprocessamento histórico.
Mídia aparece como aviso; não há download/visualização ainda.

Rotas independentes: /instagram/inbox e /instagram/members.
O gestor/administrador com permissão de gestão de usuários concede acesso
específico para a conta; ninguém é incluído automaticamente. Vendedores
e supervisores vinculados veem apenas metadados das conversas não atribuídas,
podem assumir atomicamente uma conversa e só abrem o próprio histórico.
A administração autorizada pode consultar o histórico. A revogação não
transfere conversas nem expõe o histórico a outro vendedor; transferência
e recuperação de conversas órfãs exigem fluxo administrativo posterior.
Requisições de alteração validam CSRF. A navegação é por rota direta nesta fase.

Não envia mensagens, não registra CRM, não executa SulIA, não usa Manychat
e não altera arquivos de WhatsApp, Z-API ou suas configurações. A interface
ainda é inicial. Próximas etapas: autenticação integrada e auditoria de
membros/assunção em testes, retenção, política de exclusão/edição, associação
de campanha, outbox e envio oficial com janela validada, CRM autorizado,
indicadores e menu integrado. Não ativar tráfego real antes dessas validações.

Testes: pytest -q tests/test_instagram_inbox.py tests/test_instagram_webhook_ingress.py tests/test_instagram_prospecting.py
