# contratos-service — Plan 07: CERC REST Client — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `services/cerc/client.py` — the only code in this service that calls the CERC HTTP API for contract operations. Five functions: `criar_contrato`, `atualizar_contrato`, `inativar_contrato`, `baixar_contrato` (all `PUT /v15/contratos`, differentiated by `tipoOperacao` in the payload — SPEC-02 §4), and `consultar_contrato` (`POST /contrato/consultar`, SPEC-02 §6.1). Every call logs to `cerc_requisicao` before interpreting the response, and retries once on `401` after invalidating the tenant's cached token.

**Architecture:** `financiador_id` is the first parameter of every public function from the start (no retrofit needed — this module is written after Plan 05/06 established the multi-tenant foundation, unlike `ap-back-optin` which had to retrofit an already-shipped `client.py`). Used to: (a) fetch that tenant's token via `services/cerc/token_provider.get_cerc_token` (Plan 06), (b) log to `cerc_requisicao` in that tenant's own database via `shared/cloudsql_client.get_db` (Plan 05). Copied and adapted from `ap-back-optin/optin/services/cerc/client.py`'s already-multi-tenant version — same transport shape (log-before-raise, 401-retry-once), different CERC resource (`/v15/contratos` + `/contrato/consultar` instead of `/opt_in` + `/opt_out`).

**Tech Stack:** httpx, pytest, respx (mocks both the CERC token endpoint and the contracts endpoint — no real network/credentials needed).

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§1.1, §4). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §4 (`PUT /v15/contratos` request/response), §5 (síncrona 207), §6.1 (`POST /contrato/consultar`). Series: plan 7 of ~11.

**Depends on:** `2026-08-24-contratos-plan-02-schema.md` (`cerc_requisicao` table); `2026-08-24-contratos-plan-05-multitenancy-foundation.md` (`get_db(financiador_id)`); `2026-08-24-contratos-plan-06-token-provider.md` (`get_cerc_token`/`invalidate_token`).

## Global Constraints

