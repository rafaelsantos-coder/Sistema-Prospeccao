# Prospecta VOZ — Fase 1: preparação e homologação

## Estado desta entrega

O recurso foi adicionado em uma branch de desenvolvimento, sem ativar chamadas.
O menu **Prospecta VOZ** permite:
- Cadastrar contas SIP, com senha criptografada por `app.services.secret_store`.
- Cadastrar um grupo de destino que **já exista no PABX**, com seus ramais informativos.
- Criar campanhas independentes em **AUDIO** (WAV PCM mono 16-bit, 8 ou 16 kHz, 0,5–60 s, digite 1) e **DIRECT** (transferência sem áudio).
- Importar CSV de até 1 MB e 5.000 linhas, com deduplicação por campanha e exclusão de leads que solicitaram não contato.
- Revisar campanhas sem iniciar ligações.

**A configuração de campanhas não executa ligações.** O teste SIP é um serviço independente para verificações pontuais autorizadas; transferências reais seguem bloqueadas até homologação.
O estado `EM_HOMOLOGACAO` nunca dispara chamada. Nenhuma credencial real foi inserida no repositório ou na base de produção.

## Diagnóstico de uma chamada SIP autorizada

Em **Prospecta VOZ > Contas do discador > Testar ligação**, o ADMIN informa somente um número autorizado com DDD e confirma consentimento para receber **uma chamada de teste**. O sistema verifica se o agente de testes SIP está online antes de enfileirar a solicitação. Não existem testes automáticos recorrentes.

O serviço dedicado `python -m app.workers.voice_test_worker`, habilitado somente com `VOICE_TEST_WORKER_ENABLED=true`, consulta a tabela de testes e autentica a conta via **SIP UDP REGISTER** (desafio Digest MD5/SHA-256 suportado). Depois tenta **um único SIP INVITE** para o número informado; encerra após resposta SIP 180/200 ou timeout, mediante CANCEL ou BYE. **SIP 180 indica sinalização de toque pelo PABX, não confirmação de toque físico; SIP 200 indica conexão da chamada, não qualidade de áudio.** O diagnóstico não oferece RTP bidirecional, não toca gravações e não transfere para grupos.

A página distingue: PENDENTE, REGISTRANDO, REGISTRADO, CHAMANDO, TOCANDO, TOCOU, ATENDIDA, OCUPADO, SEM_RESPOSTA, FALHOU e EXPIRADO. O teste grava os códigos SIP do registro e convite, mensagens seguras e o histórico do número. Os testes não contam como campanhas.

Segurança:
- Exclusivo perfil ADMIN, com token CSRF e auditoria de solicitação; não armazenar senha em logs nem parâmetros da URL.
- Somente destinos telefônicos brasileiros com DDD e autorização expressa, até **três testes por conta/hora**, um ativo por vez e intervalo de três minutos por destino.
- O botão fica indisponível quando o agente não registra heartbeat, quando a conta é pausada ou se o transporte não é UDP.
- Pedidos com mais de 90 segundos são expirados, sem discagem tardia; não há retentativas automáticas após falha.
- **Aviso SIP:** o registro temporário da mesma conta em outro dispositivo pode desalojar uma sessão, dependendo da política do PABX; homologar primeiro com ramal exclusivo de testes.
- Para produção do agente, configurar em serviço isolado Railway (uma réplica) `DATABASE_URL=${{Postgres.DATABASE_URL}}`, `DATA_ENCRYPTION_KEY=${{web.DATA_ENCRYPTION_KEY}}`, `SECRET_KEY=${{web.SECRET_KEY}}`, `VOICE_TEST_WORKER_ENABLED=true`, `APP_ENV=production`. Iniciar com `python -m app.workers.voice_test_worker`. Conexão SIP UDP (porta padrão 5060) e NAT dependem da rede e da central; falhas aparecem como falha real, nunca como teste bem-sucedido simulado.
- O recurso de discagem de campanhas continua **desativado** até a homologação de mídia RTP e transferências PABX.

## Navegação administrativa modular

Ao clicar em **Prospecta VOZ**, o administrador acessa o **dashboard** em /voice. O menu lateral expande os submenus independentes:

