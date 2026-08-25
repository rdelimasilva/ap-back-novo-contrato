# contratos-service — Plan 10: Webhook Receiver — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /api/v1/webhooks/contrato/<financiador_id>` — the thin ingest endpoint that receives the CERC webhook (`tipoEvento = contrato`), persists it verbatim to `webhook_inbox` (already in `sql/schema/01-contratos-schema.sql`, unchanged by this plan), and publishes a pointer to it on Pub/Sub for async processing. This plan does **not** apply the state machine or touch `contrato`/`garantia_ur` — that consumption step is Plan 11 (see "Next" at the end).

**Architecture:** The handler is intentionally dumb — authenticate, parse the envelope, hash it for dedup, insert, publish, respond. Two new modules carry the two pieces of real logic so they're unit-testable without HTTP or a live Pub/Sub: `apps/contratos/webhook_dedupe.py` (pure function, canonical hash) and `shared/pubsub_client.py` (thin wrapper around `google.cloud.pubsub_v1`, with the real client behind a swappable module-level function so tests never touch GCP). The view module (`apps/contratos/views.py`) wires them together.

Two architecture decisions this plan locks in, because they aren't obvious from the spec text alone:

1. **Tenant routing via URL path, not payload.** SPEC-02 §5.2 lists the webhook `evento` fields for `tipoEvento=contrato` (`referenciaExterna`, `protocolo`, `status`, ...) — none of them is `cnpjParticipante`/`financiador_id`. Since this service is one-Cloud-SQL-per-tenant (design doc §1.1) and `webhook_inbox` has no `financiador_id` column (it lives inside the tenant's own database), the handler must know *which* tenant's database to write into **before** it can even look at the envelope. The only place that information can come from is the URL itself: each tenant gets its own webhook URL, `.../webhooks/contrato/{financiador_id}`, registered individually with the CERC for that participante. This is a real operational consequence: onboarding a new tenant means registering a new URL with the CERC, not just adding a secret.
2. **Response code per failure mode, not a blanket 2xx.** SPEC-02 §5.3 / SPEC-01 §4.4 requires "2xx, always" for the cases where **we have safely captured the event** — that's the whole point of "persist before processing" (losing an event is irreversible; the CERC gives at most 5 attempts). It does **not** mean "always respond 2xx no matter what happened." If the row genuinely fails to persist (a transient Cloud SQL error, not a duplicate), the correct behavior is to let the CERC retry — i.e. respond 5xx — since that retry is our only safety net for that failure. So: bad Basic Auth → `401`; a body that isn't valid JSON or is missing envelope fields → `400` (retrying won't fix a malformed body, the CERC's own envelope is well-formed by contract); a duplicate delivery (same `hash_dedupe` already stored) → `202` (already safely captured, no-op); a successful insert (whether or not the Pub/Sub publish afterward succeeds) → `202`; any other insert failure → `500` (not captured — let the CERC retry).

**Tech Stack:** Django function-based view (no DRF ViewSet, per design doc §2), `google-cloud-pubsub` (new dependency), stdlib `hashlib`/`json`/`base64`/`datetime`, `python-ulid` (already a dependency, unused until now) for `webhook_inbox.id`.

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` §6 ("Webhook CERC → Pub/Sub"). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §5.2 (envelope) and §5.3 (receiver requirements, "iguais à SPEC 01 §4.4"); `SPEC-01-optin-e-gestao.md` §4.4 (the receiver requirements table and the dedup rule, since SPEC-02 just points at SPEC-01 for this). Series: plan 10 of ~12.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout), `2026-08-24-contratos-plan-04-secrets.md` (`shared/secrets.get_secret`), the multi-tenancy plans (`shared/tenant_config.get_tenant_config`, `shared/cloudsql_client.get_db`). Does **not** depend on Plan 09 (`state_machine.py`) — this plan never decides a contract's next state, it only queues the raw event; Plan 11 is the one that imports `state_machine`.

## Global Constraints

- `webhook_inbox` schema (already applied, `sql/schema/01-contratos-schema.sql:99-108`) is fixed by this plan: `id TEXT PRIMARY KEY`, `tipo_evento TEXT NOT NULL`, `data_hora_evento TIMESTAMPTZ NOT NULL`, `payload JSONB NOT NULL`, `hash_dedupe TEXT NOT NULL UNIQUE`, `processado_em TIMESTAMPTZ` (left `NULL` by this plan — Plan 11 sets it), `erro TEXT`, `recebido_em TIMESTAMPTZ NOT NULL DEFAULT now()`.
- Receiver requirements (SPEC-01 §4.4, reused verbatim by SPEC-02 §5.3): method `POST`; CERC retries up to **5** times on anything outside `200–299`; must sustain **500 req/s** (not verified by this plan's tests — a load-test concern for certification, tracked in SPEC-02 §13.3's "Definição de pronto"); dedup key is `(tipoEvento, hash canônico do evento, dataHoraEvento)`; the handler must persist to `webhook_inbox` **before** any processing — this plan performs zero business-logic processing, so that ordering is trivially satisfied.
- No Django ORM (`DATABASES = {}`) — all reads/writes go through `shared.cloudsql_client.get_db(financiador_id)` (design §1).
- `financiador_id` is always the first thing resolved in this flow (from the URL), never derived from the webhook body.

---

### Task 1: `shared/pubsub_client.py` — publish helper

**Files:**
- Create: `contratos/shared/pubsub_client.py`
- Test: `contratos/shared/tests/test_pubsub_client.py`
- Modify: `contratos/requirements.txt` (add `google-cloud-pubsub`)

**Interfaces:**
- Produces: `publish_webhook_contrato(webhook_inbox_id: str, financiador_id: str) -> None` — never raises (publish is best-effort; a failed publish is recovered later by Plan 11's sweep over `processado_em IS NULL`, per design §6). Task 3 imports this.
- Internal (overridden by tests via `monkeypatch.setattr`): `_get_publisher()` returns the lazily-constructed `google.cloud.pubsub_v1.PublisherClient()` singleton.

- [ ] **Step 1: Add the dependency**

Add to `requirements.txt` (after the `google-cloud-secret-manager` line):

```
google-cloud-pubsub
```

Run: `pip install google-cloud-pubsub` then `python -c "from google.cloud import pubsub_v1"` — expected: no output, no error.

- [ ] **Step 2: Write the failing test**

```python
# contratos/shared/tests/test_pubsub_client.py
import json

import pytest

import shared.pubsub_client as pubsub_client


class _FakeFuture:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def add_done_callback(self, callback):
        callback(self)

    def result(self):
        if self._error:
            raise self._error
        return self._result


class _FakePublisher:
    def __init__(self, future):
        self._future = future
        self.calls = []

    def publish(self, topic, data):
        self.calls.append((topic, data))
        return self._future


@pytest.fixture(autouse=True)
def _reset_publisher_singleton():
    pubsub_client._publisher = None
    yield
    pubsub_client._publisher = None


def test_publish_webhook_contrato_sends_ids_as_json_to_default_topic(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "registradora-506000")
    monkeypatch.delenv("PUBSUB_TOPIC_CONTRATOS_WEBHOOK", raising=False)
    fake = _FakePublisher(_FakeFuture(result="msg-1"))
    monkeypatch.setattr(pubsub_client, "_get_publisher", lambda: fake)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")

    assert len(fake.calls) == 1
    topic, data = fake.calls[0]
    assert topic == "projects/registradora-506000/topics/contratos-webhook-inbox"
    assert json.loads(data) == {"webhook_inbox_id": "01ABC", "financiador_id": "12345678000199"}


def test_publish_webhook_contrato_respects_custom_topic_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "registradora-506000")
    monkeypatch.setenv("PUBSUB_TOPIC_CONTRATOS_WEBHOOK", "topico-customizado")
    fake = _FakePublisher(_FakeFuture(result="msg-1"))
    monkeypatch.setattr(pubsub_client, "_get_publisher", lambda: fake)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")

    assert fake.calls[0][0] == "projects/registradora-506000/topics/topico-customizado"


