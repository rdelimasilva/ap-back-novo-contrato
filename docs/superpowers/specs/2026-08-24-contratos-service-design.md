# contratos-service — Design de implementação (Django)

> Status: aprovado em brainstorming, pronto para plano de implementação.
> Fonte normativa: `SPEC-02-criacao-de-contratos-ap007.md` (anexada como referência completa de contrato/regras de negócio). Este documento cobre **decisões de arquitetura, stack e faseamento**; não repete o conteúdo normativo já coberto lá (catálogo de erros §7, máquina de estados §8, validações C01-C20 §9 etc. — ver a spec original).

## 1. Contexto e decisão de stack

Novo microserviço Python/Django, irmão do `ap-back-optin` (mesma squad, mesmo papel na cadeia CERC — Financiador). Segue **exatamente** as convenções já validadas e implementadas no `ap-back-optin` (ver `docs/superpowers/specs/2026-08-18-optin-service-design.md` daquele repo):

- **Sem Django ORM.** `DATABASES = {}` no settings; acesso a dados via `CloudSqlClient`, wrapper próprio (SQLAlchemy + Cloud SQL Python Connector) com API estilo Supabase/PostgREST (`.table("contrato").insert(...).execute()`). Código **copiado** de `ap-back-optin/optin/shared/cloudsql_client.py`, sem virar dependência de package compartilhado entre os repos.
- **Deploy em Cloud Run**, sem Celery. Assíncrono via **Pub/Sub** (webhook) e **Cloud Scheduler** (jobs periódicos).
- **Sem DRF ViewSets** — function-based views + validação manual.
- **Schema versionado em SQL puro**, numerado (`sql/schema/NN-descricao.sql`), sem framework de migration.
- **Autenticação CERC own-service:** `services/cerc/token_provider.py` **copiado** de `ap-back-optin` (OAuth2 client-credentials, cache em memória por processo, renovação proativa a 80% de `expires_in`, single-flight via lock). **Decisão explícita:** este serviço **não** depende do `ap-back-optin` em runtime — nenhum roteamento de token ou de chamadas via outro serviço. Cada serviço autentica direto na CERC com suas próprias credenciais, mesmo padrão, zero acoplamento entre processos.
- **GCP:** projeto `registradora-506000` (mesmo projeto onde já roda a instância Cloud SQL `app-db` de outro serviço da casa). Deploy em Cloud Run/Pub/Sub dedicados a este serviço.
- **Dev local sem Docker:** esta máquina de desenvolvimento não tem Docker instalado. Diferente do `ap-back-optin` (que usa `docker-compose` + Postgres local), o dev deste serviço conecta **direto num Cloud SQL real** via Cloud SQL Python Connector — o mesmo caminho de conexão que produção usa, só que apontado para uma instância de baixo custo (`db-f1-micro`) usada como ambiente de dev/homolog. `scripts/apply_schema.py` aplica os arquivos de `sql/schema/` nela (substitui o mecanismo de init-script do docker-compose).

### 1.1 Multi-tenancy

Descoberta em 24/08 durante a implementação: o `ap-back-optin` evoluiu (em sessão separada, `docs/superpowers/specs/2026-08-24-multitenancy-design.md` daquele repo, Plan 09) para atender **múltiplos financiadores**, cada um como tenant isolado — isolamento **por banco inteiro** (uma instância Cloud SQL por tenant), não coluna `financiador_id` em tabelas compartilhadas. Confirmado com o usuário: **contratos segue o mesmo modelo.**

