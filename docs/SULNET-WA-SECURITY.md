# Motor WhatsApp Sulnet - segurança e requisitos de operação

## Objetivo

Motor de execução própria no Railway, sem copiar o código OpenWA,
mantendo Z-API e Meta oficial independentes. O Prospecta possui um cliente
Python e telas administrativas próprias. A comunicação com WhatsApp utiliza a
biblioteca de terceiros \`@whiskeysockets/baileys\`, licenciada de forma
separada. Isso NÃO é a API oficial da Meta.

## Status e limites

As alterações deste PR são condicionais: \`SULNET_WA_ENABLED=false\` por
padrão. Não há migração de números nem substituição dos provedores atuais.
Somente usuários ADMIN/GESTOR podem criar, ativar ou parar sessões.
Sessões nascem desativadas; precisam ser pareadas e habilitadas.

- Texto de entrada/saída implementado; **não há suporte completo de mídia**.
- Áudio, vídeo, imagem e documento recebidos são identificados como eventos,
  mas **não são baixados nem transcritos**, não passam para CRM nem IA.
- Não foi demonstrada compatibilidade real com WhatsApp sem pareamento no
  laboratório; testes unitários não substituem teste de dispositivo real.
- A criptografia em repouso do gateway usa AES-256-GCM, com chave própria.
- Para mensagens e conversas, há vínculo estrito por sessão/provedor/fila;
  webhooks exigem HMAC, timestamp limitado, ID imutável e deduplicação.
- Todos os envios passam pela blacklist central.

## Deploy obrigatório em serviço separado

Dockerfile: \`gateway/Dockerfile\`. Builder usa \`npm ci --ignore-scripts\` e
\`package-lock.json\`, imagem com SHA fixado e usuário \`node\` sem privilégios.
Serviço \`sulnet-wa\`, sem domínio público e sem Docker socket, sem variáveis
Z-API, Meta, OpenAI, DATABASE_URL ou REDIS_URL. Armazenamento persistente exclusivamente em S3 criptografado, sem volumes root.

O gateway exige:
\`WA_API_KEY\`, \`WA_WEBHOOK_KEY\` (ambos >=40 caracteres aleatórios),
\`WA_ENCRYPTION_KEY\` (32 bytes base64), \`WA_WEBHOOK_URL\`
(\`https://<prospecta>/webhooks/sulnet-wa\`),
\`WA_STORAGE_BACKEND=S3\` e \`WA_AUTOSTART=false\`.

O Prospecta exige:
\`SULNET_WA_ENABLED=false\` (trocar para true SOMENTE quando aprovado),
\`SULNET_WA_BASE_URL=http://sulnet-wa.railway.internal:3000\`,
\`SULNET_WA_ALLOWED_HOST=sulnet-wa.railway.internal\`, 
\`SULNET_WA_API_KEY\` igual à do gateway e \`SULNET_WA_WEBHOOK_KEY\`
igual à do gateway.

## Aceite antes de número comercial

1. Testes Python e Node verdes com resultados auditáveis.
2. \`npm audit\` sem altos/críticos, imagem Trivy sem altos/críticos
   e revisão de scripts/transitivas/licenças.
3. Container e banco isolados; nenhum segredo compartilhado com Prospecta.
4. Hostname privado, HMAC e idempotência comprovados em staging.
5. Pareamento real com número exclusivo de teste; envio, recepção,
   reconexão, troca de fila e perda de rede.
6. Backup/restauração criptografados e rollback ensaiados.
7. Pen-test focado em webhook, QR, sessão, SSRF e permissões.
8. LGPD: controle de acesso, opt-in/opt-out, eliminação de dados, logs.
9. Revisão independente de engenharia e segurança.
10. Observação: biblioteca não oficial pode causar bloqueio no WhatsApp.

**Não existe garantia absoluta de 100% de ausência de malware ou brechas.**
Implantação do menu com feature flag desativada NÃO significa aprovação
operacional do motor ou homologação do protocolo.

A inicialização do motor falha sem um backend persistente S3 configurado.
O backend em memória só funciona com NODE_ENV=test e flag explícita, sem produção.