def test_publish_webhook_contrato_nao_levanta_quando_publish_falha_de_forma_assincrona(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "registradora-506000")
    fake = _FakePublisher(_FakeFuture(error=RuntimeError("indisponível")))
    monkeypatch.setattr(pubsub_client, "_get_publisher", lambda: fake)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")  # não deve levantar


def test_publish_webhook_contrato_nao_levanta_quando_google_cloud_project_ausente(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")  # não deve levantar
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest shared/tests/test_pubsub_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.pubsub_client'`

- [ ] **Step 4: Write `shared/pubsub_client.py`**

```python
"""Publish no tópico de webhook_inbox — design §6.

O handler HTTP (apps/contratos/views.py) já gravou o evento cru em
webhook_inbox ANTES de chamar isto. Publicar aqui é melhor-esforço: se
falhar (rede, projeto GCP não configurado em dev, o que for), a linha já
persistida continua lá com processado_em IS NULL, e o job de varredura do
Plano 11 a recupera. Por isso esta função nunca deixa uma exceção escapar
— ela só loga.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_publisher = None


def _get_publisher():
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1

        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def _topic_path() -> str:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    topic = os.getenv("PUBSUB_TOPIC_CONTRATOS_WEBHOOK", "contratos-webhook-inbox")
    return f"projects/{project}/topics/{topic}"


def publish_webhook_contrato(webhook_inbox_id: str, financiador_id: str) -> None:
    """Publica só os IDs (não o payload) — o consumidor (Plano 11) busca o
    evento completo em webhook_inbox pelo id; a mensagem em si fica pequena
    e o payload nunca vive em dois lugares."""
    try:
        data = json.dumps({
            "webhook_inbox_id": webhook_inbox_id,
            "financiador_id": financiador_id,
        }).encode("utf-8")
        future = _get_publisher().publish(_topic_path(), data)
    except Exception:
        logger.exception("[Pub/Sub] Falha ao publicar webhook_inbox_id=%s", webhook_inbox_id)
        return
    future.add_done_callback(lambda f: _log_publish_result(f, webhook_inbox_id))


def _log_publish_result(future, webhook_inbox_id: str) -> None:
    try:
        future.result()
    except Exception:
        logger.exception("[Pub/Sub] Publish assíncrono falhou webhook_inbox_id=%s", webhook_inbox_id)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest shared/tests/test_pubsub_client.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add shared/pubsub_client.py shared/tests/test_pubsub_client.py requirements.txt
git commit -m "feat: Pub/Sub publish helper for webhook_inbox (SPEC-02 design §6)"
```

---

### Task 2: `apps/contratos/webhook_dedupe.py` — canonical dedup hash

**Files:**
- Create: `contratos/apps/contratos/webhook_dedupe.py`
- Test: `contratos/apps/contratos/tests/test_webhook_dedupe.py`

**Interfaces:**
- Produces: `hash_evento(tipo_evento: str, evento: dict, data_hora_evento: str) -> str` (64-char hex sha256 digest). Task 3 imports this.

- [ ] **Step 1: Write the failing test**

```python
# contratos/apps/contratos/tests/test_webhook_dedupe.py
from apps.contratos.webhook_dedupe import hash_evento


def test_hash_evento_e_deterministico():
    evento = {"referenciaExterna": "CTR-1", "protocolo": "P-1", "status": "0"}
    assert hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z") == \
        hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z")


def test_hash_evento_tem_64_caracteres_hex():
    h = hash_evento("contrato", {"a": 1}, "2026-08-17T12:00:00.000Z")
    assert len(h) == 64
    int(h, 16)  # não levanta — é hex válido


def test_hash_evento_ignora_ordem_das_chaves():
    e1 = {"a": 1, "b": 2}
    e2 = {"b": 2, "a": 1}
    assert hash_evento("contrato", e1, "2026-08-17T12:00:00.000Z") == \
        hash_evento("contrato", e2, "2026-08-17T12:00:00.000Z")


def test_hash_evento_muda_com_tipo_evento_diferente():
    evento = {"a": 1}
    assert hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z") != \
        hash_evento("efeitoContrato", evento, "2026-08-17T12:00:00.000Z")


def test_hash_evento_muda_com_data_hora_diferente():
    evento = {"a": 1}
    assert hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z") != \
        hash_evento("contrato", evento, "2026-08-17T12:00:01.000Z")


def test_hash_evento_muda_com_conteudo_diferente():
    assert hash_evento("contrato", {"a": 1}, "2026-08-17T12:00:00.000Z") != \
        hash_evento("contrato", {"a": 2}, "2026-08-17T12:00:00.000Z")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_webhook_dedupe.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.contratos.webhook_dedupe'`

- [ ] **Step 3: Write `apps/contratos/webhook_dedupe.py`**

```python
"""Hash de deduplicação de webhooks — SPEC-01 §4.4, reaproveitado pela
SPEC-02 §5.3: dedupe por (tipoEvento, hash canônico do evento,
dataHoraEvento). A CERC reentrega o mesmo evento em até 5 tentativas
quando não recebe 2xx; reentrega deve ser inofensiva."""

import hashlib
import json


def hash_evento(tipo_evento: str, evento: dict, data_hora_evento: str) -> str:
    canonico = json.dumps(evento, sort_keys=True, ensure_ascii=False, default=str)
    chave = f"{tipo_evento}|{data_hora_evento}|{canonico}"
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apps/contratos/tests/test_webhook_dedupe.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add apps/contratos/webhook_dedupe.py apps/contratos/tests/test_webhook_dedupe.py
git commit -m "feat: canonical webhook dedup hash (SPEC-01 §4.4 / SPEC-02 §5.3)"
```

---

### Task 3: `POST /api/v1/webhooks/contrato/<financiador_id>` — the receiver view

**Files:**
- Modify: `contratos/apps/contratos/views.py`
- Modify: `contratos/apps/contratos/urls.py`
- Modify: `contratos/shared/tenant_config.py` (docstring: document the two new config keys)
- Modify: `contratos/.env.example` (document the two new keys in the tenant JSON example)
- Modify: `contratos/.env` (local, not committed — add `webhook_basic_user`/`webhook_basic_password` to the dev tenant's JSON so this task's tests can authenticate)
- Test: `contratos/apps/contratos/tests/test_views_webhook.py`

**Interfaces:**
- Consumes: `apps.contratos.webhook_dedupe.hash_evento` (Task 2), `shared.pubsub_client.publish_webhook_contrato` (Task 1, imported as a module — `from shared import pubsub_client` — so tests can `monkeypatch.setattr(pubsub_client, "publish_webhook_contrato", ...)`), `shared.cloudsql_client.get_db`, `shared.tenant_config.get_tenant_config`.
- Produces: view function `webhook_contrato(request, financiador_id: str)`, routed at `webhooks/contrato/<str:financiador_id>` under the existing `apps.contratos.urls` (mounted at `/api/v1/` by `config/urls.py`). Tenant config contract gains two keys: `webhook_basic_user`, `webhook_basic_password`.

- [ ] **Step 1: Extend the tenant config contract (docs only, no code change)**

`get_tenant_config` (`shared/tenant_config.py`) already returns whatever JSON is in the secret verbatim — no parsing/validation to change. Update its docstring's "Chaves esperadas" list to add `webhook_basic_user, webhook_basic_password` (credentials this service expects the CERC to send via HTTP Basic Auth when calling our webhook — chosen because SPEC-01 §4.4 allows either OAuth2 or Basic for the CERC→us direction, and Basic needs no token-issuing infrastructure on our side).

In `.env.example`, update the `TENANT_12345678000199_CONFIG_CONTRATOS` line to include the two new keys (empty string values, same as the others).

In your local `.env`, add the same two keys to the tenant JSON with real dev values (e.g. generate a random password) — required for Task 3's tests, which authenticate against this tenant's real config the same way `services/cerc/tests/test_client.py` hits the real dev database.

- [ ] **Step 2: Write the failing tests**

```python
# contratos/apps/contratos/tests/test_views_webhook.py
import base64
import json

import pytest
from django.test import Client

from apps.contratos.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

FINANCIADOR_TESTE = "12345678000199"
URL = f"/api/v1/webhooks/contrato/{FINANCIADOR_TESTE}"


def _basic_auth_header():
    config = get_tenant_config(FINANCIADOR_TESTE)
    credenciais = f"{config['webhook_basic_user']}:{config['webhook_basic_password']}"
    return "Basic " + base64.b64encode(credenciais.encode()).decode()


def _envelope(referencia, data_hora="2026-08-17T12:00:00.000Z"):
    return {
        "tipoEvento": "contrato",
        "dataHoraEvento": data_hora,
        "evento": {"referenciaExterna": referencia, "protocolo": "proto-1", "status": "0"},
    }


def _limpar(envelope):
    h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
    get_db(FINANCIADOR_TESTE).table("webhook_inbox").delete().eq("hash_dedupe", h).execute()


@pytest.fixture
def publicados(monkeypatch):
    chamadas = []
    monkeypatch.setattr(pubsub_client, "publish_webhook_contrato", lambda *a, **k: chamadas.append(a))
    return chamadas


def test_webhook_sem_autenticacao_retorna_401(publicados):
    response = Client().post(URL, data=json.dumps(_envelope("CTR-TESTE-WEBHOOK-NOAUTH")), content_type="application/json")
    assert response.status_code == 401
    assert publicados == []


def test_webhook_com_credenciais_erradas_retorna_401(publicados):
    response = Client().post(
        URL, data=json.dumps(_envelope("CTR-TESTE-WEBHOOK-BADAUTH")), content_type="application/json",
        HTTP_AUTHORIZATION="Basic " + base64.b64encode(b"errado:errado").decode(),
    )
    assert response.status_code == 401
    assert publicados == []


def test_webhook_tenant_desconhecido_retorna_401(publicados):
    response = Client().post(
        "/api/v1/webhooks/contrato/00000000000000",
        data=json.dumps(_envelope("CTR-TESTE-WEBHOOK-TENANT")), content_type="application/json",
        HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 401


def test_webhook_get_retorna_405():
    response = Client().get(URL)
    assert response.status_code == 405


def test_webhook_corpo_nao_json_retorna_400():
    response = Client().post(
        URL, data="isto nao e json", content_type="text/plain", HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 400


def test_webhook_envelope_sem_campos_obrigatorios_retorna_400():
    response = Client().post(
        URL, data=json.dumps({"tipoEvento": "contrato"}), content_type="application/json",
        HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 400


def test_webhook_valido_persiste_no_inbox_e_publica(publicados):
    envelope = _envelope("CTR-TESTE-WEBHOOK-OK")
    _limpar(envelope)
    try:
        response = Client().post(
            URL, data=json.dumps(envelope), content_type="application/json",
            HTTP_AUTHORIZATION=_basic_auth_header(),
        )
        assert response.status_code == 202

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvo = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvo.data) == 1
        assert salvo.data[0]["tipo_evento"] == "contrato"
        assert salvo.data[0]["payload"] == envelope
        assert salvo.data[0]["processado_em"] is None

        assert len(publicados) == 1
        assert publicados[0][1] == FINANCIADOR_TESTE
    finally:
        _limpar(envelope)


def test_webhook_duplicado_nao_gera_segunda_linha_nem_publica_de_novo(publicados):
    envelope = _envelope("CTR-TESTE-WEBHOOK-DUP")
    _limpar(envelope)
    try:
        cliente = Client()
        r1 = cliente.post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())
        r2 = cliente.post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())

        assert r1.status_code == 202
        assert r2.status_code == 202

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvos = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvos.data) == 1
        assert len(publicados) == 1
    finally:
        _limpar(envelope)


def test_webhook_responde_202_mesmo_quando_publish_falha(monkeypatch):
    envelope = _envelope("CTR-TESTE-WEBHOOK-PUBFAIL")
    _limpar(envelope)

    def _falha(*args, **kwargs):
        raise RuntimeError("Pub/Sub indisponível")

    monkeypatch.setattr(pubsub_client, "publish_webhook_contrato", _falha)
    try:
        response = Client().post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())
        assert response.status_code == 500
    finally:
        _limpar(envelope)
```

Note on the last test: it deliberately makes `publish_webhook_contrato` raise (simulating a bug in the publish call site itself, not the best-effort internal failure Task 1 already swallows) to prove the view doesn't let a publish-side exception corrupt the "row is safely persisted" success response — see Step 3's `try/except` placement.

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_views_webhook.py -v`
Expected: FAIL — `django.urls.exceptions.NoReverseMatch`-style 404s / `AttributeError: module 'apps.contratos.views' has no attribute 'webhook_contrato'` (route and view don't exist yet).

- [ ] **Step 4: Write `apps/contratos/views.py`**

```python
import base64
import json
import logging
from datetime import datetime

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from sqlalchemy.exc import IntegrityError
from ulid import ULID

from apps.contratos.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)