- **Identidade do tenant:** `financiador_id` = o próprio `cnpjParticipante` (14 dígitos) — igual à decisão do optin (lá é `cnpjFinanciador`), evita campo duplicado que pode divergir.
- **Config por tenant:** segredo `TENANT_{financiador_id}_CONFIG_CONTRATOS` (JSON) via `shared/secrets.py` (inalterado). **Nome diferente do optin** (`TENANT_{financiador_id}_CONFIG`, sem sufixo) — são segredos separados por serviço, mesmo raciocínio de "cada serviço com suas próprias credenciais CERC" já decidido (§1): evita colidir no Secret Manager (nomes de segredo são únicos por projeto GCP) e evita acoplar o formato desse segredo ao contrato já publicado do optin (§9 do design doc dele). Chaves do JSON: `cloudsql_connection_name`, `cloudsql_db_user`, `cloudsql_db_password`, `cloudsql_db_name`, `cloudsql_ip_type` (opcional, default `PUBLIC` — mantém a melhoria do Plano 03 sobre o padrão do optin), `cerc_client_id`, `cerc_client_secret`. Sem `cerc_cnpj_solicitante`: a SPEC-02 não tem esse conceito — `cnpjParticipante` é sempre o `financiador_id`.
- **`shared/tenant_config.py`:** novo módulo, mesmo formato do optin (cache em memória por processo, sem TTL), mas lendo o segredo com sufixo `_CONTRATOS`.
- **`shared/cloudsql_client.py`:** `get_db()` (singleton único, do Plano 03) vira `get_db(financiador_id: str)`, cache `dict[str, CloudSQLClient]` com lock por tenant (double-checked, mesmo padrão que o optin corrigiu depois de um bug real de vazamento de engine). `LOCAL_DATABASE_URL` é **removido** — único caminho de conexão é por tenant, mesmo em dev. `.upsert()` (adição deste serviço, não existe no optin) é preservado.
- **`services/cerc/token_provider.py`:** `get_cerc_token(financiador_id)`/`invalidate_token(financiador_id)`, cache/lock por tenant, credenciais via `get_tenant_config(financiador_id)`. `CERC_AUTH_URL`/`CERC_API_BASE_URL` continuam env var global (host do ambiente, não varia por tenant).
- **`services/cerc/client.py` (Plano 07, ainda não construído):** todas as funções ganham `financiador_id` como primeiro parâmetro desde o início — sem retrofit necessário, ao contrário do optin (que já tinha `client.py` pronto quando essa decisão chegou).
- **Tenant de dev/teste:** reaproveita o CNPJ de dev do optin, `12345678000199`, apontando pra instância `contratos-db` já provisionada (ela deixa de ser "o banco do serviço" e vira "o banco desse tenant de dev para o serviço de contratos" — nenhuma mudança de infra necessária, só reorganização conceitual + o schema já aplicado nela continua válido).
- **Onboarding de tenant:** manual/scriptado por enquanto (poucos tenants esperados), mesmo raciocínio YAGNI do optin — sem automação de self-service agora.

## 2. Estrutura de pastas

```
contratos/
├── manage.py
├── requirements.txt
├── Dockerfile
├── sql/schema/                   # DDL versionado (01-contratos-schema.sql = subconjunto fase 1 do §11 da SPEC-02)
├── scripts/apply_schema.py       # aplica um arquivo .sql no Cloud SQL real via Cloud SQL Python Connector
├── config/                       # settings.py (DATABASES={}), urls.py, wsgi.py
├── apps/
│   └── contratos/
│       ├── views.py              # API interna (§4/§6) + webhook receptor CERC (§5.2) + push endpoint Pub/Sub
│       ├── urls.py
│       ├── validation.py         # C01-C20 (§9) — ContratoValidator
│       ├── state_machine.py      # máquina de estados do contrato (§8)
│       └── management/commands/  # reconciliar_pendentes, sincronizar_dominio_arranjo
├── services/
│   └── cerc/
│       ├── token_provider.py     # get_cerc_token(financiador_id) — multi-tenant, §1.1
│       └── client.py             # criar_contrato(financiador_id, ...) / atualizar_contrato / ...
└── shared/
    ├── cloudsql_client.py        # get_db(financiador_id) — multi-tenant, §1.1
    ├── tenant_config.py           # get_tenant_config(financiador_id) — TENANT_{id}_CONFIG_CONTRATOS, §1.1
    ├── pubsub_client.py           # publish helper (webhook inbox)
    └── secrets.py                 # copiado de ap-back-optin — leitura via Secret Manager
```

Decisões YAGNI explícitas (mesmo espírito do optin):

- Sem interface/porta formal `CercContratoGateway` — só existe o adapter REST hoje. `services/cerc/client.py` expõe funções diretas.
- Sem camada de domínio separada — regras locais cabem em `apps/contratos/validation.py` e `apps/contratos/state_machine.py`.
- Sem pasta `jobs/` própria — jobs são management commands (`apps/contratos/management/commands/`).
- Sem ingestor AP013 nesta fase (ver §8 abaixo) — não criar código morto para um job que não roda ainda.

