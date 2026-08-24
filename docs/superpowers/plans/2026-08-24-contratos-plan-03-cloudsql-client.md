# contratos-service — Plan 03: CloudSqlClient Data Access Wrapper — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Supabase/PostgREST-style data access wrapper (`shared/cloudsql_client.py`) over SQLAlchemy — the only way any code in this service touches the database.

**Architecture:** `QueryBuilder` with a chainable `.table(name).select()/.insert()/.update()/.upsert()/.delete()/.eq()/.order()/.limit()/.execute()` API, connecting to local Postgres (via `LOCAL_DATABASE_URL`, for a dev with Docker) or Cloud SQL (via the Cloud SQL Python Connector — what this dev machine actually uses, since it has no Docker). Copied from `ap-back-optin/optin/shared/cloudsql_client.py`, with two additions the Plan 02 final review flagged as required before this service can correctly write its own tables:
1. `.upsert()` — `garantia_ur` and `indicador_consistencia` are snapshot/observation tables whose primary keys are specifically designed for idempotent re-writes (re-observing the same webhook/consulta data), which an insert-only client cannot do safely.
2. Pool settings copied from optin's real `_create_engine` (`pool_size`, `max_overflow`, `pool_timeout`, `pool_recycle`) — not from this repo's `scripts/apply_schema.py`, whose `_create_engine` is a deliberately stripped-down one-shot variant with no pooling, wrong for a long-lived Cloud Run process.

**Tech Stack:** SQLAlchemy 2.x, pg8000, google-cloud-sql-connector, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§1, §3). Series: plan 3 of ~10.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout); `2026-08-24-contratos-plan-02-schema.md` (the `dominio_arranjo` table this plan's tests use, and the real `contratos-db` Cloud SQL instance itself — there is no local Postgres on this machine, so this plan's tests run against the real instance, same as `scripts/apply_schema.py` already does).

## Global Constraints

- Money columns stay `NUMERIC(18,2)` end to end; this wrapper does no numeric coercion of its own — callers are responsible for passing `decimal.Decimal`, never `float`.
- Secrets (`CLOUDSQL_DB_PASSWORD`) never committed; read from env vars only. `.env` already has real values from Plan 02 — do not print or log its contents.
- No Docker/local Postgres on this machine: `LOCAL_DATABASE_URL` stays empty in `.env`, so `_create_engine()` falls through to the Cloud SQL Connector branch, exactly like `scripts/apply_schema.py` already does. Keep the `LOCAL_DATABASE_URL` branch in the code anyway (matches optin, costs nothing, and lets a future dev with Docker use it) — just don't rely on it working here.

---

### Task 1: `shared/cloudsql_client.py` with upsert and configurable IP type

**Files:**
- Create: `contratos/shared/__init__.py`
- Create: `contratos/shared/cloudsql_client.py`
- Test: `contratos/shared/tests/__init__.py`
- Test: `contratos/shared/tests/test_cloudsql_client.py`
- Modify: `contratos/.env.example` (add `CLOUDSQL_IP_TYPE`)

**Interfaces:**
- Consumes: `LOCAL_DATABASE_URL` (dev/test, with Docker) or `CLOUDSQL_CONNECTION_NAME`/`CLOUDSQL_DB_USER`/`CLOUDSQL_DB_PASSWORD`/`CLOUDSQL_DB_NAME`/`CLOUDSQL_IP_TYPE` (Cloud SQL — what this machine actually uses) env vars.
- Produces: `get_db() -> CloudSQLClient | None`; `CloudSQLClient.table(name) -> QueryBuilder`; `QueryBuilder.select()/.insert()/.update()/.upsert(data, on_conflict)/.delete()/.eq()/.order()/.limit()` (all return `self`, chainable) and `.execute() -> ExecuteResult(data: list[dict], count: int | None)`. Every later plan that touches the database imports `get_db` from here. `on_conflict` is a raw SQL fragment naming the conflict target — either a plain column list (`"codigo"`) or an expression list matching a unique index built on expressions (e.g. `garantia_ur`'s `COALESCE(...)`-based natural key from Plan 02's schema fix) — the caller is responsible for passing exactly what the underlying unique index/constraint expects.