def health(request):
    return JsonResponse({"status": "ok"})


def _autenticado(request, financiador_id: str) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return False
    try:
        decodificado = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        # Header vindo de fora (a CERC ou qualquer chamador não confiável) —
        # base64/utf-8 inválido é "não autenticado", não um bug nosso.
        return False
    usuario, _, senha = decodificado.partition(":")

    try:
        config = get_tenant_config(financiador_id)
    except RuntimeError:
        # financiador_id sem segredo configurado — trata como credencial
        # inválida, não como 404/500 (não vazamos se o tenant existe).
        return False
    return usuario == config.get("webhook_basic_user") and senha == config.get("webhook_basic_password")


@require_POST
def webhook_contrato(request, financiador_id: str):
    """Receptor do webhook CERC (tipoEvento=contrato) — SPEC-02 §5.2/§5.3.

    Fino por design: autentica, grava em webhook_inbox, publica no
    Pub/Sub e responde. Nenhuma transição de estado acontece aqui — isso é
    do consumidor (Plano 11), que lê o Pub/Sub e importa state_machine.
    """
    if not _autenticado(request, financiador_id):
        return JsonResponse({"erro": "autenticação inválida"}, status=401)

    try:
        envelope = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)

    tipo_evento = envelope.get("tipoEvento") if isinstance(envelope, dict) else None
    data_hora_evento = envelope.get("dataHoraEvento") if isinstance(envelope, dict) else None
    evento = envelope.get("evento") if isinstance(envelope, dict) else None
    if not tipo_evento or not data_hora_evento or evento is None:
        return JsonResponse(
            {"erro": "envelope inválido: tipoEvento, dataHoraEvento e evento são obrigatórios"}, status=400,
        )

    hash_dedupe = hash_evento(tipo_evento, evento, data_hora_evento)
    webhook_id = str(ULID())

    try:
        get_db(financiador_id).table("webhook_inbox").insert({
            "id": webhook_id,
            "tipo_evento": tipo_evento,
            "data_hora_evento": datetime.fromisoformat(data_hora_evento),
            "payload": envelope,
            "hash_dedupe": hash_dedupe,
        }).execute()
    except IntegrityError:
        # UNIQUE(hash_dedupe) — reentrega da CERC do mesmo evento. Já está
        # persistido de uma entrega anterior: inofensivo, responde 2xx.
        logger.info("[Webhook] Evento duplicado ignorado (financiador=%s, hash=%s)", financiador_id, hash_dedupe)
        return JsonResponse({}, status=202)
    except Exception:
        # Não conseguimos persistir por um motivo que NÃO é duplicidade —
        # o evento não está seguro. Responde 5xx de propósito para que a
        # CERC use uma de suas (até 5) tentativas de reentrega.
        logger.exception("[Webhook] Falha ao persistir webhook_inbox (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "falha ao persistir evento"}, status=500)

    try:
        pubsub_client.publish_webhook_contrato(webhook_id, financiador_id)
    except Exception:
        # publish_webhook_contrato já é melhor-esforço e não deveria levantar;
        # se mesmo assim levantar, a linha já está persistida — não perdemos
        # o evento, só o atraso de processamento (o Plano 11 varre por
        # processado_em IS NULL). Loga e responde sucesso normalmente.
        logger.exception("[Webhook] publish_webhook_contrato levantou inesperadamente (financiador=%s)", financiador_id)

    return JsonResponse({}, status=202)
```

Wait — the test `test_webhook_responde_202_mesmo_quando_publish_falha` above asserts `500`, but this implementation swallows the publish exception and returns `202`. Rewrite that test to match the intended (correct) behavior: a publish failure must **never** turn a persisted event into an error response, since the row is already safely stored. Fix the test in Step 2 before proceeding:

```python
def test_webhook_responde_202_mesmo_quando_publish_falha(monkeypatch):
    envelope = _envelope("CTR-TESTE-WEBHOOK-PUBFAIL")
    _limpar(envelope)

    def _falha(*args, **kwargs):
        raise RuntimeError("Pub/Sub indisponível")

    monkeypatch.setattr(pubsub_client, "publish_webhook_contrato", _falha)
    try:
        response = Client().post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())
        assert response.status_code == 202  # linha já persistida — publish é melhor-esforço, não derruba a resposta

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvo = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvo.data) == 1  # persistiu mesmo com o publish quebrado
    finally:
        _limpar(envelope)