## 3. Camada de dados — fase 1

Tabelas do §11 da SPEC-02 usadas por **esta fase**: `contrato`, `contrato_contrato_anterior`, `contrato_parcela`, `contrato_domicilio`, `garantia`, `garantia_ur`, `indicador_consistencia`, `contrato_evento`, `cerc_requisicao`, `webhook_inbox`, `dominio_arranjo`. Virão para `sql/schema/01-contratos-schema.sql`, copiadas da spec quase literalmente (já é DDL Postgres válida), aplicadas na instância real via `scripts/apply_schema.py` (ver §1 — sem Docker nesta máquina).

**Fora da fase 1:** `simulacao_contrato` (entra com `tipoOperacao = S`, fase 2) e `divergencia_ap013` (entra com o ingestor de reconciliação, fase 2/3) — criar essas tabelas agora seria schema sem código que as use.

**Uma instância Cloud SQL por tenant (financiador), por serviço** (§1.1) — não uma instância única compartilhada por todos os financiadores. `contratos-db` (projeto `registradora-506000`) é a instância do **tenant de dev/teste** (`12345678000199`), não "a instância do serviço"; cada tenant real terá a sua própria, provisionada quando o tenant for cadastrado. Distinta da instância `app-db` (que é o tenant de dev do optin) — nenhum dado de outro serviço trafega aqui, mesmo para o mesmo tenant. `dominio_arranjo` é uma cópia local sincronizada por job próprio (`sincronizar_dominio_arranjo`) — não há leitura cross-serviço de outra tabela.

Tipos monetários: `NUMERIC(18,2)` no Postgres, `decimal.Decimal` em Python. **Proibido `float`/`double`** em qualquer campo de valor (requisito explícito da SPEC-02 §13.3, verificado por teste).

## 4. Autenticação, cliente CERC e API interna

- **Token CERC:** `services/cerc/token_provider.py`, copiado do optin. Cache em memória por processo, renovação a 80% de `expires_in`, single-flight via lock. Em `401`, invalida cache e repete a chamada uma única vez.
- **`client_secret`:** Google Secret Manager via `shared/secrets.py` (copiado do optin). Mesmo padrão de `.env` local gitignorado + `.env.example` commitado só com as chaves.
- **Cliente REST CERC** (`services/cerc/client.py`): `criar_contrato`, `atualizar_contrato`, `inativar_contrato`, `baixar_contrato` — todos via `PUT /v15/contratos`, diferenciados por `tipoOperacao` no payload — e `consultar_contrato` (`POST /contrato/consultar`). Cada chamada grava uma linha em `cerc_requisicao` **antes** de interpretar a resposta (mesmo padrão do optin `client.py`).
- **Validação local pré-envio:** `apps/contratos/validation.py` aplica C01-C20 (§9 da SPEC-02) **antes** de qualquer chamada à CERC — inclui a classificação de campos estáticos×dinâmicos (§2.1, erro `107807` evitado localmente).
- **Máquina de estados:** `apps/contratos/state_machine.py` implementa as transições do §8: `ENVIANDO → AGUARDANDO_WEBHOOK → {REGISTRADO | REJEITADO | PENDENTE_CONCILIACAO}`, e as transições pós-registro (`I`/`B`/`P`/`R`). Sub-estado de garantia derivado de `resultadoDistribuicaoOnus`. Evento de domínio `ContratoSubgarantido` emitido quando `resultadoDistribuicaoOnus = 2`.
- **API interna** (`apps/contratos/views.py`, base `/api/v1/contratos`): function-based views. **Fase 1:** criar, atualizar, inativar, baixar, consultar (analítica, `POST /contrato/consultar`). **Fora da fase 1:** simular (`tipoOperacao = S`), resilir (`P`/`R`), consulta sintética (`/v150/contrato/consultar`).
- `Idempotency-Key`/`referenciaExterna` únicos nos `POST` mutantes, com dedupe por índice.

## 5. Ambientes e credenciais

- **Homologação:** mesmos hosts do optin (`CERC_AUTH_URL=https://api.int.cerc.com/oauth/token`, `CERC_API_BASE_URL` de homologação para `/v15/contratos`), credenciais OAuth2 próprias deste serviço (mesmo participante financiador do optin, mas processo e cache de token independentes — nenhum token é compartilhado entre os dois serviços).
- **Produção:** hosts e credenciais a confirmar com a CERC — mesmo risco já registrado na SPEC-02 §12.4.