- **Dashboard VOZ** (/voice): indicadores de contas, grupos, campanhas e destinatários. O status de registro SIP é **Não verificado** enquanto não houver um cliente SIP e retorno real do PABX; **Ativa/Pausada** é somente um controle de seleção de contas, não prova conectividade.
- **Contas do discador** (/voice/accounts): lista e cadastro de contas SIP; edição, troca opcional de senha criptografada, pausa/reativação e exclusão com confirmação por nome.
- **Ramais e grupos** (/voice/groups): lista, cadastro, edição de ramais de destino e membros, pausa/reativação, exclusão com confirmação.
- **Nova campanha** (/voice/campaigns/new): seleção da conta, grupo e modalidade AUDIO/DIRECT, janela e limites.
- **Campanhas e preparação** (/voice/campaigns): contatos CSV, gravação WAV, revisão e status dos rascunhos.

Exclusão de conta ou grupo vinculado a qualquer campanha é bloqueada para preservar histórico e integridade. Alterações exigem sessão de ADMIN e CSRF; dados de senha nunca aparecem em formulários preenchidos ou listagens. Nenhuma dessas telas inicia ligação: integração SIP depende de homologação.

## Permissões e responsabilidades

**Somente o perfil `ADMIN` acessa o módulo Prospecta VOZ.** O sistema aplica o bloqueio no backend de todos os endpoints GET/POST; ocultar o menu é apenas uma melhoria de interface, não o mecanismo de segurança.

| Atividade | ADMIN | GESTOR | SUPERVISOR | CONSULTOR / VENDEDOR |
|---|:---:|:---:|:---:|:---:|
| Acessar o painel Prospecta VOZ | Sim | Não | Não | Não |
| Cadastrar / editar credenciais SIP e ramais | Sim | Não | Não | Não |
| Definir grupos e destinos no PABX | Sim | Não | Não | Não |
| Criar campanhas e escolher AUDIO/DIRECT | Sim | Não | Não | Não |
| Importar listas e carregar áudio | Sim | Não | Não | Não |
| Validar, aprovar, iniciar, pausar ou encerrar campanhas (quando implementado) | Sim | Não | Não | Não |
| Consultar resultados do módulo de voz (quando implementado) | Sim | Não | Não | Não |
| Receber ligações transferidas no ramal telefônico do vendedor | Não depende do painel | Não depende do painel | Não depende do painel | Sim |

**Separação operacional:** o administrador configura a conta de origem, a fila e as regras; o serviço discador (a implementar) executará automaticamente as instruções autorizadas; o PABX realizará o encaminhamento da chamada; os vendedores recebem a ligação no seu próprio ramal, sem permissões de alterar SIP, grupos ou campanhas. O atendente não precisa estar logado no módulo VOZ para receber a ligação na central telefônica.

Não conceder acesso de configuração ao `GESTOR` ou `SUPERVISOR` por herança de permissões de outros módulos. Novas rotas e APIs do Prospecta VOZ também devem aplicar o mesmo controle no servidor antes de operar sobre os dados.

## Homologação necessária antes de habilitar voz real

1. Rotacionar a senha da conta SIP que foi compartilhada em conversa e recadastrá-la por canal protegido.
2. Confirmar com o administrador do PABX se a conta permite originação externa, transferências SIP REFER e encaminhamento a grupo; obter ramal de destino real.
3. Provisionar cliente PJSIP isolado, com SIP/RTP e rede adequada para o PABX, sem confundir a API web Railway com endpoint RTP/UDP.
4. Validar `REGISTER`, `INVITE`, áudio bidirecional, codecs, `REFER` e confirmação final de atendimento pelo vendedor com chamadas autorizadas.
5. Criar um worker dedicado com idempotência, consulta à base de supressão completa (incluindo Não Me Perturbe), janela legal, capacidade de atendimento e bloqueio de discagem quando vendedores indisponíveis.
6. Implementar campanha aprovável somente após verificação documentada de base legal, finalidade e fonte do contato; incluir pausa global (independente de WhatsApp), controles de volume/chamadas curtas e kill switch.
7. Implementar eventos SIP idempotentes, logs sem segredo, respostas de URA DTMF e tratamento de não atendidas, caixa postal e falhas.
8. Testar em staging antes de qualquer merge ou deploy de produção.

## Arquitetura pretendida

```
Prospecta Web (Railway) — campanhas, leads, DNC, auditoria
             |
          PostgreSQL
             |
    Voice worker isolado (não implantado)
             |
          PJSIP SIP/RTP
             |
       PABX existente
             |
       Grupo de vendedores
```

Importante: as filas de voz **não reutilizam** o roteamento Z-API/WhatsApp e o sistema atual segue com o estado de pausa global de disparos existente. Não assumir que `200 OK` prova atendimento humano, nem que `REFER` aceito prova atendimento pelo vendedor.