- [ ] **Step 1: Write the failing test**

```python
# contratos/shared/tests/test_cloudsql_client.py
from shared.cloudsql_client import get_db


def _cleanup():
    db = get_db()
    db.table("dominio_arranjo").delete().eq("codigo", "VCC").execute()


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


def test_insert_select_update_delete_round_trip():
    db = get_db()

    inserted = db.table("dominio_arranjo").insert({
        "codigo": "VCC",
        "descricao": "Visa Crédito",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }).execute()
    assert inserted.data[0]["codigo"] == "VCC"

    found = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert len(found.data) == 1
    assert found.data[0]["ativo"] is True

    updated = db.table("dominio_arranjo").update({"ativo": False}).eq("codigo", "VCC").execute()
    assert updated.data[0]["ativo"] is False

    deleted = db.table("dominio_arranjo").delete().eq("codigo", "VCC").execute()
    assert len(deleted.data) == 1

    empty = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert empty.data == []


def test_upsert_inserts_then_updates_in_place():
    db = get_db()

    first = db.table("dominio_arranjo").upsert({
        "codigo": "VCC",
        "descricao": "Visa Crédito",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert first.data[0]["descricao"] == "Visa Crédito"

    second = db.table("dominio_arranjo").upsert({
        "codigo": "VCC",
        "descricao": "Visa Crédito Atualizado",
        "ativo": False,
        "atualizado_em": "2026-08-20T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert second.data[0]["descricao"] == "Visa Crédito Atualizado"
    assert second.data[0]["ativo"] is False

    rows = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert len(rows.data) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest shared/tests/test_cloudsql_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.cloudsql_client'`

- [ ] **Step 3: Write `shared/__init__.py` and `shared/tests/__init__.py`**

Empty files, both.

- [ ] **Step 4: Write `shared/cloudsql_client.py`**

