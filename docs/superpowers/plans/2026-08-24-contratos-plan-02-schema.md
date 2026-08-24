# contratos-service — Plan 02: Local Postgres + Fase-1 Schema — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local Postgres dev environment pre-loaded with the fase-1 subset of the SPEC-02 contrato schema, so every later plan has real tables to read/write against.

**Architecture:** Plain versioned SQL in `docker/initdb/`, loaded automatically by the official `postgres` Docker image's init-script mechanism. No migration framework (matches house convention — see design doc §2/§3, mirroring `ap-back-optin`).

**Tech Stack:** Docker Compose, Postgres 16.

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§3). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §11 (Modelo de dados). Series: plan 2 of ~10.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout).

## Global Constraints

- Money columns are `NUMERIC(18,2)`; **never** `float`/`double` (SPEC-02 §11, §13.3).
- Tables excluded from this schema on purpose: `simulacao_contrato` (entra com `tipoOperacao = S`, fase 2) e `divergencia_ap013` (entra com o ingestor de reconciliação AP013, fase 2/3) — criar essas tabelas agora seria schema sem código que as use (design doc §3).
- Local Postgres runs on port `5434` (not `5433`) so it never collides with `ap-back-optin`'s docker-compose on the same machine (established in Plan 01's `.env.example`).

---

### Task 1: Local Postgres + fase-1 contrato schema (DDL)

**Files:**
- Create: `contratos/docker-compose.yml`
- Create: `contratos/docker/initdb/01-contratos-schema.sql`

**Interfaces:**
- Produces: a running local Postgres on `localhost:5434`, database `contratos`, with all fase-1 tables from SPEC-02 §11 pre-created: `contrato`, `contrato_contrato_anterior`, `contrato_parcela`, `contrato_domicilio`, `garantia`, `garantia_ur`, `indicador_consistencia`, `contrato_evento`, `cerc_requisicao`, `webhook_inbox`, `dominio_arranjo`. Plan 03's tests connect to this via `LOCAL_DATABASE_URL`.

- [ ] **Step 1: Write `docker/initdb/01-contratos-schema.sql`**

