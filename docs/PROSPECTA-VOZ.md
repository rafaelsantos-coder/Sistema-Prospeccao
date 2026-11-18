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

## Verificação de conexão SIP sem ligação

O ADMIN pode acessar **Contas do discador > Testar ligação > Verificar conexão SIP**.
Essa ação cria um registro de verificação do tipo `SIP_ONLY` (sem telefone de destino),
e o worker tenta `REGISTER` no PABX para obter uma resposta autenticada (SIP 200).
O estado final é `SIP_VALIDADO`, exibido como **Autenticado no PABX**. Esse sucesso
é pontual, não equivale a conexão permanente, qualidade de áudio, disponibilidade
dos vendedores ou permissão para campanhas em massa. Em seguida o cliente tenta
`REGISTER Expires: 0` para finalizar a sessão temporária. Se o PABX permite apenas
uma sessão para esse usuário, até uma verificação pode desalojar o aparelho físico;
preferir ramal dedicado à discagem.

**Nunca** converter um `SIP_VALIDADO` isolado em estado automático
`EM_EXECUCAO` de campanha. É indispensável verificar discagem, transferência
ao ramal do grupo, disponibilidade real dos vendedores e controles de contato antes
de desenvolver/ativar o worker de campanhas de voz.

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


## Motor de chamadas PJSUA2: controles e operacao

O Prospecta VOZ utiliza a central SIP existente, sem criar Asterisk. O novo serviço independente voice-engine-worker usa PJSUA2 e Python 3.12 para registro SIP, chamada com RTP/RTCP, audio WAV, DTMF e transferencias SIP REFER/NOTIFY. Web/WhatsApp continuam em Python 3.14 e nao sao alterados.

Configuracao pelo ADMIN:
- Conta SIP: servidor DNS/IP, porta SIP, proxy e porta, usuario, senha criptografada, UDP/TCP/TLS, dominio de discagem, prefixo, remocao opcional do codigo 55 e identificador de chamada. O identificador real e controlado pela operadora.
- Grupos: destino do ramal na central e teste real de transferencia para numero autorizado. Somente resultado REFER final bem-sucedido do mesmo par conta/grupo certifica transferencia. Alterar uma configuracao invalida certificacoes anteriores.
- Campanhas: checklist, base legal confirmada pelo ADMIN, iniciar/pausar, parada geral e status/eventos reais. A interface nao pode iniciar uma campanha apenas porque a conta esta ativa no cadastro.

Bloqueios preventivos:
- Parada geral DESLIGADA por padrao, exigindo liberacao ADMIN; flags de worker independentes e heartbeat recente.
- Chamadas progressivas, no maximo uma por worker, ate 30/dia por campanha, intervalo minimo de 60 s, uma tentativa por contato, de segunda a sexta, 09h-18h fuso Sao Paulo e janela configurada.
- Verificacao fail-closed da blacklist global antes de selecionar contatos e imediatamente antes do INVITE SIP.
- Sem nova discagem apos erro/crash de resultado incerto; parada geral e pausa interrompem chamadas.
- Exclusivo ADMIN, controles CSRF e auditoria, sem ativacao de IA nem filas WhatsApp.

Deploy em um servico Railway separado com Dockerfile docker/voice-engine.Dockerfile e uma replica. Variaveis: DATABASE_URL (referencia Postgres), DATA_ENCRYPTION_KEY e SECRET_KEY (referencias web), APP_ENV=production, VOICE_ENGINE_WORKER_ENABLED=true, VOICE_CAMPAIGN_OUTBOUND_ENABLED=true e VOICE_TRANSFER_TESTS_ENABLED=true. As duas ultimas variaveis liberam a capacidade do worker, mas nenhuma chamada ocorre sem a parada geral e uma campanha/teste autorizados no painel. Para audios: MEDIA_S3_ENDPOINT, MEDIA_S3_BUCKET, MEDIA_S3_REGION, MEDIA_S3_ACCESS_KEY_ID, MEDIA_S3_SECRET_ACCESS_KEY conforme web.

