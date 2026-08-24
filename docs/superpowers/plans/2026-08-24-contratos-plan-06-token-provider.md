# contratos-service — Plan 06: CERC OAuth2 Token Provider (multi-tenant) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One function, `get_cerc_token(financiador_id)`, that always returns a valid CERC access token for that tenant — fetched via OAuth2 client-credentials, cached in-process per tenant, renewed proactively before it expires, and safe under concurrent callers (single-flight per tenant, no duplicate token requests).

**Architecture:** `services/cerc/token_provider.py`. Cache/lock now `dict[str, ...]` keyed by `financiador_id` (not a single global cache — this is multi-tenant from the start, since this module was never merged single-tenant; see design §1.1). `client_id`/`client_secret` come from `shared.tenant_config.get_tenant_config(financiador_id)`; `CERC_AUTH_URL` stays a plain global env var (the CERC OAuth host doesn't vary by tenant). Copied verbatim from `ap-back-optin/optin/services/cerc/token_provider.py`'s already-retrofitted multi-tenant version.

**Tech Stack:** httpx, threading (stdlib), pytest, respx (mocks the CERC token endpoint — no real network calls or real CERC credentials needed to pass these tests).

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§1.1, §4). Series: plan 6 of ~11.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout, `httpx`/`respx` in requirements); `2026-08-24-contratos-plan-05-multitenancy-foundation.md` (`get_tenant_config()`, the `TENANT_{financiador_id}_CONFIG_CONTRATOS` secret shape).

## Global Constraints