```python
"""Cliente Cloud SQL — API estilo Supabase/PostgREST sobre SQLAlchemy.

    get_db().table("contrato").select("*").eq("status", "REGISTRADO").limit(10).execute()
    get_db().table("contrato").insert({...}).execute()
    get_db().table("garantia_ur").upsert({...}, on_conflict="garantia_id, ...").execute()

Sem Django ORM (design §1): DATABASES={} no settings, todo acesso passa por
aqui. Conecta a Postgres local via LOCAL_DATABASE_URL (dev com Docker) ou a
Cloud SQL via Connector (CLOUDSQL_CONNECTION_NAME) — o caminho real usado
nesta máquina, que não tem Docker.
"""

import json
import logging
import os
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class ExecuteResult:
    def __init__(self, data=None, count: Optional[int] = None):
        self.data = data or []
        self.count = count


class QueryBuilder:
    def __init__(self, engine, table_name: str):
        self._engine = engine
        self._table = table_name
        self._select_fields = "*"
        self._count_mode: Optional[str] = None
        self._filters: List[tuple] = []
        self._order_by: List[tuple] = []
        self._limit_val: Optional[int] = None
        self._op = "select"
        self._insert_data = None
        self._update_data: Optional[dict] = None
        self._on_conflict: Optional[str] = None

    def select(self, fields: str = "*", count: Optional[str] = None) -> "QueryBuilder":
        self._select_fields = fields
        self._count_mode = count
        return self

    def eq(self, field: str, value: Any) -> "QueryBuilder":
        self._filters.append(("eq", field, value))
        return self

    def order(self, field: str, desc: bool = False) -> "QueryBuilder":
        self._order_by.append((field, desc))
        return self

    def limit(self, n: int) -> "QueryBuilder":
        self._limit_val = n
        return self

    def insert(self, data) -> "QueryBuilder":
        self._op = "insert"
        self._insert_data = data
        return self

    def upsert(self, data, on_conflict: str) -> "QueryBuilder":
        self._op = "upsert"
        self._insert_data = data
        self._on_conflict = on_conflict
        return self

    def update(self, data: dict) -> "QueryBuilder":
        self._op = "update"
        self._update_data = data
        return self

    def delete(self) -> "QueryBuilder":
        self._op = "delete"
        return self

    def execute(self) -> ExecuteResult:
        try:
            return {
                "select": self._exec_select,
                "insert": self._exec_insert,
                "upsert": self._exec_upsert,
                "update": self._exec_update,
                "delete": self._exec_delete,
            }[self._op]()
        except Exception:
            logger.exception("[CloudSQL] Erro em %s.%s", self._table, self._op)
            raise

    def _build_where(self):
        if not self._filters:
            return "", {}
        clauses, params = [], {}
        for i, (op, field, val) in enumerate(self._filters):
            pname = f"p{i}"
            if op == "eq":
                clauses.append(f"{field} = :{pname}")
                params[pname] = val
        return "WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _serialize(v: Any) -> Any:
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, default=str)
        return v

    @staticmethod
    def _deserialize_row(row: dict) -> dict:
        result = {}
        for k, v in row.items():
            if isinstance(v, str) and len(v) > 1 and v[0] in ("{", "["):
                try:
                    result[k] = json.loads(v)
                    continue
                except (json.JSONDecodeError, ValueError):
                    pass
            result[k] = v
        return result

    def _exec_select(self) -> ExecuteResult:
        from sqlalchemy import text

        where, params = self._build_where()
        with self._engine.connect() as conn:
            if self._count_mode == "exact":
                sql = f"SELECT COUNT(*) FROM {self._table} {where}"
                return ExecuteResult(data=[], count=conn.execute(text(sql), params).scalar())

            order_clause = ""
            if self._order_by:
                parts = [f"{f} {'DESC' if d else 'ASC'}" for f, d in self._order_by]
                order_clause = "ORDER BY " + ", ".join(parts)
            limit_clause = f"LIMIT {self._limit_val}" if self._limit_val else ""

            sql = f"SELECT {self._select_fields} FROM {self._table} {where} {order_clause} {limit_clause}"
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])

    def _exec_insert(self) -> ExecuteResult:
        from sqlalchemy import text

        rows = self._insert_data if isinstance(self._insert_data, list) else [self._insert_data]
        inserted = []
        with self._engine.begin() as conn:
            for row in rows:
                serialized = {k: self._serialize(v) for k, v in row.items()}
                cols = list(serialized.keys())
                placeholders = [f":{c}" for c in cols]
                sql = f"INSERT INTO {self._table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) RETURNING *"
                result = conn.execute(text(sql), serialized)
                inserted.extend(self._deserialize_row(dict(r._mapping)) for r in result)
        return ExecuteResult(data=inserted)

    def _exec_upsert(self) -> ExecuteResult:
        from sqlalchemy import text

        rows = self._insert_data if isinstance(self._insert_data, list) else [self._insert_data]
        upserted = []
        with self._engine.begin() as conn:
            for row in rows:
                serialized = {k: self._serialize(v) for k, v in row.items()}
                cols = list(serialized.keys())
                placeholders = [f":{c}" for c in cols]
                update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
                sql = (
                    f"INSERT INTO {self._table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) "
                    f"ON CONFLICT ({self._on_conflict}) DO UPDATE SET {update_clause} RETURNING *"
                )
                result = conn.execute(text(sql), serialized)
                upserted.extend(self._deserialize_row(dict(r._mapping)) for r in result)
        return ExecuteResult(data=upserted)

    def _exec_update(self) -> ExecuteResult:
        from sqlalchemy import text

        serialized = {k: self._serialize(v) for k, v in self._update_data.items()}
        set_clause = ", ".join(f"{k} = :u_{k}" for k in serialized)
        params = {f"u_{k}": v for k, v in serialized.items()}
        where, where_params = self._build_where()
        params.update(where_params)
        sql = f"UPDATE {self._table} SET {set_clause} {where} RETURNING *"
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])

    def _exec_delete(self) -> ExecuteResult:
        from sqlalchemy import text

        where, params = self._build_where()
        sql = f"DELETE FROM {self._table} {where} RETURNING *"
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])


class CloudSQLClient:
    def __init__(self, engine):
        self._engine = engine

    def table(self, name: str) -> QueryBuilder:
        return QueryBuilder(self._engine, name)


def _create_engine():
    import sqlalchemy

    local_url = os.getenv("LOCAL_DATABASE_URL")
    if local_url:
        logger.info("[CloudSQL] Engine LOCAL via LOCAL_DATABASE_URL")
        return sqlalchemy.create_engine(local_url, pool_pre_ping=True)

    connection_name = os.getenv("CLOUDSQL_CONNECTION_NAME")
    if not connection_name:
        return None

    from google.cloud.sql.connector import Connector, IPTypes

    connector = Connector()
    db_user = os.getenv("CLOUDSQL_DB_USER", "postgres")
    db_pass = os.getenv("CLOUDSQL_DB_PASSWORD", "")
    db_name = os.getenv("CLOUDSQL_DB_NAME", "postgres")
    ip_type = IPTypes[os.getenv("CLOUDSQL_IP_TYPE", "PUBLIC").upper()]

    def getconn():
        return connector.connect(
            connection_name, "pg8000", user=db_user, password=db_pass, db=db_name, ip_type=ip_type,
        )

    logger.info("[CloudSQL] Engine criado para %s (ip_type=%s)", connection_name, ip_type.name)
    return sqlalchemy.create_engine(
        "postgresql+pg8000://", creator=getconn, pool_size=5, max_overflow=2, pool_timeout=30, pool_recycle=1800,
    )


_client: Optional[CloudSQLClient] = None


def get_db() -> Optional[CloudSQLClient]:
    global _client
    if _client is not None:
        return _client
    engine = _create_engine()
    if engine is None:
        return None
    _client = CloudSQLClient(engine)
    return _client
```

