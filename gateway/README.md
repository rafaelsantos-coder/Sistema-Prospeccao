# Sulnet WhatsApp Gateway (motor proprio)

Implementacao original para o Prospecta: HTTP/JSON, controles de sessao,
QR Code, envio de textos, entrega de eventos, armazenamento cifrado AES-256-GCM,
autenticacao de chamadas internas e outbox resiliente.

A **biblioteca de protocolo** \`@whiskeysockets/baileys\` e software de terceiro;
nao e o OpenWA. Ela executa localmente no nosso container, mas comunica com
servidores externos do WhatsApp atraves de protocolo nao oficial.

## Variaveis obrigatorias
- \`WA_API_KEY\`: token aleatorio com >= 40 caracteres, exclusivo do gateway
- \`WA_WEBHOOK_KEY\`: token aleatorio com >= 40 caracteres para HMAC eventos
- \`WA_WEBHOOK_URL\`: URL HTTPS do Prospecta \`/webhooks/sulnet-wa\`
- \`WA_ENCRYPTION_KEY\`: 32 bytes codificados em base64, diferentes de outras chaves
- \`WA_STORAGE_BACKEND=S3\`: bucket S3 privado, exclusivamente para credenciais e eventos cifrados
- \`PORT=3000\`, \`WA_AUTOSTART=false\` na homologacao
- \`WA_S3_ENDPOINT\`, \`WA_S3_BUCKET\`, \`WA_S3_REGION\`, \`WA_S3_ACCESS_KEY_ID\`, \`WA_S3_SECRET_ACCESS_KEY\` (credenciais isoladas).

## Controles obrigatorios
- Railway service SEM dominio publico; chamadas API somente pela rede privada
- NAO configurar DATABASE_URL/REDIS_URL/tokens Z-API ou Meta no gateway
- SEM Docker socket, plugins ou scripts de instalacao de terceiros
- Egress somente para WhatsApp e endpoint HTTPS de webhook, quando ACLs disponiveis
- Bucket S3 sem volume root, acesso exclusivo, backup e rotacao de segredos
- QR nao e exposto a visitantes anonimos; exige token administrativo do backend
- Mensagens de grupos, listas e LIDs sem telefone verificavel sao ignoradas
- Dados de autenticacao e outbox cifrados com AES-256-GCM
- Chaves e secrets nao aparecem nas respostas

### Limites atuais da etapa 1
Anexos sao sinalizados como recebidos, mas arquivos de midia ainda nao sao
transportados/armazenados; nao interpretar o placeholder como audio transcrito.
Os estados e QR precisam de ensaio com numero de TESTE.
O escopo de rede e auditoria de bibliotecas precisa ser verificado antes
da conexao real de qualquer numero comercial.