- Every call to the CERC API writes one row to `cerc_requisicao` **before** deciding whether to raise `CercApiError` — the audit trail exists even when the call ends in error (design §4, SPEC-02 §5.1 — "persistir `protocolo` e aguardar o webhook").
- On `401`, invalidate that tenant's token (`invalidate_token(financiador_id)`) and retry the exact same call **exactly once**, with a second `cerc_requisicao` row (`tentativa=2`). Never retry more than once from this layer.
- `PUT /v15/contratos` always sends an **array** body, even for a single item (SPEC-02 §4 — "Body: array de contratos") and always responds `207` (multi-status). `POST /contrato/consultar` sends a **plain object** body and responds `200`/`404`/`400` (SPEC-02 §6.1) — not a batch, not multi-status. `consultar_contrato`'s return value is therefore a single `dict`, while the other four functions return the raw `207` array (item-by-item parsing of that array is the caller's job — SPEC-02 §5.1, "nunca tratar o HTTP 207 como sucesso global" — not this module's).
- `Idempotency-Key` header carries `correlacao_id` on every request, including the query (harmless there, consistent everywhere).
- No cross-service/cross-tenant coupling: `CERC_API_BASE_URL` stays a plain global env var (the CERC host doesn't vary by tenant, same as `CERC_AUTH_URL` in Plan 06); `client_id`/`client_secret` never touched directly by this module — only through `token_provider`.

---

### Task 1: `services/cerc/client.py`

**Files:**
- Create: `contratos/services/cerc/client.py`
- Test: `contratos/services/cerc/tests/test_client.py`

**Interfaces:**
- Consumes: `services.cerc.token_provider.get_cerc_token(financiador_id)` / `invalidate_token(financiador_id)` (Plan 06); `shared.cloudsql_client.get_db(financiador_id)` (Plan 05); `CERC_API_BASE_URL` env var.
- Produces: `criar_contrato(financiador_id, payload, correlacao_id) -> list`, `atualizar_contrato(financiador_id, payload, correlacao_id) -> list`, `inativar_contrato(financiador_id, payload, correlacao_id) -> list`, `baixar_contrato(financiador_id, payload, correlacao_id) -> list`, `consultar_contrato(financiador_id, payload, correlacao_id) -> dict`, and `CercApiError(status_code, body)`. Later plans (state machine, webhook, validation-gated views) import all of these.

- [ ] **Step 1: Write the failing test**

```python
# contratos/services/cerc/tests/test_client.py
from dotenv import load_dotenv
load_dotenv()

import json
import os

import httpx
import pytest
import respx

os.environ.setdefault("CERC_AUTH_URL", "https://api.int.cerc.com/oauth/token")
os.environ.setdefault("CERC_API_BASE_URL", "https://ap-homolog.cerc.inf.br")

FINANCIADOR_TESTE = "12345678000199"

from services.cerc import client, token_provider  # noqa: E402
from shared.cloudsql_client import get_db  # noqa: E402


def _mock_token():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )


def _multistatus(protocolo="P-1", referencia="CTR-2026-000001", status="0"):
    return [
        {
            "referenciaExterna": referencia,
            "protocolo": protocolo,
            "dataHoraProcessamento": "2026-08-24T12:00:00.000Z",
            "status": status,
            "erros": [],
        }
    ]


@pytest.fixture(autouse=True)
def _reset_state():
    token_provider._caches.clear()
    token_provider._locks.clear()
    import shared.tenant_config as tenant_config_module
    tenant_config_module._cache.clear()

    db = get_db(FINANCIADOR_TESTE)
    db.table("cerc_requisicao").delete().eq("correlacao_id", "corr-1").execute()
    yield
    db.table("cerc_requisicao").delete().eq("correlacao_id", "corr-1").execute()
    tenant_config_module._cache.clear()


@respx.mock
def test_criar_contrato_sends_array_body_with_tipo_operacao_c():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus())
    )

    result = client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert result == _multistatus()
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "tipoOperacao": "C"}]

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["http_status"] == 207
    assert logged.data[0]["recurso"] == "/v15/contratos"
    assert logged.data[0]["tentativa"] == 1


@respx.mock
def test_criar_contrato_retries_once_on_401():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-expired", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-fresh", "expires_in": 3600}),
        ]
    )
    contratos_route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        side_effect=[
            httpx.Response(401, json={"erro": "token expirado"}),
            httpx.Response(207, json=_multistatus()),
        ]
    )

    result = client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert result == _multistatus()
    assert contratos_route.call_count == 2

    logged = (
        get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*")
        .eq("correlacao_id", "corr-1").order("tentativa").execute()
    )
    assert [row["tentativa"] for row in logged.data] == [1, 2]
    assert logged.data[0]["http_status"] == 401
    assert logged.data[1]["http_status"] == 207


@respx.mock
def test_criar_contrato_raises_cerc_api_error_on_4xx():
    _mock_token()
    respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(422, json={"codigo": "107807", "mensagem": "campo estático"})
    )

    with pytest.raises(client.CercApiError) as exc:
        client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert exc.value.status_code == 422
    assert exc.value.body == {"codigo": "107807", "mensagem": "campo estático"}

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["http_status"] == 422


@respx.mock
def test_criar_contrato_logs_before_raising_on_transport_failure():
    _mock_token()
    respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(httpx.ConnectError):
        client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["http_status"] is None
    assert logged.data[0]["tentativa"] == 1


@respx.mock
def test_atualizar_contrato_sends_tipo_operacao_a():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus(status="0"))
    )

    result = client.atualizar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001", "cnpjDetentor": "99999999000191"}, correlacao_id="corr-1")

    assert result[0]["status"] == "0"
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "cnpjDetentor": "99999999000191", "tipoOperacao": "A"}]


@respx.mock
def test_inativar_contrato_sends_tipo_operacao_i():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus(status="0"))
    )

    client.inativar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "tipoOperacao": "I"}]


@respx.mock
def test_baixar_contrato_sends_tipo_operacao_b():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus(status="0"))
    )

    client.baixar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "tipoOperacao": "B"}]


@respx.mock
def test_consultar_contrato_sends_plain_object_body_and_returns_dict():
    _mock_token()
    detalhe = {
        "referenciaExterna": "CTR-2026-000001",
        "identificadorContrato": "OP-88231",
        "quantidadeUnidadesRecebiveisAlcancadas": 3,
    }
    route = respx.post("https://ap-homolog.cerc.inf.br/contrato/consultar").mock(
        return_value=httpx.Response(200, json=detalhe)
    )

    result = client.consultar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert result == detalhe
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {"referenciaExterna": "CTR-2026-000001"}  # objeto puro, não array

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["recurso"] == "/contrato/consultar"


@respx.mock
def test_consultar_contrato_raises_cerc_api_error_on_404():
    _mock_token()
    respx.post("https://ap-homolog.cerc.inf.br/contrato/consultar").mock(
        return_value=httpx.Response(404, json={"codigo": "113005", "mensagem": "contrato inexistente"})
    )

    with pytest.raises(client.CercApiError) as exc:
        client.consultar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-INEXISTENTE"}, correlacao_id="corr-1")

    assert exc.value.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/cerc/tests/test_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.cerc.client'`

- [ ] **Step 3: Write `services/cerc/client.py`**

```python
"""Cliente REST da CERC — criar/atualizar/inativar/baixar/consultar contrato.

Toda chamada grava uma linha em cerc_requisicao ANTES de decidir se levanta
CercApiError (design §4) — a trilha de auditoria existe mesmo quando a
chamada termina em erro. Em 401, invalida o token do tenant (Plano 06) e
repete a mesma chamada uma única vez, com uma segunda linha de log
(tentativa=2).

SPEC-02 §4: PUT /v15/contratos recebe sempre um array (lote), mesmo para
um único item, e responde 207 multi-status (array, um item por entrada
enviada) — o parsing item-a-item do 207 é responsabilidade de quem
consome o retorno desta camada de transporte, não deste módulo. Diferente
de PUT /v15/contratos: POST /contrato/consultar (SPEC-02 §6.1) recebe um
objeto puro (não array) e responde 200 com o detalhe do contrato — logo
consultar_contrato devolve um dict, não uma lista.

Multi-tenancy: toda função pública recebe financiador_id como primeiro
parâmetro — usado para buscar o token do tenant certo
(services/cerc/token_provider.py) e gravar a auditoria em cerc_requisicao
do banco do tenant certo (shared/cloudsql_client.py). Ver
docs/superpowers/specs/2026-08-24-contratos-service-design.md §1.1.
"""

import os
import uuid

import httpx

from services.cerc.token_provider import get_cerc_token, invalidate_token
from shared.cloudsql_client import get_db


class CercApiError(Exception):
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"CERC API respondeu {status_code}: {body}")


def _log_attempt(financiador_id: str, recurso: str, correlacao_id: str, request_body, response, tentativa: int) -> None:
    get_db(financiador_id).table("cerc_requisicao").insert({
        "id": str(uuid.uuid4()),
        "recurso": recurso,
        "correlacao_id": correlacao_id,
        "http_status": response.status_code if response is not None else None,
        "request_body": request_body,
        "response_body": _safe_json(response),
        "tentativa": tentativa,
    }).execute()


def _safe_json(response):
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _send(method: str, path: str, body, correlacao_id: str, token: str) -> httpx.Response:
    url = os.environ["CERC_API_BASE_URL"] + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": correlacao_id,
    }
    return httpx.request(method, url, json=body, headers=headers, timeout=15.0)


def _request(financiador_id: str, method: str, path: str, body, correlacao_id: str):
    token = get_cerc_token(financiador_id)
    try:
        response = _send(method, path, body, correlacao_id, token)
    except httpx.HTTPError:
        _log_attempt(financiador_id, path, correlacao_id, body, None, tentativa=1)
        raise
    _log_attempt(financiador_id, path, correlacao_id, body, response, tentativa=1)

    if response.status_code == 401:
        invalidate_token(financiador_id)
        token = get_cerc_token(financiador_id)
        try:
            response = _send(method, path, body, correlacao_id, token)
        except httpx.HTTPError:
            _log_attempt(financiador_id, path, correlacao_id, body, None, tentativa=2)
            raise
        _log_attempt(financiador_id, path, correlacao_id, body, response, tentativa=2)

    if response.status_code >= 400:
        raise CercApiError(response.status_code, _safe_json(response))

    return response.json()


def criar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "C"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def atualizar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "A"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def inativar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "I"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def baixar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "B"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def consultar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> dict:
    return _request(financiador_id, "POST", "/contrato/consultar", payload, correlacao_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest services/cerc/tests/test_client.py -v`
Expected: PASS (9 tests) — against the real `contratos-db` instance for the `cerc_requisicao` audit rows (same instance Plan 02/03/05 already use), with `respx` mocking the CERC HTTP calls themselves (no real CERC credentials/network needed).

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add services/cerc/client.py services/cerc/tests/test_client.py
git commit -m "feat: CERC REST client (criar/atualizar/inativar/baixar/consultar contrato)"
```

---

## Self-Review Notes

- **Spec coverage:** SPEC-02 §4 (PUT /v15/contratos, array body, tipoOperacao per item, 207 multi-status), §5.1 (persist protocolo before webhook, never treat 207 as global success — enforced by returning the raw array untouched for the caller to parse item-by-item), §6.1 (POST /contrato/consultar, plain object body, single dict response) — fully covered for the four mutating operations plus the analytic query. Design §4 (log-before-raise, 401-retry-once) and §1.1 (financiador_id threading, no cross-tenant/cross-service coupling) — fully covered.
- **Out of scope for this plan, deliberately:** `tipoOperacao = S` (simular) and `P`/`R` (resilição) — fase 2 per design §8; `POST /v150/contrato/consultar` (consulta sintética) — also fase 2. Adding them now would be code with no caller yet (YAGNI) — see design §8 for the fase-1/fase-2 split.
- **Placeholder scan:** none — every step has runnable code and a full test suite mirroring `ap-back-optin`'s already-passing equivalent.
- **Type consistency:** `criar_contrato`/`atualizar_contrato`/`inativar_contrato`/`baixar_contrato` all return `list` (the raw 207 array); `consultar_contrato` returns `dict` (the raw 200 body) — this asymmetry is deliberate (see module docstring) and every later plan (validation-gated views, state machine) must not assume all five functions return the same shape.

**Next:** `2026-08-24-contratos-plan-08-validation.md` (local validation rules C01-C20, blocking bad requests before they ever reach this client).
