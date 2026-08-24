# contratos-service — Plan 05: Multi-tenancy Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retrofit this service onto the multi-tenancy model `ap-back-optin` already adopted (separate session, its own Plan 09) and that the user confirmed contratos must follow too: one Cloud SQL instance per tenant (financiador), config resolved per tenant, no more single global database/credentials.

**Architecture:** New `shared/tenant_config.py` (config per tenant, from a Secret Manager secret named `TENANT_{financiador_id}_CONFIG_CONTRATOS` — note the `_CONTRATOS` suffix, deliberately different from optin's `TENANT_{financiador_id}_CONFIG`, to avoid a Secret Manager name collision in the same GCP project and to avoid coupling this service's config shape to optin's already-shipped one). Retrofit of `shared/cloudsql_client.py` (Plan 03): `get_db()` singleton becomes `get_db(financiador_id)`, one `CloudSQLClient` cached per tenant behind a per-tenant lock — copying the exact fix `ap-back-optin` had to make after a real engine-leak bug in its own first attempt at this. `LOCAL_DATABASE_URL` is removed entirely: there is exactly one connection code path now (per-tenant Cloud SQL), even for the dev/test tenant.

**Tech Stack:** Same as Plan 03 (SQLAlchemy, pg8000, Cloud SQL Python Connector), plus `threading` (stdlib, already used).

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§1.1). Series: plan 5 of ~11 (this plan replaces what would have been a straight "token provider" plan — token provider is now Plan 06, tenant-aware from the start since it was never merged single-tenant).

**Depends on:** `2026-08-24-contratos-plan-03-cloudsql-client.md` (the code being retrofitted); `2026-08-24-contratos-plan-04-secrets.md` (`get_secret()`, unchanged, reused as-is per the design — same as optin did).

## Global Constraints