- The access token and `client_secret` are **never** logged in plaintext (SPEC-02 §3, design §4).
- Renewal is proactive at **80% of `expires_in`** — a call arriving after that point always triggers a fresh fetch, never returns a token close to expiry (design §4).
- Concurrent callers during a cold/expired cache for the **same tenant** must **not** each fire their own token request — exactly one HTTP call happens per renewal, the rest wait and reuse its result (single-flight per tenant, design §4). Two *different* tenants must never share a cache entry or block on each other's lock.
- On `401` from a downstream CERC API call, the caller (Plan 07's `services/cerc/client.py`) invalidates that tenant's cache via `invalidate_token(financiador_id)` and retries the original call once — this plan only provides the invalidation hook, the retry-once behavior itself lives in Plan 07.
- `client_id`/`client_secret` come from `get_tenant_config(financiador_id)`, never from a bare `CERC_CLIENT_ID`/`CERC_CLIENT_SECRET` env var (those no longer exist after Plan 05's `.env` consolidation).

---

### Task 1: `services/cerc/token_provider.py`

**Files:**
- Create: `contratos/services/__init__.py`
- Create: `contratos/services/cerc/__init__.py`
- Create: `contratos/services/cerc/token_provider.py`
- Test: `contratos/services/cerc/tests/__init__.py`
- Test: `contratos/services/cerc/tests/test_token_provider.py`

**Interfaces:**
- Consumes: `CERC_AUTH_URL` (env var, global); `shared.tenant_config.get_tenant_config(financiador_id)` for `cerc_client_id`/`cerc_client_secret`.
- Produces: `get_cerc_token(financiador_id: str) -> str`; `invalidate_token(financiador_id: str) -> None`. Plan 07's `services/cerc/client.py` imports both.

- [ ] **Step 1: Write the failing test**

```python
# contratos/services/cerc/tests/test_token_provider.py
import json
import threading

import httpx
import pytest
import respx

from services.cerc import token_provider

FINANCIADOR_TESTE = "12345678000199"


@pytest.fixture(autouse=True)
def _reset_cache_and_env(monkeypatch):
    monkeypatch.setenv("CERC_AUTH_URL", "https://api.int.cerc.com/oauth/token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv(f"TENANT_{FINANCIADOR_TESTE}_CONFIG_CONTRATOS", json.dumps({
        "cerc_client_id": "client-123",
        "cerc_client_secret": "segredo-local",
    }))
    token_provider._caches.clear()
    token_provider._locks.clear()

    import shared.tenant_config as tenant_config_module
    tenant_config_module._cache.clear()
    yield
    tenant_config_module._cache.clear()


@respx.mock
def test_get_cerc_token_fetches_and_caches():
    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )

    token = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    assert token == "tok-1"
    assert route.call_count == 1

    token_again = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    assert token_again == "tok-1"
    assert route.call_count == 1  # cached, no second call


@respx.mock
def test_get_cerc_token_refetches_after_80_percent_expiry():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    token_provider.get_cerc_token(FINANCIADOR_TESTE)

    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600})
    )
    calls_before = route.call_count
    token_provider._caches[FINANCIADOR_TESTE]["expires_at"] = 0.0  # simula 80% de expires_in decorrido

    token = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    assert token == "tok-2"
    assert route.call_count == calls_before + 1


@respx.mock
def test_get_cerc_token_single_flight_under_concurrency():
    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )

    results = []

    def _call():
        results.append(token_provider.get_cerc_token(FINANCIADOR_TESTE))

    threads = [threading.Thread(target=_call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["tok-1"] * 10
    assert route.call_count == 1


@respx.mock
def test_invalidate_token_forces_refetch():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    token_provider.get_cerc_token(FINANCIADOR_TESTE)

    token_provider.invalidate_token(FINANCIADOR_TESTE)

    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600})
    )
    calls_before = route.call_count
    assert token_provider.get_cerc_token(FINANCIADOR_TESTE) == "tok-2"
    assert route.call_count == calls_before + 1


@respx.mock
def test_get_cerc_token_isola_cache_entre_tenants(monkeypatch):
    monkeypatch.setenv("TENANT_99999999000191_CONFIG_CONTRATOS", json.dumps({
        "cerc_client_id": "client-999",
        "cerc_client_secret": "outro-segredo",
    }))
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-tenant-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-tenant-2", "expires_in": 3600}),
        ]
    )

    token1 = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    token2 = token_provider.get_cerc_token("99999999000191")

    assert token1 == "tok-tenant-1"
    assert token2 == "tok-tenant-2"
```

Note the `calls_before = route.call_count` pattern in two of these tests, instead of asserting `route.call_count == 1` directly: `respx` keeps counting on the **same** `Route` object across two `respx.post(...)` registrations for the same URL/method within one `@respx.mock` block, so a freshly-registered mock's `call_count` is not reset to 0 — it starts wherever the previous registration's count left off. Asserting `== calls_before + 1` is what actually verifies "exactly one new call happened," and is copied verbatim from `ap-back-optin`'s real (already-passing) test for this exact reason — don't simplify it back to `== 1`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/cerc/tests/test_token_provider.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.cerc.token_provider'`

- [ ] **Step 3: Write `services/__init__.py`, `services/cerc/__init__.py`, `services/cerc/tests/__init__.py`**

Empty files, all three.

- [ ] **Step 4: Write `services/cerc/token_provider.py`**

```python
"""OAuth2 client-credentials — obtém e cacheia o access token da CERC, por
tenant (financiador).

Cache em memória por processo, uma entrada por financiador_id. Renovação
proativa a 80% de expires_in (uma chamada depois desse ponto sempre busca
um token novo, nunca devolve um perto de vencer). Single-flight por tenant
via threading.Lock com double-checked locking: o caminho comum (token em
cache, ainda válido) nunca bloqueia; só quem chega com o cache frio/vencido
disputa o lock daquele tenant, e só um deles de fato faz a chamada HTTP.

client_id/client_secret vêm de shared.tenant_config.get_tenant_config —
CERC_AUTH_URL continua env var global (host do ambiente CERC, não varia
por tenant). Ver docs/superpowers/specs/2026-08-24-contratos-service-design.md §1.1.

Em 401 numa chamada à API da CERC, quem fez a chamada (services/cerc/client.py,
Plano 07) invalida o cache daquele tenant com invalidate_token(financiador_id)
e tenta de novo uma única vez — o retry em si não é responsabilidade deste
módulo.
"""

import os
import threading
import time

import httpx

from shared.tenant_config import get_tenant_config

_meta_lock = threading.Lock()
_locks: dict = {}
_caches: dict = {}


def _lock_for(financiador_id: str) -> threading.Lock:
    if financiador_id not in _locks:
        with _meta_lock:
            if financiador_id not in _locks:
                _locks[financiador_id] = threading.Lock()
    return _locks[financiador_id]


def _fetch_token(financiador_id: str) -> dict:
    config = get_tenant_config(financiador_id)
    response = httpx.post(
        os.environ["CERC_AUTH_URL"],
        data={
            "grant_type": "client_credentials",
            "client_id": config["cerc_client_id"],
            "client_secret": config["cerc_client_secret"],
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


def get_cerc_token(financiador_id: str) -> str:
    now = time.time()
    cache = _caches.get(financiador_id)
    if cache and cache["access_token"] and now < cache["expires_at"]:
        return cache["access_token"]

    with _lock_for(financiador_id):
        now = time.time()
        cache = _caches.get(financiador_id)
        if cache and cache["access_token"] and now < cache["expires_at"]:
            return cache["access_token"]

        payload = _fetch_token(financiador_id)
        _caches[financiador_id] = {
            "access_token": payload["access_token"],
            "expires_at": now + 0.8 * payload["expires_in"],
        }
        return _caches[financiador_id]["access_token"]


def invalidate_token(financiador_id: str) -> None:
    with _lock_for(financiador_id):
        _caches.pop(financiador_id, None)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest services/cerc/tests/test_token_provider.py -v`
Expected: PASS (5 tests) — no real CERC credentials or network access needed, `respx` mocks the HTTP call.

- [ ] **Step 6: Commit**

```bash
git add services/__init__.py services/cerc/__init__.py services/cerc/token_provider.py services/cerc/tests/__init__.py services/cerc/tests/test_token_provider.py
git commit -m "feat: CERC OAuth2 token provider, per tenant (cache, 80% renewal, single-flight)"
```

---

## Self-Review Notes

- **Spec coverage:** design §1.1 (per-tenant cache/lock, config via `get_tenant_config`) and §4 (80% renewal, single-flight, invalidate-on-401 hook) — fully covered. The retry-once-on-401 behavior itself is explicitly deferred to Plan 07, which is the actual caller of the CERC API.
- **Placeholder scan:** none — every step has runnable code, tests mock the token endpoint with `respx` (no real network/credentials needed to pass).
- **Type consistency:** `get_cerc_token(financiador_id: str) -> str` and `invalidate_token(financiador_id: str) -> None` are the exact names Plan 07 imports — matching `get_db(financiador_id)`'s parameter shape from Plan 05.

**Next:** `2026-08-24-contratos-plan-07-cerc-client.md` (CERC REST client — criar/atualizar/inativar/baixar/consultar contrato, `financiador_id` as first parameter from the start).