- [ ] **Step 5: Add `CLOUDSQL_IP_TYPE` to `.env.example`**

In the Cloud SQL section, add a line after `CLOUDSQL_DB_NAME=`:

```
CLOUDSQL_IP_TYPE=PUBLIC
```

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest shared/tests/test_cloudsql_client.py -v`
Expected: PASS (2 passed) — this connects to the real `contratos-db` Cloud SQL instance via the `.env` already configured in this repo (Plan 02); there is no local Postgres to start.

- [ ] **Step 7: Commit**

```bash
git add shared/__init__.py shared/cloudsql_client.py shared/tests/__init__.py shared/tests/test_cloudsql_client.py .env.example
git commit -m "feat: CloudSqlClient data access wrapper with upsert (no ORM)"
```

---

## Self-Review Notes

- **Spec coverage:** design §1/§3 (`CloudSqlClient`, no-ORM data access), plus the Plan 02 final review's two carried-forward findings (upsert support, pooled `_create_engine`) — fully covered. `ip_type` is now env-driven per that same review.
- **Placeholder scan:** none.
- **Type consistency:** `get_db()` returns `CloudSQLClient | None`; `.table(name)` returns `QueryBuilder`; `.execute()` returns `ExecuteResult(data: list[dict], count: int | None)` — these exact names/shapes are what every later plan imports and relies on. `.upsert(data, on_conflict)` is new relative to optin's version — Plan 09 (webhook receiver, writing `garantia_ur`/`indicador_consistencia`) must use `on_conflict="garantia_id, cnpj_credenciadora, COALESCE(documento_ufr, ''), COALESCE(documento_titular, ''), codigo_arranjo, data_liquidacao, origem"` for `garantia_ur` (matching Plan 02's schema-fix natural-key index exactly) and `on_conflict="contrato_id, indicador, observado_em"` for `indicador_consistencia`.

**Next:** `2026-08-24-contratos-plan-04-secrets.md` (Secret Manager wrapper).