Dependencias que nao podem ser inferidas pelo software: IP/DNS e acesso externo ao PABX, credenciais, limites e politica do tronco, autorizacao para ligacoes externas, NAT e RTP, suporte REFER/NOTIFY, disponibilidade real dos vendedores. SIP autenticado nao garante audio nem transferencia. Em testes observados em 18/11/2026 o dominio sip.sulnet.com.br respondeu NXDOMAIN a partir do Railway; o ADMIN precisa cadastrar um endereco DNS/IP efetivamente acessivel ao servidor.


### Chamada individual, sem grupo

Em Contas do discador > Testar ligação, o administrador também dispõe de Fazer ligação
PJSUA2 sem grupo, que registra a conta e realiza uma única chamada para um número
autorizado, com tentativa de negociação RTP e encerramento após o teste. A solicitação
recebe o status PENDENTE_PJSIP, consumido apenas pelo voice-engine-worker (nunca pelo
worker SIP antigo nem pela fila da campanha). O admin deve habilitar a parada geral
e o worker de testes. Ao final, o histórico informa códigos SIP e se houve mídia
RTP ativa; isso não comprova o que o destinatário ouviu sem verificação humana.
A blacklist é consultada antes de solicitar e antes de executar a chamada, e
uma falha nunca gera nova tentativa automática.


## Campanha de áudio sem transferência (produção)

O administrador pode selecionar **Não transferir para grupo — reproduzir áudio e encerrar**
no cadastro de campanhas. Essa opção exige modalidade **AUDIO**; transferência
**DIRECT** continua exigindo um grupo ativo. A migration
`0023_voice_no_transfer` torna `voice_campaigns.group_id` anulável e preserva
campanhas preexistentes. Quando não há grupo:

- O checklist **não exige** teste REFER nem grupo cadastrado, mas mantém verificação
  real de SIP, motor ativo, áudio WAV, autorização ADMIN, base legal, contatos,
  blacklist, janela e limites aplicáveis.
- Após atendimento confirmado e mídia RTP disponível, o motor envia o WAV completo,
  espera o tempo de reprodução e encerra a chamada por BYE, sem solicitar tecla DTMF,
  sem transferir e sem enviar SIP REFER.
- A duração máxima não é artificial; a chamada tem timeout dinâmico calculado pela
  duração do arquivo e ainda depende da capacidade real do armazenamento.
- Status finais: `AUDIO_CONCLUIDO` significa reprodução concluída no motor
  (não confirma que a pessoa ouviu o conteúdo); `AUDIO_INTERROMPIDO` significa
  desligamento antecipado, e `FALHA_AUDIO` registra ausência de mídia RTP.
- Os indicadores de execução mostram **Áudios concluídos** separadamente das
  ligações atendidas, falhas ou transferidas.

Campanhas existentes que selecionam grupos mantêm o fluxo anterior de DIRECT
ou AUDIO com tecla DTMF e transferência validada. A opção sem grupo jamais habilita
a discagem automaticamente: a parada geral e o botão de iniciar continuam obrigatórios.


## Gestão de campanhas — editar, excluir e restaurar

- **Editar:** disponível para campanhas `RASCUNHO`, `PRONTA`, `EM_HOMOLOGACAO`, `PAUSADA` e `CONCLUIDA`, desde que não exista ligação em andamento. Permite alterar nome, conta SIP, grupo ou ausência de grupo, modalidade, tecla DTMF, limite diário, intervalo, horários e formato SIP nacional/E.164. Salvar não dispara chamada e não reinicia contatos já processados.
- **Campanha pausada:** permite importar novos contatos por CSV ou substituir o áudio WAV. A importação preserva eventos antigos e não rediscagem automaticamente números já processados. Novos destinatários ou troca de áudio exigem nova confirmação da base legal.
- **Excluir:** somente com campanha parada e nenhuma chamada em andamento. Por motivos de auditoria, é uma **exclusão lógica** (status `EXCLUIDA`), que a remove do painel operacional e dos contadores ativos sem apagar contatos, tentativas, eventos SIP ou histórico de ações. Exige digitar o nome exato e CSRF.
- **Restaurar:** em Campanhas → Excluídas, somente pelo administrador. Volta para `RASCUNHO` ou `PAUSADA` e invalida a confirmação anterior da base legal, exigindo início manual e novos pré-requisitos.
- As rotas de operação continuam exclusivas para o perfil `ADMIN`; não existe discagem automática ao editar, excluir ou restaurar.


