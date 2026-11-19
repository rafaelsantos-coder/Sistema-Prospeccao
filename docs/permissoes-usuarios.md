# Controle de acesso por usuário — Sulnet Prospecta

## O que mudou

Em **Configurações > Usuários e acessos** a criação e edição apresentam uma matriz de permissões por menu e ação. É possível deixar, por exemplo, um consultor apenas com Conversas e cadastro de CRM, ou conceder-lhe a criação de campanhas VOZ sem acesso às contas SIP.

### Diferença entre perfil e permissão

- **Perfil** (CONSULTOR / SUPERVISOR / GESTOR / ADMIN) continua definindo o limite de privilégios. Permissões não elevam o usuário a administrador, não concedem acesso a dados de outros vendedores e não alteram as filas associadas.
- **Permissão** autoriza acessar um menu e, separadamente, executar ações. Uma ação só funciona com o acesso ao menu-pai.
- **Usuários existentes** continuam com o padrão histórico do perfil (campo access_permissions NULL). Ao editar e salvar a matriz, passam a ter controle individual.
- **Administradores** mantêm todos os acessos, não podem restringir a si mesmos na UI nem perder controle acidentalmente.
- **Gestores** podem conceder somente permissões que possuem e funções compatíveis com o perfil do destinatário; não podem editar administradores/gestores.

### Categorias

Visão geral; Leads e CRM; Conversas/filas WhatsApp, incluindo registrar oportunidade, consultar CPF, contestar, responder e transferir; Prospecção; Campanhas WhatsApp; Disparos WhatsApp; Prospecta VOZ (separando campanhas de contas SIP/ramais); IA; Relatórios; Mapa de crédito; Blacklist; Configurações.

### Segurança

1. A matriz é salva como JSON no banco; todos os POSTs de alteração exigem sessão com perfil adequado e token CSRF.
2. O servidor verifica os direitos a cada requisição autenticada, por módulo e método/rota, independentemente de o menu estar visível.
3. Consultores recebem no máximo as conversas e oportunidades autorizadas pelas regras de escopo e associação de fila existentes.
4. Cadastro de usuários, contas SIP e gestão das configurações sensíveis continuam protegidos por limites de perfil.
5. A IA não é iniciada nem reativada pela concessão de acesso à seção de IA. Os bloqueios de atendimento estabelecidos anteriormente permanecem.
6. O campo de permissões de usuários legados não é preenchido por migration: mantêm os acessos anteriores até serem editados.

### Exemplos práticos

- **Consultor WhatsApp:** Conversas: visualizar/responder/transferir; CRM: registrar/CPF/contestar. Sem disparos, sem campanhas VOZ e sem IA.
- **Operador de disparos:** Visão geral; Campanhas: visualizar; Disparos: visualizar/gerenciar, concedidos por administrador ou gestor autorizado.
- **Operador do discador:** VOZ: visualizar; VOZ: criar/operar campanhas, sem permissão para cadastrar conta SIP ou editar ramais.
- **Gestor completo:** permissões históricas de gestor, personalizáveis conforme suas atribuições; as funções não concedidas deixam de aparecer no menu e bloqueiam URL direta.

### Limites importantes

As caixas de seleção controlam **módulo e ação**; acesso a uma fila específica depende adicionalmente de membros, papéis e regras de visibilidade das filas, configuráveis no menu de filas. A delegação não cria vínculo de fila automaticamente. A implementação não modifica nenhuma regra de envio, IA ou telefonia.