## 6. Assíncrono, jobs e deploy

- **Webhook CERC → Pub/Sub:** endpoint próprio (`POST /api/v1/webhooks/contrato`), registrado diretamente na CERC para `tipoEvento = contrato` (não passa pelo optin). Handler grava em `webhook_inbox` **antes** de publicar no tópico `contratos-webhook-inbox` (se o publish falhar, job de varredura recupera por `processado_em IS NULL`). Responde `2xx` em <200ms — zero lógica de negócio na rota. Push subscription bate em endpoint próprio, verificado por OIDC.
- **Jobs periódicos** (Cloud Scheduler → endpoint HTTP interno protegido por OIDC):
  - `reconciliar_pendentes` — contratos em `AGUARDANDO_WEBHOOK` há mais de 30 min (configurável) → `PENDENTE_CONCILIACAO` + `POST /contrato/consultar` automático (§8 da SPEC-02); alerta se persistir > 2h.
  - `sincronizar_dominio_arranjo` — diário.
- **Deploy:** Cloud Run + build container, mesmo molde do optin. Mesmo projeto GCP, Cloud SQL/tópico Pub/Sub/serviço Cloud Run dedicados a este serviço.

## 7. Testes e observabilidade

- `pytest` + `pytest-django`.
- Unitários: cada regra C01-C20 com caso positivo e negativo (§13.1), serializador monetário (sem `float`, datas `AAAA-MM-DD`, documentos zero-padded), parser do `207` multi-status, classificador de campos estáticos×dinâmicos (C17), detector de sobreposição de garantias (C13, incluindo `99T`), `TokenProvider` (renovação 80%, single-flight).
- Integração (§13.2, subconjunto fase 1): IT-01 (criar + webhook sucesso), IT-02 (webhook com erro), IT-03 (timeout SLA → `PENDENTE_CONCILIACAO`), IT-04 (webhook duplicado), IT-05 a IT-10 (bloqueios locais C03/C06/C07/C08/C10/C17), IT-12 (baixa), IT-13 (`ContratoSubgarantido`).
- Observabilidade: mesmo padrão do optin (métricas + logging estruturado), alertas na configuração de monitoring do GCP, fora do código.

## 8. Fase 1 vs. fases seguintes

**Fase 1 (este ciclo):** scaffold, schema (subconjunto acima), `CloudSqlClient`/`secrets`/`token_provider` copiados, cliente CERC para `C`/`A`/`I`/`B` + `consultar_contrato`, validações C01-C20, webhook + máquina de estados, job de reconciliação por SLA, consulta analítica.

**Fases seguintes (fora deste ciclo, não iniciar código):**
- Simulação (`tipoOperacao = S`, tabela `simulacao_contrato`, TTL 30 dias).
- Resilição parcial/total (`P`/`R`).
- Consulta sintética por credenciadora (`POST /v150/contrato/consultar`, tabela/campos de `indSobrecolateral`).
- Ingestor de reconciliação por arquivo AP013/A/B/C (SFTP, 4 layouts, tabela `divergencia_ap013`) — job independente, não bloqueante para a API.
- `tipoEfeito = 8` (promessa de cessão) e a matriz modalidade×efeito (§12.1/§12.2 da SPEC-02) — pendências de confirmação com a CERC; até lá, `8` é rejeitado localmente com mensagem explícita e `107805` é apenas mapeado para mensagem clara ao usuário.

## 9. Riscos e pendências (herdados da SPEC-02 §12)

1. `tipoEfeito = 8` (promessa de cessão) aceito no arquivo AP013/AP013B mas fora do enum publicado da API v1.5 — confirmar com a CERC antes de habilitar.
2. Matriz modalidade×tipo de efeito (erro `107805`) não publicada — mapear quando a CERC fornecer.
3. Rate limits e tamanho máximo de lote de `PUT /v15/contratos` não publicados — começar com lotes de 100 e ajustar após certificação.
4. Grade horária da janela de processamento online não confirmada.
5. `107822` (limite de ativos alcançados) sem valor numérico publicado — instrumentar e descobrir empiricamente em homologação.
6. Hosts e credenciais de produção da CERC ainda não confirmados (só homologação).
