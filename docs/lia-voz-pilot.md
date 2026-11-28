# Eduarda Voz - piloto isolado (fase 1)

Identidade pública: Eduarda (treinamento comercial V2.2 preservado). Os caminhos,
variáveis, estados de fila e identificadores técnicos `lia_voz`, `LIA_VOZ_*` e
`/voice/lia-voz` permanecem compatíveis com a instalação existente.

SDRIA de ligacoes da Sulnet. O modulo novo nao altera WhatsApp, campanhas,
banco de dados, SIP, rotas HTTP, credenciais ou deploys existentes.

## Implementado nesta fase

- Sessao Realtime GA, modelo configuravel (padrao gpt-realtime-2.1-mini),
  voz marin, audio PCM16 mono 24 kHz e VAD no servidor.
- Transporte WebSocket assincrono, chave recebida do chamador, opt-in
  explicito, limite de duracao e sem reconexao automatica.
- Fila de audio limitada a 15 segundos, interrupcao de reproducao,
  descarte de deltas atrasados e truncamento pela quantidade de audio
  entregue ao consumidor. A medida nao comprova audicao no aparelho.
- Prompt exclusivo de homologacao, sem dados de clientes ou ferramentas.
- Testes offline com socket simulado, sem custo de API ou telefonia.

## Teste local offline

    python -m pytest -q tests/test_lia_voz_realtime.py

A dependencia opcional e instalada apenas no ambiente de homologacao.
Acesso real a OpenAI, permissao do modelo e qualidade de voz precisam
ser verificados com uma chave de teste aprovada.

## Proxima fase (nao incluida neste PR)

1. Adaptar AudioMediaPort do PJSUA2, com resampling validado se necessario,
   transferencia segura entre threads e medicao de latencia.
2. Criar tela e teste individual administrativo, com numero de teste
   autorizado, protecao CSRF/RBAC e bloqueios existentes de telefonia.
3. Persistir memoria, treinamento e transcricoes em estruturas exclusivas
   de voz, com retencao, segregacao de acesso e aprovacao de aprendizado.
4. Conectar ferramentas CRM validadas, opt-out imediato, agendamento
   idempotente e transferencia assistida. Nao inventar viabilidade,
   preco, cobertura ou aprovacao de credito.
5. Homologar chamadas Native em ambiente controlado. Distinguir aceite
   de SIP REFER de atendimento humano confirmado. Sem ativar campanhas
   automaticamente; exigir validacao de operacao e conformidade.

Referencias:
- https://developers.openai.com/api/docs/guides/realtime-conversations
- https://developers.openai.com/api/reference/resources/realtime/client-events
- https://developers.openai.com/api/reference/resources/realtime/server-events

## Fase 2: adaptador nativo isolado

Adicionado MediaBridge e porta AudioMediaPort (24 kHz, mono, 16 bits, 20 ms),
com fila de entrada limitada a 500 ms e silencio na saida apos fechamento.
PJSIP faz a conversao do formato da conferencia. Os callbacks nao usam rede.
O protocolo Realtime sincroniza leitura e interrupcao com a thread de midia.
O runner encerra as tarefas WebSocket em caso de falha ou fechamento.
O adaptador ainda nao e chamado por nenhum worker de producao.
Testes offline com SWIG simulado: python -m pytest -q tests/test_lia_voz_media.py.
Proximo: testar com wheel PJSUA2 2.15.1.post1 e RTP real de bancada,
validar cancelamento, desconexao e autorizacao antes de ligar pela Native.


## Fase 3: tela e orquestracao de teste individual

Tela administrativa /voice/lia-voz, acessivel pelo dashboard de voz.
POST com CSRF, ADMIN, confirmacao explicita, destino unico permitido,
blacklist, conta Native 1804, janela de solicitacao de 90 s e limites de
3 testes/hora e intervalo de 3 minutos. A fila usa estados LIA_ distintos
em voice_test_calls, sem nova migracao, sem campanhas ou chamadas automaticas.

Apenas o motor isolado processa o teste quando todas as flags abaixo estiverem
configuradas. O handshake Realtime ocorre antes do SIP INVITE. A ponte de
audio conecta somente apos SIP confirmado e RTP ativo; limite 90 s de conversa.
O encerramento cancela tarefas de rede, desconecta a porta nativa e encerra SIP.
Sem CRM, transferencia, transcricao persistida, memoria permanente ou ferramenta
de opt-out nesta fase: somente teste interno autorizado, nao clientes.

