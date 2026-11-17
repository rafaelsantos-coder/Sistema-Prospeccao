# Prospecta VOZ — Fase 1: preparação e homologação

## Estado desta entrega

O recurso foi adicionado em uma branch de desenvolvimento, sem ativar chamadas.
O menu **Prospecta VOZ** permite:
- Cadastrar contas SIP, com senha criptografada por `app.services.secret_store`.
- Cadastrar um grupo de destino que **já exista no PABX**, com seus ramais informativos.
- Criar campanhas independentes em **AUDIO** (WAV PCM mono 16-bit, 8 ou 16 kHz, 0,5–60 s, digite 1) e **DIRECT** (transferência sem áudio).
- Importar CSV de até 1 MB e 5.000 linhas, com deduplicação por campanha e exclusão de leads que solicitaram não contato.
- Revisar campanhas sem iniciar ligações.

**Não existe ainda cliente SIP em execução, registro autenticado, teste de conexão, worker que disque ou transferência real.**
O estado `AGUARDANDO_HOMOLOGACAO` nunca dispara chamada. Nenhuma credencial real foi inserida no repositório ou na base de produção.

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