```

- [ ] **Step 5: Wire the URL**

```python
# contratos/apps/contratos/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path("health", views.health),
    path("webhooks/contrato/<str:financiador_id>", views.webhook_contrato),
]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest apps/contratos/tests/test_views_webhook.py -v`
Expected: PASS (9 tests)

- [ ] **Step 7: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass (existing suites untouched).

- [ ] **Step 8: Commit**

```bash
git add apps/contratos/views.py apps/contratos/urls.py apps/contratos/tests/test_views_webhook.py \
        shared/tenant_config.py .env.example
git commit -m "feat: CERC contract webhook receiver (SPEC-02 §5.2/§5.3) — ingest to webhook_inbox + Pub/Sub"
```

(`.env` is gitignored and not part of this commit — it's local-only setup from Step 1.)

---

## Self-Review Notes

- **Spec coverage:** SPEC-01 §4.4 requirements table — method (`@require_POST`), auth (Basic, chosen and documented), 2xx-on-success (`202`), dedup key `(tipoEvento, hash, dataHoraEvento)` (`webhook_dedupe.hash_evento`), persist-before-process (insert happens before the only other side effect, publish) — all covered. SPEC-02 §5.2 envelope fields (`tipoEvento`, `dataHoraEvento`, `evento`) validated for presence. Design §6 ("grava em webhook_inbox antes de publicar... se o publish falhar, job de varredura recupera") covered by the publish-failure test. Out of scope by design (Plan 11): applying `state_machine`, updating `contrato`/`garantia_ur`, the reconciliation SLA sweep, OIDC-verified push endpoint.
- **Placeholder scan:** none — every branch (401 no-header / bad-credentials / unknown-tenant, 400 bad-JSON / missing-fields, 202 success / duplicate / publish-failure, 500 genuine-persist-failure, 405 wrong-method) has a real test.
- **Type consistency:** `hash_evento(tipo_evento: str, evento: dict, data_hora_evento: str) -> str` (Task 2) is called identically in the view (Task 3) and in every test's `_limpar`/assertions. `publish_webhook_contrato(webhook_inbox_id: str, financiador_id: str) -> None` (Task 1) is called with the same positional order and imported as a module (`from shared import pubsub_client`) everywhere, which is what makes `monkeypatch.setattr(pubsub_client, "publish_webhook_contrato", ...)` work in Task 3's tests.

**Next:** `2026-08-25-contratos-plan-11-webhook-processor.md` (Pub/Sub push subscription consumer — `POST /api/v1/webhooks/contrato/processar` verified by OIDC, reads the `webhook_inbox` row by id, applies `apps.contratos.state_machine`, updates `contrato`/`garantia_ur`/`indicador_consistencia`/`contrato_evento`, marks `processado_em`; then the `reconciliar_pendentes` SLA sweep management command).