**Nao ativar em producao.** A mesma conta SIP pode estar em uso por outros
workers, projetos Railway ou aparelhos. O flag de exclusividade e uma
confirmacao operacional, nao uma trava distribuida entre PABXs/projetos.
Usar ambiente de homologacao sem campanhas e sem outro dispositivo/worker
registrado nessa conta; validar a janela com a operacao/telefonia.
Apenas depois dos testes nativos, configurar explicitamente no web e no
motor isolado:
- LIA_VOZ_TEST_ENABLED=true
- LIA_VOZ_TEST_EXCLUSIVE_SIP=true
- LIA_VOZ_TEST_NATIVE_PREFIX (uma das cinco linhas)
No motor, usar docker/lia-voz-pilot.Dockerfile, uma replica, habilitar
VOICE_ENGINE_WORKER_ENABLED, manter VOICE_CAMPAIGN_OUTBOUND_ENABLED e
VOICE_TRANSFER_TESTS_ENABLED desabilitados, configurar LIA_VOZ_OPENAI_API_KEY
via segredo separado e liberar a trava geral apenas no ambiente de teste.

Testes adicionados para handshake, revogacao de autorizacao, registro SIP e
encerramento remoto simulado. O CI de imagem nativa tambem executa o benchmark
PCM entre duas portas PJSUA2 sem SIP, rede externa ou OpenAI.

Nenhum segredo ou flag de ativacao foi configurado neste PR. Os contadores de
quadros e SIP 200 nao comprovam que a pessoa ouviu e respondeu; validar no
aparelho. Se houver falha de rede ou midia, nao repetir automaticamente.

## Laboratorio Railway independente (sem telefone)

Imagem docker/lia-voz-lab.Dockerfile, processo de execucao unica e politica
NEVER. O container inclui somente adaptadores de audio e os testes; nao possui
worker, modelos de banco, cliente CRM ou codigo de discagem. Nao adicionar
DATABASE_URL, credenciais SIP, dominio publico ou referencias de producao.
O teste PCM executa automaticamente; a conexao com a OpenAI fica desabilitada
por padrao. Para o teste sintetico separado, configurar uma chave de projeto
especifica para homologacao em LIA_VOZ_OPENAI_API_KEY e ativar explicitamente
LIA_VOZ_LAB_REALTIME_ENABLED=true. O teste solicita somente a saudacao
sintetica, registra contadores e tempos, sem transcricao ou audio persistido.
A saida PASS nao equivale a homologacao de uma ligacao real.


## Modo de fila compartilhada (opt-in)

A nova opcao LIA_VOZ_TEST_SHARED_ENGINE=true permite enfileirar um teste
individual sem alterar o status das campanhas. Os workers nativo e legado
adquirem a mesma trava advisory PostgreSQL antes de reservar e executar
qualquer chamada/registro. A Eduarda Voz tem prioridade na proxima vaga,
sem interromper a chamada atual. O teste expira em 10 minutos e permanece
limitado a 90 segundos apos atendimento, sem rediscagem. O heartbeat
informa readiness do worker sem enviar chave para o web.

Antes de ativar: publicar e verificar ambos os workers, a migracao 0044,
a interface e o pacote websockets. Confirmar ausencia de outro dispositivo
ou projeto usando o ramal 1804 fora deste banco. Usar PostgreSQL direto
ou pool em modo session; advisory locks nao sao seguros em pool de
transacoes. Manter uma replica do motor. O flag EXCLUSIVE_SIP continua
exigindo essa confirmacao externa. Definir a mesma configuracao de teste
em web e worker (habilitacao, compartilhado, exclusividade, destino
autorizado e techprefix); a chave Realtime fica apenas no worker.
O flag nao confirma por si so acesso real a API. Primeiro validar
a saudacao sintetica no laboratorio. Nenhuma flag foi ativada neste PR.


## Atualizacao: destino escolhido em cada teste manual

A tela administrativa permite escolher um telefone brasileiro valido com
DDD para cada teste com ou sem IA, sem cadastro previo de destino. A antiga
variavel LIA_VOZ_TEST_PHONE deixou de ser utilizada. A confirmacao explicita
do administrador continua obrigatoria: apenas numeros proprios ou autorizados
para o teste. O worker confere o destino e a conta persistidos, usuario ativo
ADMIN, blacklist global, estado do motor e trava de telefonia antes e durante
a chamada. Os testes continuam individuais, sem rediscagem automatica.
A rota por techprefix continua exclusiva da conta Native 1804; demais contas
ativas usam a configuracao de discagem cadastrada. Os trechos historicos
das fases anteriores sobre numero fixo nao se aplicam a esta versao.
