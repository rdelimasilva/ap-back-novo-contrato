# contratos-service — Plan 02: Fase-1 Schema on Cloud SQL — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The fase-1 contrato schema applied to a real, dedicated Cloud SQL instance, so every later plan has real tables to read/write against.

**Architecture:** Plain versioned SQL in `sql/schema/`, applied via a small standalone script (`scripts/apply_schema.py`) using the Cloud SQL Python Connector — the same connection path production uses. No migration framework (matches house convention — see design doc §2/§3). No local Docker/Postgres: this dev machine has no Docker installed, and per an explicit decision with the user, this service always talks to its real dedicated Cloud SQL instance, even in dev — never a local database. That decision also settled the earlier "one instance per service?" question: yes, dedicated (not the shared `app-db` instance another service in the same GCP project uses), because contratos is expected to carry a high data volume.

**Tech Stack:** Cloud SQL for PostgreSQL 16, `cloud-sql-python-connector[pg8000]`, SQLAlchemy (all already in `requirements.txt` from Plan 01).

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§1, §3). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §11 (Modelo de dados). Series: plan 2 of ~10.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout, `requirements.txt`).

## Global Constraints

- Money columns are `NUMERIC(18,2)`; **never** `float`/`double` (SPEC-02 §11, §13.3).
- Tables excluded from this schema on purpose: `simulacao_contrato` (entra com `tipoOperacao = S`, fase 2) e `divergencia_ap013` (entra com o ingestor de reconciliação AP013, fase 2/3) — criar essas tabelas agora seria schema sem código que as use (design doc §3).
- **The Cloud SQL instance already exists** — provisioned outside this plan (controller ran `gcloud sql instances create`), so this task does not create infrastructure, only applies schema to it:
  - Instance: `contratos-db`, project `registradora-506000`, region `us-east1`, Postgres 16, tier `db-f1-micro`.
  - Database: `contratos`. User: `contratos_app`.
  - Connection name: `registradora-506000:us-east1:contratos-db`.
  - `.env` in the repo root (git-ignored) already has `CLOUDSQL_CONNECTION_NAME`, `CLOUDSQL_DB_USER`, `CLOUDSQL_DB_PASSWORD`, `CLOUDSQL_DB_NAME` filled in with real values, and `LOCAL_DATABASE_URL` empty. Do not print or log the contents of `.env` in any report — treat the password as a secret even though you can read the file to use it.
- Google Application Default Credentials are already configured on this machine (`gcloud auth application-default login` already run) — the connector will authenticate with them automatically; no additional auth setup needed.

---

### Task 1: `sql/schema/01-contratos-schema.sql` + `scripts/apply_schema.py`, applied to Cloud SQL

**Files:**
- Create: `contratos/sql/schema/01-contratos-schema.sql`
- Create: `contratos/scripts/apply_schema.py`
- Create: `contratos/scripts/__init__.py` (empty — makes it an importable package if a later plan needs to reuse the connector-setup helper; keeps `python scripts/apply_schema.py` working either way)