- `financiador_id` is always the tenant's own `cnpjParticipante` (14-digit CNPJ string) — never a separate opaque tenant ID (design §1.1, matching optin's identical decision for `cnpjFinanciador`).
- The tenant-config secret name for this service is `TENANT_{financiador_id}_CONFIG_CONTRATOS` — **not** `TENANT_{financiador_id}_CONFIG` (that name belongs to `ap-back-optin`; reusing it would collide in Secret Manager and couple this service to optin's JSON shape).
- Money columns stay `NUMERIC(18,2)` end to end (unchanged from Plan 03) — this retrofit does not touch any money-handling code.
- Secrets never logged/committed in plaintext. `.env` (git-ignored) holds this repo's dev/test tenant's real config as a single JSON-valued env var after this plan; `.env.example` shows only the shape.
- The dev/test tenant reuses the CNPJ `12345678000199` — the same one `ap-back-optin` already uses for its own dev/test tenant (same conceptual "tenant," different service, different secret name, same underlying `contratos-db` Cloud SQL instance from Plan 02 — no new infra needed for this plan).

---

### Task 1: `shared/tenant_config.py`

**Files:**
- Create: `contratos/shared/tenant_config.py`
- Test: `contratos/shared/tests/test_tenant_config.py`

**Interfaces:**
- Consumes: `shared.secrets.get_secret` (Plan 04, unchanged).
- Produces: `get_tenant_config(financiador_id: str) -> dict`. Task 2 (`cloudsql_client.py`) and Plan 06 (`token_provider.py`) both call this.

- [ ] **Step 1: Write the failing test**

```python
# contratos/shared/tests/test_tenant_config.py
import json

import pytest

import shared.tenant_config as tenant_config_module


@pytest.fixture(autouse=True)
def _clear_cache():
    tenant_config_module._cache.clear()
    yield
    tenant_config_module._cache.clear()


CONFIG_EXEMPLO = {
    "cloudsql_connection_name": "proj:region:instance",
    "cloudsql_db_user": "app",
    "cloudsql_db_password": "senha",
    "cloudsql_db_name": "app",
    "cerc_client_id": "client-1",
    "cerc_client_secret": "segredo",
}


def test_get_tenant_config_le_e_parseia_json(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("TENANT_12345678000199_CONFIG_CONTRATOS", json.dumps(CONFIG_EXEMPLO))

    assert get_tenant_config("12345678000199") == CONFIG_EXEMPLO


def test_get_tenant_config_usa_cache_sem_reler_env(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("TENANT_99999999000191_CONFIG_CONTRATOS", json.dumps(CONFIG_EXEMPLO))

    primeira = get_tenant_config("99999999000191")
    monkeypatch.delenv("TENANT_99999999000191_CONFIG_CONTRATOS", raising=False)
    segunda = get_tenant_config("99999999000191")

    assert primeira == segunda


def test_get_tenant_config_propaga_erro_quando_segredo_ausente(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("TENANT_00000000000000_CONFIG_CONTRATOS", raising=False)

    with pytest.raises(RuntimeError):
        get_tenant_config("00000000000000")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest shared/tests/test_tenant_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.tenant_config'`

- [ ] **Step 3: Write `shared/tenant_config.py`**

```python
"""Configuração por tenant (financiador) para o serviço de contratos.

Um segredo por tenant (TENANT_{financiador_id}_CONFIG_CONTRATOS, JSON) via
shared.secrets.get_secret — dev local lê a env var de mesmo nome (sem
GOOGLE_CLOUD_PROJECT); produção/homolog lê do Secret Manager, um segredo
por tenant. Cacheado em memória por processo, sem TTL (mesma filosofia do
cache de token de services/cerc/token_provider.py).

Nome de segredo com sufixo _CONTRATOS: deliberadamente diferente do
TENANT_{financiador_id}_CONFIG do ap-back-optin — cada serviço tem seu
próprio segredo (mesmo raciocínio de "cada serviço com suas próprias
credenciais CERC" já adotado), evitando colisão de nome no Secret Manager.

Chaves esperadas no JSON: cloudsql_connection_name, cloudsql_db_user,
cloudsql_db_password, cloudsql_db_name, cloudsql_ip_type (opcional,
default "PUBLIC"), cerc_client_id, cerc_client_secret.

Ver docs/superpowers/specs/2026-08-24-contratos-service-design.md §1.1.
"""
import json

from shared.secrets import get_secret

_cache: dict = {}


def get_tenant_config(financiador_id: str) -> dict:
    if financiador_id in _cache:
        return _cache[financiador_id]

    raw = get_secret(f"TENANT_{financiador_id}_CONFIG_CONTRATOS")
    config = json.loads(raw)
    _cache[financiador_id] = config
    return config
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest shared/tests/test_tenant_config.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add shared/tenant_config.py shared/tests/test_tenant_config.py
git commit -m "feat: per-tenant config reader (TENANT_{id}_CONFIG_CONTRATOS secret)"
```

---

### Task 2: Retrofit `shared/cloudsql_client.py` to `get_db(financiador_id)`

**Files:**
- Modify: `contratos/shared/cloudsql_client.py` (full rewrite of the module-level engine/client management; `QueryBuilder`/`ExecuteResult`/`CloudSQLClient` classes keep their Plan 03 shape, including `.upsert()` and the hardening fixes already applied — only `_create_engine` and the singleton at the bottom change)
- Modify: `contratos/shared/tests/test_cloudsql_client.py` (every `get_db()` call becomes `get_db(FINANCIADOR_TESTE)`; two new tests added for per-tenant caching and the single-flight-on-first-access race)
- Modify: `contratos/.env` (consolidate `CLOUDSQL_CONNECTION_NAME`/`CLOUDSQL_DB_USER`/`CLOUDSQL_DB_PASSWORD`/`CLOUDSQL_DB_NAME`/`CLOUDSQL_IP_TYPE` into one `TENANT_12345678000199_CONFIG_CONTRATOS` JSON value; drop `LOCAL_DATABASE_URL`) — **do not print this file's contents anywhere**, just edit it
- Modify: `contratos/.env.example` (same restructuring, placeholder values only)

**Interfaces:**
- Consumes: `shared.tenant_config.get_tenant_config` (Task 1).
- Produces: `get_db(financiador_id: str) -> CloudSQLClient` (no longer `Optional` — a missing/invalid tenant config now raises via `get_tenant_config`/`json.loads` instead of silently returning `None`). `CloudSQLClient.table(name) -> QueryBuilder` and all of `QueryBuilder`'s methods (`.select()/.insert()/.upsert()/.update()/.delete()/.eq()/.order()/.limit()/.execute()`) are **unchanged** from Plan 03 — every later plan that already assumed these names still works, only the `get_db()` call site changes to `get_db(financiador_id)`.

- [ ] **Step 1: Write the failing/updated test**

```python
# contratos/shared/tests/test_cloudsql_client.py
import os
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ENVIRONMENT") == "production",
    reason="testes gravam/apagam dados reais — nunca rodar contra produção",
)

TEST_CODIGO = "__TEST_ZZZ__"
FINANCIADOR_TESTE = "12345678000199"
FINANCIADOR_TESTE_2 = "99999999000191"
FINANCIADOR_TESTE_3 = "11111111000100"

from shared.cloudsql_client import get_db  # noqa: E402
import shared.cloudsql_client as cloudsql_client_module  # noqa: E402


def _cleanup():
    db = get_db(FINANCIADOR_TESTE)
    db.table("dominio_arranjo").delete().eq("codigo", TEST_CODIGO).execute()


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


def test_insert_select_update_delete_round_trip():
    db = get_db(FINANCIADOR_TESTE)

    inserted = db.table("dominio_arranjo").insert({
        "codigo": TEST_CODIGO,
        "descricao": "Arranjo de teste",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }).execute()
    assert inserted.data[0]["codigo"] == TEST_CODIGO

    found = db.table("dominio_arranjo").select("*").eq("codigo", TEST_CODIGO).execute()
    assert len(found.data) == 1
    assert found.data[0]["ativo"] is True

    updated = db.table("dominio_arranjo").update({"ativo": False}).eq("codigo", TEST_CODIGO).execute()
    assert updated.data[0]["ativo"] is False

    deleted = db.table("dominio_arranjo").delete().eq("codigo", TEST_CODIGO).execute()
    assert len(deleted.data) == 1

    empty = db.table("dominio_arranjo").select("*").eq("codigo", TEST_CODIGO).execute()
    assert empty.data == []


def test_upsert_inserts_then_updates_in_place():
    db = get_db(FINANCIADOR_TESTE)

    first = db.table("dominio_arranjo").upsert({
        "codigo": TEST_CODIGO,
        "descricao": "Arranjo de teste",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert first.data[0]["descricao"] == "Arranjo de teste"

    second = db.table("dominio_arranjo").upsert({
        "codigo": TEST_CODIGO,
        "descricao": "Arranjo de teste atualizado",
        "ativo": False,
        "atualizado_em": "2026-08-20T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert second.data[0]["descricao"] == "Arranjo de teste atualizado"
    assert second.data[0]["ativo"] is False

    rows = db.table("dominio_arranjo").select("*").eq("codigo", TEST_CODIGO).execute()
    assert len(rows.data) == 1


def test_delete_without_filter_raises():
    db = get_db(FINANCIADOR_TESTE)
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").delete().execute()


def test_update_without_filter_raises():
    db = get_db(FINANCIADOR_TESTE)
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").update({"ativo": False}).execute()


def test_get_db_cacheia_por_financiador_id(monkeypatch):
    cloudsql_client_module._clients.clear()
    # Aponta o "segundo tenant" para a MESMA config do tenant de teste — o
    # objetivo aqui é provar que o cache é chaveado por financiador_id (dois
    # tenants diferentes nunca compartilham o mesmo CloudSQLClient), não
    # provisionar um segundo Cloud SQL real só para este teste.
    monkeypatch.setenv(
        f"TENANT_{FINANCIADOR_TESTE_2}_CONFIG_CONTRATOS",
        os.environ[f"TENANT_{FINANCIADOR_TESTE}_CONFIG_CONTRATOS"],
    )

    db1a = get_db(FINANCIADOR_TESTE)
    db1b = get_db(FINANCIADOR_TESTE)
    db2 = get_db(FINANCIADOR_TESTE_2)

    assert db1a is db1b
    assert db1a is not db2

    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_2, None)


def test_get_db_single_flight_on_concurrent_first_access(monkeypatch):
    # Reproduz o bug real que o ap-back-optin encontrou e corrigiu: duas
    # (aqui, dez) threads chamando get_db() pela primeira vez para o MESMO
    # financiador_id ainda não cacheado, ao mesmo tempo. Sem o lock por
    # tenant, cada uma chamaria _create_engine (engine + connector reais) e
    # a perdedora vazaria um pool de conexões nunca fechado. Trocamos
    # _create_engine por um fake lento pra alargar a janela de corrida e
    # contamos quantas vezes ele é de fato chamado.
    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_3, None)
    cloudsql_client_module._locks.pop(FINANCIADOR_TESTE_3, None)
    monkeypatch.setenv(
        f"TENANT_{FINANCIADOR_TESTE_3}_CONFIG_CONTRATOS",
        os.environ[f"TENANT_{FINANCIADOR_TESTE}_CONFIG_CONTRATOS"],
    )

    call_count = 0
    count_lock = threading.Lock()

    def _slow_fake_engine(config):
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.05)  # alarga a janela pra forçar a corrida
        return object()

    monkeypatch.setattr(cloudsql_client_module, "_create_engine", _slow_fake_engine)

    results = []

    def _call():
        results.append(get_db(FINANCIADOR_TESTE_3))

    threads = [threading.Thread(target=_call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 1  # engine construído uma única vez
    assert len({id(r) for r in results}) == 1  # todas as threads recebem o mesmo client

    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_3, None)
    cloudsql_client_module._locks.pop(FINANCIADOR_TESTE_3, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest shared/tests/test_cloudsql_client.py -v`
Expected: FAIL — `get_db()` still takes no argument, so every call raises `TypeError: get_db() takes 0 positional arguments but 1 was given` (and `_clients`/`_locks` don't exist yet on the module).

- [ ] **Step 3: Rewrite `shared/cloudsql_client.py`**

Keep the `ExecuteResult`, `QueryBuilder` (with `.upsert()`, `_check_unfiltered`, the array-vs-JSON `_serialize`, everything from Plan 03's hardening fix), and `CloudSQLClient` classes byte-for-byte as they are today. Replace only the module docstring and everything from `_create_engine` down to the end of the file with:

```python
"""Cliente Cloud SQL — API estilo Supabase/PostgREST sobre SQLAlchemy.

    get_db(financiador_id).table("contrato").select("*").eq("status", "REGISTRADO").limit(10).execute()
    get_db(financiador_id).table("contrato").insert({...}).execute()
    get_db(financiador_id).table("garantia_ur").upsert({...}, on_conflict="garantia_id, ...").execute()

Sem Django ORM (design §1): DATABASES={} no settings, todo acesso passa por
aqui. Um Cloud SQL (banco) por tenant/financiador (design §1.1) — a config
de conexão vem de shared.tenant_config.get_tenant_config, e o
CloudSQLClient resultante é cacheado em memória por financiador_id.
"""

import json
import logging
import threading
from typing import Any, List, Optional

from shared.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)
```

(everything between here and `_create_engine` — `ExecuteResult`, `QueryBuilder`, `CloudSQLClient` — stays exactly as it is in the current file; do not touch it)

```python
def _create_engine(config: dict):
    import sqlalchemy
    from google.cloud.sql.connector import Connector, IPTypes

    connector = Connector()
    ip_type = IPTypes[config.get("cloudsql_ip_type", "PUBLIC").upper()]

    def getconn():
        return connector.connect(
            config["cloudsql_connection_name"],
            "pg8000",
            user=config["cloudsql_db_user"],
            password=config["cloudsql_db_password"],
            db=config["cloudsql_db_name"],
            ip_type=ip_type,
        )

    logger.info(
        "[CloudSQL] Engine criado para tenant (connection=%s, ip_type=%s)",
        config["cloudsql_connection_name"], ip_type.name,
    )
    return sqlalchemy.create_engine(
        "postgresql+pg8000://",
        creator=getconn,
        pool_size=5,
        max_overflow=2,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
        hide_parameters=True,
    )


_meta_lock = threading.Lock()
_locks: dict = {}
_clients: dict = {}


def _lock_for(financiador_id: str) -> threading.Lock:
    if financiador_id not in _locks:
        with _meta_lock:
            if financiador_id not in _locks:
                _locks[financiador_id] = threading.Lock()
    return _locks[financiador_id]


def get_db(financiador_id: str) -> CloudSQLClient:
    if financiador_id in _clients:
        return _clients[financiador_id]

    with _lock_for(financiador_id):
        if financiador_id in _clients:
            return _clients[financiador_id]

        config = get_tenant_config(financiador_id)
        engine = _create_engine(config)
        client = CloudSQLClient(engine)
        _clients[financiador_id] = client
        return client
```

Note what's removed relative to Plan 03: `os` import (no longer reads `os.environ` directly), the entire `LOCAL_DATABASE_URL` branch, and the module-level `_client`/`_lock` singleton (replaced by the `_clients`/`_locks` dicts above).

- [ ] **Step 4: Consolidate `.env` into the tenant-config secret**

Read the current `.env`'s `CLOUDSQL_CONNECTION_NAME`, `CLOUDSQL_DB_USER`, `CLOUDSQL_DB_PASSWORD`, `CLOUDSQL_DB_NAME`, `CLOUDSQL_IP_TYPE` (default to `PUBLIC` if that key isn't set), and `CERC_CLIENT_ID`/`CERC_CLIENT_SECRET` (may be empty strings today — that's fine, they're only needed for real CERC calls, not for this task's tests). Build one JSON object with keys `cloudsql_connection_name`, `cloudsql_db_user`, `cloudsql_db_password`, `cloudsql_db_name`, `cloudsql_ip_type`, `cerc_client_id`, `cerc_client_secret`, and set it as a single-line env var:

```
TENANT_12345678000199_CONFIG_CONTRATOS={"cloudsql_connection_name": "...", "cloudsql_db_user": "...", "cloudsql_db_password": "...", "cloudsql_db_name": "...", "cloudsql_ip_type": "PUBLIC", "cerc_client_id": "", "cerc_client_secret": ""}
```

Remove the now-unused `LOCAL_DATABASE_URL`, `CLOUDSQL_CONNECTION_NAME`, `CLOUDSQL_DB_USER`, `CLOUDSQL_DB_PASSWORD`, `CLOUDSQL_DB_NAME`, `CLOUDSQL_IP_TYPE` lines from `.env`. Leave `CERC_CLIENT_ID`/`CERC_CLIENT_SECRET`/`CERC_AUTH_URL`/`CERC_API_BASE_URL` as they are for now — Plan 06 still reads `CERC_AUTH_URL`/`CERC_API_BASE_URL` as plain env vars (design §1.1: those are environment hosts, not per-tenant), but `CERC_CLIENT_ID`/`CERC_CLIENT_SECRET` as bare env vars become dead after this task (superseded by the JSON's `cerc_client_id`/`cerc_client_secret`) — remove those two lines too, to avoid two sources of truth for the same values.

- [ ] **Step 5: Update `.env.example` to match the new shape**

Replace the `LOCAL_DATABASE_URL`/`CLOUDSQL_*` block and the `CERC_CLIENT_ID`/`CERC_CLIENT_SECRET` lines with:

```
# Config por tenant (financiador) — um segredo por tenant, ver
# docs/superpowers/specs/2026-08-24-contratos-service-design.md §1.1.
# Em produção/homolog isso vem do Secret Manager (mesma chave); em dev,
# é uma env var local com o JSON completo numa linha só.
TENANT_12345678000199_CONFIG_CONTRATOS={"cloudsql_connection_name": "", "cloudsql_db_user": "", "cloudsql_db_password": "", "cloudsql_db_name": "", "cloudsql_ip_type": "PUBLIC", "cerc_client_id": "", "cerc_client_secret": ""}
```

Keep `CERC_AUTH_URL`/`CERC_API_BASE_URL` as they are (still plain env vars, not per-tenant).

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest shared/tests/test_cloudsql_client.py -v`
Expected: PASS (7 passed) — against the real `contratos-db` instance (now addressed through the tenant config for `12345678000199`, same instance as before).

- [ ] **Step 7: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass (Plans 01/03/04's tests are unaffected by this retrofit — Plan 04's `test_secrets.py` doesn't touch `cloudsql_client`, and Plan 01's `test_health.py` doesn't touch the database).

- [ ] **Step 8: Commit**

```bash
git add shared/cloudsql_client.py shared/tests/test_cloudsql_client.py .env.example
git commit -m "feat: retrofit CloudSqlClient to get_db(financiador_id) — one Cloud SQL per tenant"
```

(`.env` is git-ignored — its edit in Step 4 is not part of this commit, and must never be added.)

---

## Self-Review Notes

- **Spec coverage:** design §1.1 (multi-tenancy: per-tenant secret name distinct from optin's, per-tenant Cloud SQL instance, per-tenant cache with the same lock fix optin needed) — fully covered for the data-access layer. Plan 06 (token provider) and Plan 07 (CERC client) still need `financiador_id` threaded through, covered in their own plans.
- **Placeholder scan:** none — every step has runnable code, and Task 2's retrofit explicitly says what to keep unchanged (the classes) vs. what to replace (`_create_engine` down).
- **Type consistency:** `get_tenant_config(financiador_id: str) -> dict` (Task 1) is what Task 2's `_create_engine` and Plan 06's `token_provider.py` both consume. `get_db(financiador_id: str) -> CloudSQLClient` (no longer `Optional`) is what every later plan touching the database calls — Plan 03's callers list is empty so far (no handler code exists yet), so there is nothing else to update outside this plan.

**Next:** `2026-08-24-contratos-plan-06-token-provider.md` (CERC OAuth2 token provider, multi-tenant from the start).