```sql
CREATE TABLE contrato (
  id                        TEXT PRIMARY KEY,
  referencia_externa        TEXT UNIQUE NOT NULL,
  identificador_contrato    TEXT NOT NULL,
  protocolo_cerc            TEXT,
  id_contrato_cerc          TEXT,
  status                    TEXT NOT NULL,      -- §8
  status_garantia           TEXT,               -- NAO_APLICAVEL|SUFICIENTE|INSUFICIENTE|EXCESSO
  cnpj_participante         TEXT NOT NULL,
  documento_contratante     TEXT NOT NULL,
  cnpj_detentor             TEXT NOT NULL,
  tipo_efeito               TEXT NOT NULL,
  modalidade_operacao       TEXT NOT NULL,
  gestao_entidade_registradora TEXT NOT NULL,
  tipo_servico              TEXT,               -- de /v150: 1 GCAP, 2 simples, 3 monitoramento
  saldo_devedor             NUMERIC(18,2) NOT NULL,
  limite_operacao_garantida NUMERIC(18,2) NOT NULL,
  valor_mantido             NUMERIC(18,2) NOT NULL,
  data_assinatura           DATE NOT NULL,
  data_vencimento           DATE NOT NULL,
  repactuacao               BOOLEAN NOT NULL,
  carteira                  TEXT,
  tipo_avaliacao            TEXT,
  taxa_juros                NUMERIC(8,2),
  indexador                 TEXT,
  qtd_urs_alcancadas        INT,
  valor_urs_alcancadas      NUMERIC(18,2),
  resultado_distribuicao    TEXT,
  ind_sobrecolateral        NUMERIC(12,4),
  enviado_em                TIMESTAMPTZ,
  confirmado_em             TIMESTAMPTZ,
  UNIQUE (cnpj_participante, identificador_contrato)
);
CREATE INDEX ON contrato (cnpj_participante, status);
CREATE INDEX ON contrato (status);

CREATE TABLE contrato_contrato_anterior (
  contrato_id TEXT REFERENCES contrato(id), identificador_anterior TEXT,
  PRIMARY KEY (contrato_id, identificador_anterior));

CREATE TABLE contrato_parcela (
  contrato_id TEXT REFERENCES contrato(id), vencimento DATE,
  valor NUMERIC(18,2) NOT NULL CHECK (valor >= 0.01),
  PRIMARY KEY (contrato_id, vencimento));

CREATE TABLE contrato_domicilio (
  contrato_id TEXT PRIMARY KEY REFERENCES contrato(id),
  numero_documento_titular TEXT NOT NULL, nome_titular TEXT,
  tipo_conta TEXT NOT NULL, compe TEXT, ispb TEXT NOT NULL,
  agencia TEXT, numero_conta TEXT NOT NULL);

CREATE TABLE garantia (
  id                 TEXT PRIMARY KEY,
  contrato_id        TEXT NOT NULL REFERENCES contrato(id),
  referencia_externa TEXT NOT NULL,
  regras_divisao     TEXT NOT NULL,             -- 1 valor | 2 percentual
  valor_a_onerar     NUMERIC(18,2) NOT NULL,
  tipo_distribuicao  TEXT,                      -- padrao_empilhamento_ap | padrao_pro_rata_ap
  def_lista_credenciadoras TEXT[] NOT NULL,
  def_lista_arranjos       TEXT[] NOT NULL,
  def_documento_ufr        TEXT,
  def_documento_titular    TEXT,
  def_data_inicio          DATE NOT NULL,
  def_data_fim             DATE NOT NULL,
  UNIQUE (contrato_id, referencia_externa));

CREATE TABLE garantia_ur (                      -- snapshot do webhook/consulta
  garantia_id TEXT REFERENCES garantia(id),
  cnpj_credenciadora TEXT, documento_ufr TEXT, documento_titular TEXT,
  codigo_arranjo TEXT, data_liquidacao DATE, constituicao TEXT,
  valor_constituido_total NUMERIC(18,2), valor_bloqueado NUMERIC(18,2),
  indicador_oneracao TEXT, regras_divisao TEXT,
  valor_onerado NUMERIC(18,2), valor_constituido_efeito NUMERIC(18,2),
  origem TEXT NOT NULL,                         -- WEBHOOK | CONSULTA | AP013
  snapshot_em TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (garantia_id, cnpj_credenciadora, documento_ufr,
               documento_titular, codigo_arranjo, data_liquidacao, origem));

CREATE TABLE indicador_consistencia (
  contrato_id TEXT REFERENCES contrato(id), indicador TEXT, resultado TEXT,
  parametros JSONB, criticidade TEXT, observado_em TIMESTAMPTZ,
  PRIMARY KEY (contrato_id, indicador, observado_em));

CREATE TABLE contrato_evento (                  -- histórico completo (event sourcing leve)
  id BIGSERIAL PRIMARY KEY, contrato_id TEXT REFERENCES contrato(id),
  tipo TEXT NOT NULL, payload JSONB NOT NULL, ocorrido_em TIMESTAMPTZ NOT NULL);

CREATE TABLE cerc_requisicao (
  id                 TEXT PRIMARY KEY,
  recurso            TEXT NOT NULL,
  correlacao_id      TEXT NOT NULL,
  http_status        INT,
  request_body       JSONB NOT NULL,
  response_body      JSONB,
  tentativa          INT NOT NULL DEFAULT 1,
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_inbox (
  id               TEXT PRIMARY KEY,
  tipo_evento      TEXT NOT NULL,
  data_hora_evento TIMESTAMPTZ NOT NULL,
  payload          JSONB NOT NULL,
  hash_dedupe      TEXT NOT NULL UNIQUE,
  processado_em    TIMESTAMPTZ,
  erro             TEXT,
  recebido_em      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dominio_arranjo (
  codigo        TEXT PRIMARY KEY,
  descricao     TEXT,
  ativo         BOOLEAN NOT NULL DEFAULT true,
  atualizado_em TIMESTAMPTZ NOT NULL
);
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: contratos
      POSTGRES_PASSWORD: contratos
      POSTGRES_DB: contratos
    ports:
      - "5434:5432"
    volumes:
      - ./docker/initdb:/docker-entrypoint-initdb.d
```

- [ ] **Step 3: Start it and verify the schema loaded**

Run: `docker compose up -d postgres` then `docker compose exec postgres psql -U contratos -d contratos -c "\dt"`
Expected: lists `contrato`, `contrato_contrato_anterior`, `contrato_parcela`, `contrato_domicilio`, `garantia`, `garantia_ur`, `indicador_consistencia`, `contrato_evento`, `cerc_requisicao`, `webhook_inbox`, `dominio_arranjo`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docker/initdb/01-contratos-schema.sql
git commit -m "feat: local Postgres + fase-1 contrato schema"
```

---

## Self-Review Notes

- **Spec coverage:** SPEC-02 §11, scoped to the fase-1 subset (excludes `simulacao_contrato` and `divergencia_ap013`, deferred per design doc §3/§8) — fully covered.
- **Placeholder scan:** none.
- **Type consistency:** table/column names copied verbatim from SPEC-02 §11 — every later plan's `cloudsql_client.table("...")` calls must match these exact names. `cerc_requisicao`/`webhook_inbox`/`dominio_arranjo` match `ap-back-optin`'s schema shape exactly (same design, separate instance — see design doc §1).

**Next:** `2026-08-24-contratos-plan-03-cloudsql-client.md` (data access wrapper).