**Interfaces:**
- Produces: all fase-1 tables from SPEC-02 §11 created in the real `contratos` database on `contratos-db`: `contrato`, `contrato_contrato_anterior`, `contrato_parcela`, `contrato_domicilio`, `garantia`, `garantia_ur`, `indicador_consistencia`, `contrato_evento`, `cerc_requisicao`, `webhook_inbox`, `dominio_arranjo`. Plan 03's tests connect to this same instance via the same env vars.
- Produces: `scripts/apply_schema.py <path-to-sql-file>` — a reusable command for applying future `sql/schema/NN-*.sql` files to the real instance (there is no other schema-apply mechanism in this service, since there's no docker-compose init-script step to lean on).

- [ ] **Step 1: Write `sql/schema/01-contratos-schema.sql`**

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

- [ ] **Step 2: Write `scripts/__init__.py`**

Empty file.

- [ ] **Step 3: Write `scripts/apply_schema.py`**

```python
#!/usr/bin/env python
"""Aplica um arquivo .sql no Cloud SQL real deste serviço.

Uso: python scripts/apply_schema.py sql/schema/01-contratos-schema.sql

Não há Docker/Postgres local nesta máquina — este é o único mecanismo de
aplicar schema, tanto em dev quanto (futuramente) em homolog/produção. Usa
o mesmo caminho de conexão que o app em produção usará: Cloud SQL Python
Connector + SQLAlchemy (ver design doc §1). Lê CLOUDSQL_CONNECTION_NAME,
CLOUDSQL_DB_USER, CLOUDSQL_DB_PASSWORD, CLOUDSQL_DB_NAME do .env local.

Statements são separados por ";" — não usar ";" dentro de strings/valores
nos arquivos de schema aplicados por este script.
"""

import os
import sys

import sqlalchemy
from dotenv import load_dotenv
from google.cloud.sql.connector import Connector, IPTypes

load_dotenv()


def _create_engine():
    connector = Connector()

    def getconn():
        return connector.connect(
            os.environ["CLOUDSQL_CONNECTION_NAME"],
            "pg8000",
            user=os.environ["CLOUDSQL_DB_USER"],
            password=os.environ["CLOUDSQL_DB_PASSWORD"],
            db=os.environ["CLOUDSQL_DB_NAME"],
            ip_type=IPTypes.PUBLIC,
        )

    engine = sqlalchemy.create_engine("postgresql+pg8000://", creator=getconn)
    return engine, connector


def apply_sql_file(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        sql = f.read()

    statements = [s.strip() for s in sql.split(";") if s.strip()]

    engine, connector = _create_engine()
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(sqlalchemy.text(statement))
    finally:
        connector.close()

    return len(statements)


def main():
    if len(sys.argv) != 2:
        print("uso: python scripts/apply_schema.py <arquivo.sql>")
        sys.exit(1)

    count = apply_sql_file(sys.argv[1])
    print(f"Aplicado {sys.argv[1]}: {count} statement(s).")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Apply it to the real instance and verify**

Run: `python scripts/apply_schema.py sql/schema/01-contratos-schema.sql`
Expected: `Aplicado sql/schema/01-contratos-schema.sql: N statement(s).` with no error (N is the number of `;`-separated statements in the file — count them, don't guess).

Then verify the tables actually landed. Run this inline check (uses the same connector setup, so a successful run here also re-confirms Step 4 worked, not just that the script exited 0):

```bash
python -c "
from scripts.apply_schema import _create_engine
import sqlalchemy

engine, connector = _create_engine()
try:
    with engine.connect() as conn:
        rows = conn.execute(sqlalchemy.text(
            \"SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1\"
        )).fetchall()
        for r in rows:
            print(r[0])
finally:
    connector.close()
"
```

Expected output (11 lines, alphabetical): `cerc_requisicao`, `contrato`, `contrato_contrato_anterior`, `contrato_domicilio`, `contrato_evento`, `contrato_parcela`, `dominio_arranjo`, `garantia`, `garantia_ur`, `indicador_consistencia`, `webhook_inbox`.

- [ ] **Step 5: Commit**

```bash
git add sql/schema/01-contratos-schema.sql scripts/apply_schema.py scripts/__init__.py
git commit -m "feat: fase-1 contrato schema, applied to Cloud SQL via apply_schema.py"
```

Note: `.env` is git-ignored and already existed before this task (real Cloud SQL credentials) — do not add it, and do not paste its contents into your report or commit message.

---

## Self-Review Notes

- **Spec coverage:** SPEC-02 §11, scoped to the fase-1 subset (excludes `simulacao_contrato` and `divergencia_ap013`, deferred per design doc §3/§8) — fully covered. Applying to a real dedicated Cloud SQL instance (vs. local Docker) reflects the controller's decision with the user, recorded in design doc §1/§3.
- **Placeholder scan:** none.
- **Type consistency:** table/column names copied verbatim from SPEC-02 §11 — every later plan's `cloudsql_client.table("...")` calls must match these exact names. `cerc_requisicao`/`webhook_inbox`/`dominio_arranjo` match `ap-back-optin`'s schema shape (same design, separate instance). `scripts/apply_schema.py`'s `_create_engine()` connector setup is the same shape Plan 03's `shared/cloudsql_client.py` will use in production — Plan 03 should not need to invent a different pattern, just wrap it in the query-builder API.

**Next:** `2026-08-24-contratos-plan-03-cloudsql-client.md` (data access wrapper).