## Produção — transferência direta sem homologação prévia (18/11/2026)

A partir desta versão, o cadastro de uma campanha com grupo/ramal ativo autoriza o
motor a **tentar a transferência SIP REFER durante a própria ligação**. Não é mais
necessário realizar, aguardar ou renovar um teste REFER anterior para iniciar a
campanha. O painel de grupos não apresenta mais a etapa de homologação obrigatória.
Qualquer referência antiga neste documento a uma certificação prévia de REFER
como requisito de início está obsoleta.

Continua obrigatório: administração autorizada; conta SIP cadastrada, ativa e
com autenticação exigida pelas verificações de conexão existentes; motor e worker
online; grupo de destino ativo quando houver transferência; origem autorizada,
blacklist, base legal, limites operacionais, janela de discagem e parada geral.
O motor executa o REGISTER/INVITE/REFER reais usando as configurações cadastradas
e registra o código SIP e o resultado de cada chamada, inclusive
`TRANSFERIDA`, `FALHA_TRANSFERENCIA` e timeout. **Sem pré-teste não significa
transferência garantida**: o resultado depende do PABX, do grupo e da resposta do
destino. Falhas não são registradas como sucesso.


## Interface compacta de campanhas — carregamento sob demanda

A listagem `/voice/campaigns` passa a exibir **uma linha por campanha**,
com nome, número, quantidade de contatos e status. A seleção da linha expande
os detalhes **sem navegar para outra página**. Somente uma campanha fica
aberta por vez.

- Os dados de checklist, histórico SIP, tentativas, indicadores e o número
  de discagem são buscados **somente ao abrir** aquela campanha, por
  `GET /voice/campaigns/{id}/details` (somente ADMIN, sem alteração de dados).
- Depois de aberta, a atualização automática usa
  `GET /voice/campaigns/status?campaign_id={id}` a cada dez segundos,
  sem consultar os relatórios de todas as outras campanhas.
- Editar, excluir, restaurar, importar CSV, atualizar WAV e iniciar/pausar
  mantêm os mesmos controles do backend. Após uma ação de formulário, a
  listagem reabre a campanha selecionada no mesmo navegador, quando presente.
- As campanhas começam recolhidas, inclusive na página de excluídas; esta
  alteração é exclusivamente de interface e **não inicia nem reinicia discagem**.


## Reenviar campanha (completa / somente não atendidos)

Campanhas **PAUSADAS** ou **CONCLUÍDAS**, sem chamadas em andamento, oferecem
duas opções dentro da campanha expandida:

- **Reenviar campanha:** seleciona todos os telefones válidos e não bloqueados
  da campanha original, inclusive atendidos e contatos que ainda estavam
  pendentes. A tela alerta explicitamente que números atendidos serão contatados
  outra vez.
- **Reenviar somente não atendidos:** seleciona apenas números com tentativa
  registrada e resultado real sem atendimento (SEM_ATENDIMENTO, SEM_RESPOSTA,
  OCUPADO, TIMEOUT sem evidência de resposta ou FALHOU após SIP TOCANDO).
  Exclui números com **qualquer** evidência histórica de atendimento nas
  campanhas de voz, inclusive SIP ATENDIDA, REFER, DTMF, áudio, falha de
  transferência e falha de áudio após atendimento. Não seleciona resultados
  indeterminados, falhas antes de tocar ou contatos ainda não tentados.

Os dois modos respeitam a blacklist global e opt-outs legados antes de
gerar contatos e **novamente no worker imediatamente antes de cada INVITE**.
Não há desbloqueio, reinício automático ou reimportação de contatos.

O usuário ADMIN vê uma **prévia de quantidade e exclusões**, confirma o
nome exato da campanha e assinala declaração de base legal e autorização para
a nova rodada. Só então uma **nova campanha** é criada com mesma conta SIP,
grupo, modalidade, áudio, limites, janela e formato de discagem; novos
destinatários iniciam em PENDENTE. O histórico antigo permanece intacto,
e o monitoramento e a auditoria da nova rodada são independentes. O motor
PJSUA2 passa a executar as ligações somente enquanto os bloqueios usuais
(estágio, conta, horário, limite diário, parada geral) estiverem liberados.
Submissões duplicadas em até dois minutos são rejeitadas.
