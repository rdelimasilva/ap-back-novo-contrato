# contratos-service — Plan 11: Webhook Processor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /api/v1/webhooks/contrato/processar` — the Pub/Sub push-subscription consumer that reads a `webhook_inbox` row queued by Plan 10's receiver, applies `apps.contratos.state_machine` (Plan 09) to compute the contract's next state, and persists the result to `contrato`, `garantia_ur`, `indicador_consistencia`, and `contrato_evento`. This is where SPEC-02's "coração da implementação" (§0) actually happens — Plan 10 only queued the raw event, this plan interprets it.

**Architecture:** Same two-pure-modules-plus-thin-view shape as Plan 10. `apps/contratos/webhook_processor.py` maps a webhook `evento` dict into the row-shaped dicts the DB writes need (no I/O, no state_machine transition decision beyond the one pure lookup it needs — `sub_estado_garantia`). `shared/pubsub_auth.py` verifies the Pub/Sub push's OIDC identity token (no I/O beyond the Google-library HTTP call, isolated behind a swappable function so tests never make a real network call). `apps/contratos/views.py` gains `processar_webhook_contrato`, which orchestrates: verify OIDC → fetch the queued event → resolve the `contrato` row → call `state_machine` → write everything → mark `webhook_inbox.processado_em`.

Three things worth calling out before the tasks, because they aren't obvious from the spec text:

1. **This plan assumes a matching `contrato` row already exists.** SPEC-02's `criar_contrato` flow (the internal API that submits `PUT /v15/contratos tipoOperacao=C`, gets the `207`, and creates the initial `contrato` row in `AGUARDANDO_WEBHOOK`) has **not been built yet** — it's a future plan (design doc §4, "API interna"). Until it lands, this processor has nothing real to consume in production; it can be fully built and tested today because every test creates its `contrato`/`garantia` fixture rows directly (the same thing SPEC-02 §13.2's IT-01–IT-04 scenarios do implicitly — "Criar contrato válido, webhook status=0 → REGISTRADO" describes the state transition under test, not how the contract got there). Sequencing the async pipeline before the synchronous create API is intentional: SPEC-02 §0 is explicit that a synchronous-request/response mental model is *wrong* for this service, so the state machine that governs "what does a `207` actually mean" should be solid before anything calls it.
2. **`garantiasAlcancadas[]` items are assumed to carry their own `referenciaExterna`.** SPEC-02 §5.2's compacted webhook-payload table lists the *fields inside* `unidadesRecebiveisAlcancadas[]` but doesn't explicitly spell out a `referenciaExterna` on each `garantiasAlcancadas[]` entry. There is no other way to correlate a batch of returned URs back to the specific `garantia` row that requested them (`AP007B`'s own `referenciaExterna`, §4.2, is the only per-garantia identifier that exists anywhere in this data model), and the CERC uses `referenciaExterna` as the correlation key at every other level of this same payload, so this is treated as a documented, reasonable assumption rather than an invented one — flagged here as the plan's own risk, the same way SPEC-02 §12 flags its own open questions. The processor **degrades safely** if the assumption is wrong for a given event: a `garantiasAlcancadas[]` entry whose `referenciaExterna` doesn't match any `garantia` row for that contract is logged and skipped (its URs are not persisted), not a hard failure — the contract's own status transition still succeeds.
3. **Idempotency under Pub/Sub's at-least-once redelivery** is handled primarily by one guard: if `webhook_inbox.processado_em IS NOT NULL` when this view runs, it immediately acks (`204`) without repeating any write. `garantia_ur` and `indicador_consistencia` writes additionally use `.upsert()` against their natural composite primary keys (built from data in the event itself, not wall-clock time) as a second line of defense for the narrow window where a crash happens *after* some writes but *before* `processado_em` gets set. `contrato_evento` (an append-only audit log with no natural dedup key) does **not** get this same protection — a crash in that exact window could produce one duplicate audit row on redelivery. This is an accepted, documented tradeoff: duplicating an audit-trail entry is cheap; inventing a synthetic dedup key for an append-only log is not worth the complexity it would add.

**Tech Stack:** Django function-based view, `google-oauth2`/`google-auth` (already transitive dependencies of `google-cloud-secret-manager`/`google-cloud-pubsub`, confirmed importable — no `requirements.txt` change needed), stdlib `base64`/`json`/`datetime`, `python-ulid` (webhook_inbox already uses it from Plan 10; this plan doesn't mint new IDs of that kind, but test fixtures do for `contrato.id`/`garantia.id` via `uuid.uuid4()`, matching `services/cerc/client.py`'s existing convention for those tables).

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` §6 ("Push subscription bate em endpoint próprio, verificado por OIDC"). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §5.2 (webhook `evento` fields), §5.2 "Regras de negócio derivadas" (subgarantido/excedente/UR-insucesso/indicador-crítico — the four predicates `apps/contratos/state_machine.py` already implements), §8 (state diagram), §11 (schema this plan writes to). Series: plan 11 of ~13.

**Depends on:** `2026-08-24-contratos-plan-09-state-machine.md` (`state_machine.estado_apos_webhook`, `.sub_estado_garantia`, `.eh_subgarantido`, `.EstadoInvalidoError` — all already implemented and tested, this plan only calls them), `2026-08-25-contratos-plan-10-webhook-receiver.md` (`webhook_inbox` rows this plan reads; `shared/cloudsql_client.get_db`).

## Global Constraints

- State names/sub-states are exactly what `apps/contratos/state_machine.py` already defines (`AGUARDANDO_WEBHOOK`, `ATUALIZANDO`, `REGISTRADO`, `REJEITADO`, `NAO_APLICAVEL`/`SUFICIENTE`/`INSUFICIENTE`/`EXCESSO`) — this plan never invents a new state string, only calls the existing pure functions.
- No Django ORM — all reads/writes go through `shared.cloudsql_client.get_db(financiador_id)`.
- No `float`/`double` for monetary values reaching the database — `evento`'s money fields (`valorUnidadesRecebiveisAlcancadas`, `valorConstituidoTotal`, etc.) pass through as whatever JSON produced (Python `float` from `json.loads` on a JSON number) directly into `NUMERIC` columns; this mirrors how Plan 10's receiver already stores the entire raw envelope as JSONB without type coercion, and is consistent with the existing codebase's monetary discipline being enforced at the schema layer (`NUMERIC(18,2)`), not by Python-side `Decimal` conversion — no task in this plan introduces a new pattern here.
- `webhook_inbox`, `contrato`, `garantia`, `garantia_ur`, `indicador_consistencia`, `contrato_evento` schemas (`sql/schema/01-contratos-schema.sql`) are fixed by this plan — no migration, no new columns.
- The Pub/Sub push endpoint has no `financiador_id` in its URL (unlike Plan 10's receiver) — `financiador_id` arrives inside the push message's decoded `data` payload, which Plan 10's `pubsub_client.publish_webhook_contrato` already embeds (`{"webhook_inbox_id": ..., "financiador_id": ...}`).

---

### Task 1: `apps/contratos/webhook_processor.py` — pure event-to-row mapping

**Files:**
- Create: `contratos/apps/contratos/webhook_processor.py`
- Test: `contratos/apps/contratos/tests/test_webhook_processor.py`

**Interfaces:**
- Consumes: `apps.contratos.state_machine.sub_estado_garantia(resultado_distribuicao_onus: str) -> str` (Plan 09, already implemented).
- Produces: `atualizacoes_contrato_do_evento(evento: dict) -> dict` (column→value dict for the `contrato` row; empty dict when `evento["status"] != "0"`, since a failed webhook only requires a `status` transition, nothing else — the caller, Task 3, adds `status` itself since that comes from `state_machine`, not from this module), `garantia_urs_do_evento(evento: dict, snapshot_em) -> list[dict]` (each dict has a synthetic `referencia_externa_garantia` key the caller must `.pop()` and resolve to a real `garantia_id` before writing — this module has no DB access, so it cannot do that resolution itself), `indicadores_do_evento(evento: dict, observado_em) -> list[dict]` (each ready to write to `indicador_consistencia` once the caller adds `contrato_id`). Task 3 imports all three.

- [ ] **Step 1: Write the failing test**

```python
# contratos/apps/contratos/tests/test_webhook_processor.py
from apps.contratos.webhook_processor import (
    atualizacoes_contrato_do_evento,
    garantia_urs_do_evento,
    indicadores_do_evento,
)


def _evento_sucesso(**overrides):
    base = {
        "referenciaExterna": "CTR-1",
        "protocolo": "P-1",
        "status": "0",
        "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
        "quantidadeUnidadesRecebiveisAlcancadas": 3,
        "valorUnidadesRecebiveisAlcancadas": 15000.00,
        "resultadoDistribuicaoOnus": "1",
        "garantiasAlcancadas": [],
        "indicadoresConsistencia": [],
    }
    base.update(overrides)
    return base


def test_atualizacoes_contrato_do_evento_sucesso_mapeia_todos_os_campos():
    resultado = atualizacoes_contrato_do_evento(_evento_sucesso())
    assert resultado == {
        "qtd_urs_alcancadas": 3,
        "valor_urs_alcancadas": 15000.00,
        "confirmado_em": "2026-08-25T12:00:00.000Z",
        "resultado_distribuicao": "1",
        "status_garantia": "SUFICIENTE",
    }


def test_atualizacoes_contrato_do_evento_falha_retorna_vazio():
    evento = {"referenciaExterna": "CTR-1", "protocolo": "P-1", "status": "1", "erros": []}
    assert atualizacoes_contrato_do_evento(evento) == {}


def test_atualizacoes_contrato_do_evento_status_garantia_insuficiente():
    resultado = atualizacoes_contrato_do_evento(_evento_sucesso(resultadoDistribuicaoOnus="2"))
    assert resultado["status_garantia"] == "INSUFICIENTE"


def _ur(cnpj_credenciadora, documento_ufr, codigo_arranjo):
    return {
        "cnpjCredenciadora": cnpj_credenciadora,
        "documentoUsuarioFinalRecebedor": documento_ufr,
        "documentoTitular": documento_ufr,
        "codigoArranjoPagamento": codigo_arranjo,
        "dataLiquidacao": "2026-09-01",
        "constituicao": "1",
        "valorConstituidoTotal": 5000.00,
        "valorBloqueado": 0.00,
        "indicadorOneracao": "1",
        "regrasDivisao": "1",
        "valorOnerado": 5000.00,
        "valorConstituidoEfeito": 5000.00,
    }


def test_garantia_urs_do_evento_achata_urs_de_multiplas_garantias():
    evento = _evento_sucesso(garantiasAlcancadas=[
        {"referenciaExterna": "CTR-1-G1", "unidadesRecebiveisAlcancadas": [_ur("11111111000111", "22222222000122", "VCC")]},
        {"referenciaExterna": "CTR-1-G2", "unidadesRecebiveisAlcancadas": [_ur("33333333000133", "44444444000144", "MCC")]},
    ])

    linhas = garantia_urs_do_evento(evento, snapshot_em="2026-08-25T12:00:00Z")

    assert len(linhas) == 2
    assert linhas[0]["referencia_externa_garantia"] == "CTR-1-G1"
    assert linhas[0]["cnpj_credenciadora"] == "11111111000111"
    assert linhas[0]["documento_ufr"] == "22222222000122"
    assert linhas[0]["codigo_arranjo"] == "VCC"
    assert linhas[0]["valor_onerado"] == 5000.00
    assert linhas[0]["origem"] == "WEBHOOK"
    assert linhas[0]["snapshot_em"] == "2026-08-25T12:00:00Z"
    assert linhas[1]["referencia_externa_garantia"] == "CTR-1-G2"
    assert linhas[1]["codigo_arranjo"] == "MCC"


def test_garantia_urs_do_evento_sem_garantias_retorna_lista_vazia():
    assert garantia_urs_do_evento(_evento_sucesso(), snapshot_em="2026-08-25T12:00:00Z") == []


def test_garantia_urs_do_evento_garantia_sem_urs_nao_gera_linha():
    evento = _evento_sucesso(garantiasAlcancadas=[{"referenciaExterna": "CTR-1-G1", "unidadesRecebiveisAlcancadas": []}])
    assert garantia_urs_do_evento(evento, snapshot_em="2026-08-25T12:00:00Z") == []


def test_indicadores_do_evento_mapeia_campos():
    evento = _evento_sucesso(indicadoresConsistencia=[
        {"indicador": "estabilidade_agenda", "resultado": "estável", "parametros": [{"chave": "dias", "valor": "30"}], "criticidade": "1"},
    ])

    linhas = indicadores_do_evento(evento, observado_em="2026-08-25T12:00:00Z")

    assert linhas == [{
        "indicador": "estabilidade_agenda",
        "resultado": "estável",
        "parametros": [{"chave": "dias", "valor": "30"}],
        "criticidade": "1",
        "observado_em": "2026-08-25T12:00:00Z",
    }]


def test_indicadores_do_evento_sem_indicadores_retorna_lista_vazia():
    assert indicadores_do_evento(_evento_sucesso(), observado_em="2026-08-25T12:00:00Z") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_webhook_processor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.contratos.webhook_processor'`

- [ ] **Step 3: Write `apps/contratos/webhook_processor.py`**

```python
"""Mapeia o `evento` do webhook CERC (tipoEvento=contrato, SPEC-02 §5.2)
para as escritas que o processador (Plano 11, apps/contratos/views.py)
precisa fazer em `contrato`, `garantia_ur` e `indicador_consistencia`.

Funções puras — nenhuma chamada a banco. O caller resolve `garantia_id`
(a partir da `referencia_externa_garantia` sintética que garantia_urs_do_evento
inclui em cada linha) e `contrato_id`, e executa as escritas.

Assunção documentada (não afirmada explicitamente na tabela compactada da
SPEC-02 §5.2, mas consistente com como a CERC correlaciona tudo nesse
payload — ver §4.2): cada item de `garantiasAlcancadas[]` carrega sua
própria `referenciaExterna`, igual à `garantias[].referenciaExterna`
enviada no request original (AP007B).
"""

from apps.contratos import state_machine


def atualizacoes_contrato_do_evento(evento: dict) -> dict:
    """§5.2: campos do evento que atualizam a linha de `contrato` quando
    status=0. Quando status=1 (falha), o evento não traz nenhum desses
    campos — o caller só atualiza `status` (via state_machine), nada mais."""
    if evento.get("status") != "0":
        return {}
    resultado = evento["resultadoDistribuicaoOnus"]
    return {
        "qtd_urs_alcancadas": evento.get("quantidadeUnidadesRecebiveisAlcancadas"),
        "valor_urs_alcancadas": evento.get("valorUnidadesRecebiveisAlcancadas"),
        "confirmado_em": evento.get("dataHoraProcessamento"),
        "resultado_distribuicao": resultado,
        "status_garantia": state_machine.sub_estado_garantia(resultado),
    }


def garantia_urs_do_evento(evento: dict, snapshot_em) -> list:
    """§5.2 `garantiasAlcancadas[].unidadesRecebiveisAlcancadas[]` -> linhas
    de `garantia_ur` (design §11, schema em sql/schema/01-contratos-schema.sql).
    `snapshot_em` deve ser determinístico (derivado do evento, não
    wall-clock) para que uma reentrega da CERC produza a mesma chave
    primária e o upsert do caller seja idempotente."""
    linhas = []
    for garantia in evento.get("garantiasAlcancadas", []) or []:
        referencia_externa_garantia = garantia.get("referenciaExterna")
        for ur in garantia.get("unidadesRecebiveisAlcancadas", []) or []:
            linhas.append({
                "referencia_externa_garantia": referencia_externa_garantia,
                "cnpj_credenciadora": ur.get("cnpjCredenciadora"),
                "documento_ufr": ur.get("documentoUsuarioFinalRecebedor"),
                "documento_titular": ur.get("documentoTitular"),
                "codigo_arranjo": ur.get("codigoArranjoPagamento"),
                "data_liquidacao": ur.get("dataLiquidacao"),
                "constituicao": ur.get("constituicao"),
                "valor_constituido_total": ur.get("valorConstituidoTotal"),
                "valor_bloqueado": ur.get("valorBloqueado"),
                "indicador_oneracao": ur.get("indicadorOneracao"),
                "regras_divisao": ur.get("regrasDivisao"),
                "valor_onerado": ur.get("valorOnerado"),
                "valor_constituido_efeito": ur.get("valorConstituidoEfeito"),
                "origem": "WEBHOOK",
                "snapshot_em": snapshot_em,
            })
    return linhas


def indicadores_do_evento(evento: dict, observado_em) -> list:
    """§5.2 `indicadoresConsistencia[]` -> linhas de `indicador_consistencia`
    (design §11). `observado_em` determinístico pelo mesmo motivo de
    `garantia_urs_do_evento`."""
    linhas = []
    for item in evento.get("indicadoresConsistencia", []) or []:
        linhas.append({
            "indicador": item.get("indicador"),
            "resultado": item.get("resultado"),
            "parametros": item.get("parametros", []),
            "criticidade": item.get("criticidade"),
            "observado_em": observado_em,
        })
    return linhas
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apps/contratos/tests/test_webhook_processor.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/contratos/webhook_processor.py apps/contratos/tests/test_webhook_processor.py
git commit -m "feat: pure webhook-event-to-row mapping for contract processing (SPEC-02 §5.2)"
```

---

### Task 2: `shared/pubsub_auth.py` — OIDC verification for the Pub/Sub push endpoint

**Files:**
- Create: `contratos/shared/pubsub_auth.py`
- Test: `contratos/shared/tests/test_pubsub_auth.py`

**Interfaces:**
- Produces: `verificar_push_oidc(request) -> bool` — never raises, fails closed (`False`) on any error (missing header, missing `PUBSUB_PUSH_AUDIENCE` env var, invalid/expired token, wrong invoker account). Task 3 imports this.
- Internal (overridden by tests via `monkeypatch.setattr`): `_verificar_id_token(token: str, audiencia: str) -> dict` — the real Google-library call, isolated exactly the way Plan 10 Task 1 isolated `_get_publisher()` so tests never touch the network.

- [ ] **Step 1: Write the failing test**

```python
# contratos/shared/tests/test_pubsub_auth.py
import shared.pubsub_auth as pubsub_auth


class _FakeRequest:
    def __init__(self, auth_header=None):
        self.META = {}
        if auth_header is not None:
            self.META["HTTP_AUTHORIZATION"] = auth_header


def test_verificar_push_oidc_sem_header_retorna_false():
    assert pubsub_auth.verificar_push_oidc(_FakeRequest()) is False


def test_verificar_push_oidc_header_sem_bearer_retorna_false():
    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Basic xxx")) is False


def test_verificar_push_oidc_token_valido_sem_conta_esperada_retorna_true(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")
    monkeypatch.delenv("PUBSUB_PUSH_INVOKER_SA", raising=False)
    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", lambda token, audiencia: {"email": "pubsub-push@proj.iam.gserviceaccount.com"})

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is True


def test_verificar_push_oidc_token_valido_mas_conta_diferente_da_esperada_retorna_false(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")
    monkeypatch.setenv("PUBSUB_PUSH_INVOKER_SA", "pubsub-push@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", lambda token, audiencia: {"email": "outra-conta@proj.iam.gserviceaccount.com"})

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is False


def test_verificar_push_oidc_token_valido_e_conta_esperada_bate_retorna_true(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")
    monkeypatch.setenv("PUBSUB_PUSH_INVOKER_SA", "pubsub-push@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", lambda token, audiencia: {"email": "pubsub-push@proj.iam.gserviceaccount.com"})

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is True


def test_verificar_push_oidc_token_invalido_retorna_false(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")

    def _falha(token, audiencia):
        raise ValueError("token expirado")

    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", _falha)

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is False


def test_verificar_push_oidc_sem_audiencia_configurada_retorna_false(monkeypatch):
    monkeypatch.delenv("PUBSUB_PUSH_AUDIENCE", raising=False)

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest shared/tests/test_pubsub_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.pubsub_auth'`

- [ ] **Step 3: Write `shared/pubsub_auth.py`**

```python
"""Verificação OIDC do push subscription do Pub/Sub — design §6 ("Push
subscription bate em endpoint próprio, verificado por OIDC"). O Pub/Sub
assina cada requisição de push com um ID token OIDC do Google, emitido
para a conta de serviço configurada na subscription; verificamos aqui que
o token é genuíno e foi emitido para esta audiência específica — defesa
em profundidade além do IAM do próprio Cloud Run (roles/run.invoker
restrito à conta do Pub/Sub).
"""

import logging
import os

logger = logging.getLogger(__name__)


def _verificar_id_token(token: str, audiencia: str) -> dict:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=audiencia)


def verificar_push_oidc(request) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return False
    token = header[len("Bearer "):]

    try:
        audiencia = os.environ["PUBSUB_PUSH_AUDIENCE"]
        claims = _verificar_id_token(token, audiencia)
    except Exception:
        logger.warning("[Processor] Token OIDC do push inválido ou PUBSUB_PUSH_AUDIENCE não configurado")
        return False

    conta_esperada = os.getenv("PUBSUB_PUSH_INVOKER_SA")
    if conta_esperada and claims.get("email") != conta_esperada:
        logger.warning("[Processor] Token OIDC de conta inesperada: %s", claims.get("email"))
        return False

    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest shared/tests/test_pubsub_auth.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Document the new env vars**

Add to `.env.example` (after the `CERC_API_BASE_URL` line, as a new block):

```
# Push subscription do Pub/Sub (Plano 11) — audiência esperada no ID token
# OIDC que o Pub/Sub assina em cada push. PUBSUB_PUSH_INVOKER_SA é opcional
# (allowlist extra de conta de serviço); sem ela, qualquer token OIDC válido
# para a audiência configurada é aceito.
PUBSUB_PUSH_AUDIENCE=https://contratos-homolog.example.com/api/v1/webhooks/contrato/processar
PUBSUB_PUSH_INVOKER_SA=
```

- [ ] **Step 6: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add shared/pubsub_auth.py shared/tests/test_pubsub_auth.py .env.example
git commit -m "feat: OIDC verification for the Pub/Sub push subscription (design §6)"
```

---

### Task 3: `POST /api/v1/webhooks/contrato/processar` — the processor view

**Files:**
- Modify: `contratos/apps/contratos/views.py`
- Modify: `contratos/apps/contratos/urls.py`
- Test: `contratos/apps/contratos/tests/test_views_webhook_processor.py`

**Interfaces:**
- Consumes: `apps.contratos.state_machine.estado_apos_webhook`, `.eh_subgarantido`, `.EstadoInvalidoError` (Plan 09); `apps.contratos.webhook_processor.atualizacoes_contrato_do_evento`, `.garantia_urs_do_evento`, `.indicadores_do_evento` (Task 1); `shared.pubsub_auth.verificar_push_oidc` (Task 2); `shared.cloudsql_client.get_db`.
- Produces: view function `processar_webhook_contrato(request)`, routed at `webhooks/contrato/processar` (a literal path segment — Plan 10's final review already constrained the sibling receiver route to `\d{14}` specifically so it wouldn't swallow this one).

- [ ] **Step 1: Write the failing tests**

```python
# contratos/apps/contratos/tests/test_views_webhook_processor.py
import base64
import json
import uuid
from datetime import date, datetime

import pytest
from django.test import Client
from ulid import ULID

from apps.contratos import views
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/webhooks/contrato/processar"


def _push_envelope(webhook_inbox_id, financiador_id=FINANCIADOR_TESTE):
    dados = json.dumps({"webhook_inbox_id": webhook_inbox_id, "financiador_id": financiador_id}).encode()
    return json.dumps({
        "message": {"data": base64.b64encode(dados).decode(), "messageId": "msg-1", "publishTime": "2026-08-25T12:00:00Z"},
        "subscription": "projects/registradora-506000/subscriptions/contratos-webhook-inbox-sub",
    })


def _criar_contrato(status, referencia_externa):
    contrato_id = str(uuid.uuid4())
    get_db(FINANCIADOR_TESTE).table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": referencia_externa,
        "identificador_contrato": "OP-TESTE",
        "status": status,
        "cnpj_participante": FINANCIADOR_TESTE,
        "documento_contratante": "22751826000125",
        "cnpj_detentor": FINANCIADOR_TESTE,
        "tipo_efeito": "2",
        "modalidade_operacao": "2",
        "gestao_entidade_registradora": "1",
        "saldo_devedor": 150000.00,
        "limite_operacao_garantida": 200000.00,
        "valor_mantido": 180000.00,
        "data_assinatura": date(2026, 8, 15),
        "data_vencimento": date(2027, 8, 15),
        "repactuacao": False,
    }).execute()
    return contrato_id


def _criar_garantia(contrato_id, referencia_externa):
    garantia_id = str(uuid.uuid4())
    get_db(FINANCIADOR_TESTE).table("garantia").insert({
        "id": garantia_id,
        "contrato_id": contrato_id,
        "referencia_externa": referencia_externa,
        "regras_divisao": "1",
        "valor_a_onerar": 180000.00,
        "def_lista_credenciadoras": ["99T"],
        "def_lista_arranjos": ["VCC", "MCC"],
        "def_data_inicio": date(2026, 8, 18),
        "def_data_fim": date(2027, 8, 15),
    }).execute()
    return garantia_id


def _criar_webhook_inbox(payload, processado_em=None):
    webhook_id = str(ULID())
    get_db(FINANCIADOR_TESTE).table("webhook_inbox").insert({
        "id": webhook_id,
        "tipo_evento": payload["tipoEvento"],
        "data_hora_evento": datetime.fromisoformat(payload["dataHoraEvento"]),
        "payload": payload,
        "hash_dedupe": webhook_id,  # único por linha de teste, dedupe não é o que este teste exercita
        "processado_em": datetime.fromisoformat(processado_em) if processado_em else None,
    }).execute()
    return webhook_id


def _limpar(contrato_id=None, garantia_id=None, webhook_inbox_id=None):
    db = get_db(FINANCIADOR_TESTE)
    if contrato_id:
        db.table("indicador_consistencia").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
    if garantia_id:
        db.table("garantia_ur").delete().eq("garantia_id", garantia_id).execute()
        db.table("garantia").delete().eq("id", garantia_id).execute()
    if contrato_id:
        db.table("contrato").delete().eq("id", contrato_id).execute()
    if webhook_inbox_id:
        db.table("webhook_inbox").delete().eq("id", webhook_inbox_id).execute()


def _envelope_sucesso(referencia_externa, referencia_garantia, resultado="1"):
    return {
        "tipoEvento": "contrato",
        "dataHoraEvento": "2026-08-25T12:00:00.000Z",
        "evento": {
            "referenciaExterna": referencia_externa,
            "protocolo": "proto-1",
            "status": "0",
            "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
            "quantidadeUnidadesRecebiveisAlcancadas": 1,
            "valorUnidadesRecebiveisAlcancadas": 5000.00,
            "resultadoDistribuicaoOnus": resultado,
            "garantiasAlcancadas": [{
                "referenciaExterna": referencia_garantia,
                "unidadesRecebiveisAlcancadas": [{
                    "cnpjCredenciadora": "11111111000111",
                    "documentoUsuarioFinalRecebedor": "22222222000122",
                    "documentoTitular": "22222222000122",
                    "codigoArranjoPagamento": "VCC",
                    "dataLiquidacao": "2026-09-01",
                    "constituicao": "1",
                    "valorConstituidoTotal": 5000.00,
                    "valorBloqueado": 0.00,
                    "indicadorOneracao": "1",
                    "regrasDivisao": "1",
                    "valorOnerado": 5000.00,
                    "valorConstituidoEfeito": 5000.00,
                }],
            }],
            "indicadoresConsistencia": [
                {"indicador": "estabilidade_agenda", "resultado": "estável", "parametros": [], "criticidade": "0"},
            ],
        },
    }


def _envelope_falha(referencia_externa):
    return {
        "tipoEvento": "contrato",
        "dataHoraEvento": "2026-08-25T12:00:00.000Z",
        "evento": {
            "referenciaExterna": referencia_externa, "protocolo": "proto-1", "status": "1",
            "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
            "erros": [{"codigo": "107501", "mensagem": "UFR sem vínculo"}],
        },
    }


@pytest.fixture(autouse=True)
def _oidc_ok(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)


def test_processor_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL, data=_push_envelope("qualquer-id"), content_type="application/json")
    assert response.status_code == 401


def test_processor_envelope_pubsub_malformado_retorna_400():
    response = Client().post(URL, data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


def test_processor_webhook_inbox_nao_encontrado_retorna_404():
    response = Client().post(URL, data=_push_envelope("id-inexistente"), content_type="application/json")
    assert response.status_code == 404


def test_processor_ja_processado_e_idempotente():
    webhook_id = _criar_webhook_inbox(_envelope_falha("CTR-TESTE-PROC-DUP"), processado_em="2026-08-25T12:05:00Z")
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_contrato_nao_encontrado_retorna_500():
    webhook_id = _criar_webhook_inbox(_envelope_sucesso("CTR-TESTE-PROC-SEMCONTRATO", "G1"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 500

        linha = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha.data[0]["processado_em"] is None  # deixado para nova entrega do Pub/Sub
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_evento_sucesso_atualiza_contrato_e_persiste_urs_e_indicadores():
    referencia_externa = "CTR-TESTE-PROC-OK"
    referencia_garantia = "CTR-TESTE-PROC-OK-G1"
    contrato_id = _criar_contrato("AGUARDANDO_WEBHOOK", referencia_externa)
    garantia_id = _criar_garantia(contrato_id, referencia_garantia)
    webhook_id = _criar_webhook_inbox(_envelope_sucesso(referencia_externa, referencia_garantia, resultado="1"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        contrato = db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]
        assert contrato["status"] == "REGISTRADO"
        assert contrato["status_garantia"] == "SUFICIENTE"
        assert contrato["resultado_distribuicao"] == "1"
        assert contrato["qtd_urs_alcancadas"] == 1

        urs = db.table("garantia_ur").select("*").eq("garantia_id", garantia_id).execute()
        assert len(urs.data) == 1
        assert urs.data[0]["cnpj_credenciadora"] == "11111111000111"
        assert urs.data[0]["origem"] == "WEBHOOK"

        indicadores = db.table("indicador_consistencia").select("*").eq("contrato_id", contrato_id).execute()
        assert len(indicadores.data) == 1
        assert indicadores.data[0]["indicador"] == "estabilidade_agenda"

        eventos = db.table("contrato_evento").select("*").eq("contrato_id", contrato_id).execute()
        assert any(e["tipo"] == "webhook_recebido" for e in eventos.data)

        linha_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha_inbox.data[0]["processado_em"] is not None
    finally:
        _limpar(contrato_id=contrato_id, garantia_id=garantia_id, webhook_inbox_id=webhook_id)


def test_processor_evento_falha_marca_rejeitado_sem_escrever_urs():
    referencia_externa = "CTR-TESTE-PROC-FALHA"
    contrato_id = _criar_contrato("AGUARDANDO_WEBHOOK", referencia_externa)
    webhook_id = _criar_webhook_inbox(_envelope_falha(referencia_externa))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        contrato = db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]
        assert contrato["status"] == "REJEITADO"
        assert contrato["qtd_urs_alcancadas"] is None
    finally:
        _limpar(contrato_id=contrato_id, webhook_inbox_id=webhook_id)


def test_processor_resultado_insuficiente_emite_evento_subgarantido():
    referencia_externa = "CTR-TESTE-PROC-SUBGARANTIDO"
    referencia_garantia = "CTR-TESTE-PROC-SUBGARANTIDO-G1"
    contrato_id = _criar_contrato("AGUARDANDO_WEBHOOK", referencia_externa)
    garantia_id = _criar_garantia(contrato_id, referencia_garantia)
    webhook_id = _criar_webhook_inbox(_envelope_sucesso(referencia_externa, referencia_garantia, resultado="2"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        eventos = get_db(FINANCIADOR_TESTE).table("contrato_evento").select("*").eq("contrato_id", contrato_id).execute()
        assert any(e["tipo"] == "ContratoSubgarantido" for e in eventos.data)
    finally:
        _limpar(contrato_id=contrato_id, garantia_id=garantia_id, webhook_inbox_id=webhook_id)


def test_processor_estado_invalido_marca_processado_sem_atualizar_contrato():
    referencia_externa = "CTR-TESTE-PROC-ESTADOINVALIDO"
    contrato_id = _criar_contrato("REGISTRADO", referencia_externa)  # já registrado — webhook de novo é inesperado
    webhook_id = _criar_webhook_inbox(_envelope_falha(referencia_externa))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        contrato = db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]
        assert contrato["status"] == "REGISTRADO"  # inalterado

        linha_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha_inbox.data[0]["processado_em"] is not None
        assert linha_inbox.data[0]["erro"] is not None
    finally:
        _limpar(contrato_id=contrato_id, webhook_inbox_id=webhook_id)


def test_processor_tipo_evento_diferente_de_contrato_e_ignorado_mas_marcado_processado():
    payload = {"tipoEvento": "agenda", "dataHoraEvento": "2026-08-25T12:00:00.000Z", "evento": {"algumCampo": "valor"}}
    webhook_id = _criar_webhook_inbox(payload)
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        linha_inbox = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha_inbox.data[0]["processado_em"] is not None
    finally:
        _limpar(webhook_inbox_id=webhook_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_views_webhook_processor.py -v`
Expected: FAIL — `AttributeError: module 'apps.contratos.views' has no attribute 'processar_webhook_contrato'` (and `verificar_push_oidc`/`get_db` not yet imported into `views.py` for the fixture's `monkeypatch.setattr(views, ...)` to target).

- [ ] **Step 3: Add to `apps/contratos/views.py`**

Add these imports at the top (alongside the existing ones from Plan 10):

```python
from datetime import datetime, timezone

from apps.contratos import state_machine
from apps.contratos.webhook_processor import (
    atualizacoes_contrato_do_evento,
    garantia_urs_do_evento,
    indicadores_do_evento,
)
from shared.pubsub_auth import verificar_push_oidc
```

Add this view function (after `webhook_contrato`):

```python
@require_POST
def processar_webhook_contrato(request):
    """Consumidor da push subscription do Pub/Sub — aplica a máquina de
    estados (§8) sobre o evento já persistido em webhook_inbox pelo
    receptor (Plano 10). Verificado por OIDC (design §6). Idempotente sob
    reentrega do Pub/Sub (at-least-once): o guard de
    webhook_inbox.processado_em evita refazer qualquer escrita."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    try:
        envelope = json.loads(request.body)
        dados = json.loads(base64.b64decode(envelope["message"]["data"]))
        webhook_inbox_id = dados["webhook_inbox_id"]
        financiador_id = dados["financiador_id"]
    except Exception:
        logger.exception("[Processor] Envelope do Pub/Sub push malformado")
        return JsonResponse({"erro": "envelope inválido"}, status=400)

    db = get_db(financiador_id)
    linhas_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_inbox_id).execute()
    if not linhas_inbox.data:
        logger.error("[Processor] webhook_inbox_id=%s não encontrado (financiador=%s)", webhook_inbox_id, financiador_id)
        return JsonResponse({"erro": "webhook_inbox não encontrado"}, status=404)
    inbox = linhas_inbox.data[0]

    if inbox["processado_em"] is not None:
        return JsonResponse({}, status=204)

    payload = inbox["payload"]
    tipo_evento = payload.get("tipoEvento")
    evento = payload.get("evento")

    if tipo_evento != "contrato":
        logger.warning("[Processor] tipoEvento=%s fora do escopo deste consumidor, ignorando", tipo_evento)
        db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    referencia_externa = evento.get("referenciaExterna")
    contratos = db.table("contrato").select("*").eq("referencia_externa", referencia_externa).execute()
    if not contratos.data:
        logger.error(
            "[Processor] contrato referencia_externa=%s não encontrado (financiador=%s) — deixando para nova entrega do Pub/Sub",
            referencia_externa, financiador_id,
        )
        return JsonResponse({"erro": "contrato não encontrado"}, status=500)
    contrato = contratos.data[0]

    try:
        novo_status = state_machine.estado_apos_webhook(contrato["status"], evento["status"])
    except state_machine.EstadoInvalidoError:
        logger.warning(
            "[Processor] webhook para contrato %s chegou em estado inesperado (%s) — tratando como já processado",
            contrato["id"], contrato["status"],
        )
        db.table("webhook_inbox").update({
            "processado_em": datetime.now(timezone.utc), "erro": "estado inválido para webhook",
        }).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    atualizacoes = atualizacoes_contrato_do_evento(evento)
    if atualizacoes.get("confirmado_em"):
        atualizacoes["confirmado_em"] = datetime.fromisoformat(atualizacoes["confirmado_em"])
    atualizacoes["status"] = novo_status
    db.table("contrato").update(atualizacoes).eq("id", contrato["id"]).execute()

    if evento.get("status") == "0":
        data_processamento = evento.get("dataHoraProcessamento")
        snapshot_em = datetime.fromisoformat(data_processamento) if data_processamento else datetime.now(timezone.utc)

        for ur in garantia_urs_do_evento(evento, snapshot_em):
            referencia_garantia = ur.pop("referencia_externa_garantia")
            garantias = (
                db.table("garantia").select("id")
                .eq("contrato_id", contrato["id"]).eq("referencia_externa", referencia_garantia)
                .execute()
            )
            if not garantias.data:
                logger.warning(
                    "[Processor] garantia referencia_externa=%s não encontrada no contrato %s — UR ignorada",
                    referencia_garantia, contrato["id"],
                )
                continue
            ur["garantia_id"] = garantias.data[0]["id"]
            db.table("garantia_ur").upsert(
                ur,
                on_conflict="garantia_id, cnpj_credenciadora, documento_ufr, documento_titular, codigo_arranjo, data_liquidacao, origem",
            ).execute()

        for indicador in indicadores_do_evento(evento, snapshot_em):
            indicador["contrato_id"] = contrato["id"]
            db.table("indicador_consistencia").upsert(
                indicador, on_conflict="contrato_id, indicador, observado_em",
            ).execute()

        if state_machine.eh_subgarantido(evento.get("resultadoDistribuicaoOnus")):
            db.table("contrato_evento").insert({
                "contrato_id": contrato["id"], "tipo": "ContratoSubgarantido",
                "payload": evento, "ocorrido_em": snapshot_em,
            }).execute()

    db.table("contrato_evento").insert({
        "contrato_id": contrato["id"], "tipo": "webhook_recebido",
        "payload": payload, "ocorrido_em": datetime.now(timezone.utc),
    }).execute()

    db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()

    return JsonResponse({}, status=204)
```

- [ ] **Step 4: Wire the URL**

```python
# contratos/apps/contratos/urls.py
from django.urls import path, re_path
from . import views

urlpatterns = [
    path("health", views.health),
    re_path(r"^webhooks/contrato/(?P<financiador_id>\d{14})$", views.webhook_contrato),
    path("webhooks/contrato/processar", views.processar_webhook_contrato),
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest apps/contratos/tests/test_views_webhook_processor.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass (existing suites untouched).

- [ ] **Step 7: Commit**

```bash
git add apps/contratos/views.py apps/contratos/urls.py apps/contratos/tests/test_views_webhook_processor.py
git commit -m "feat: webhook processor — apply state machine, persist URs/indicadores (SPEC-02 §5.2/§8)"
```

---

## Self-Review Notes

- **Spec coverage:** §5.2's `evento` fields for `status=0` (URs, indicadores, resultado de distribuição) and `status=1` (nothing beyond the state transition) are both covered, with dedicated tests for each branch. §5.2's four "regras de negócio derivadas" — `eh_subgarantido` is wired to the `ContratoSubgarantido` event (tested); `eh_candidato_liberacao_excedente`, `ur_teve_insucesso`, `indicador_critico` are **not** wired to any action in this plan (they remain pure predicates in `state_machine.py` from Plan 09) — this is a deliberate scope cut: excedente-liberation is explicitly "fora do escopo" per SPEC-02 §5.2 itself (AP026), and UR-insucesso/indicador-crítico are already fully queryable from the persisted `garantia_ur.indicador_oneracao`/`indicador_consistencia.criticidade` columns this plan writes — no additional action (e.g. an alert) is specified anywhere as belonging to *this* plan, so none was invented. §8's state diagram is exercised via `state_machine.estado_apos_webhook` (already tested in Plan 09) plus this plan's own tests proving the DB actually gets updated to match. Idempotency (SPEC-01 §4.4/SPEC-02 §5.3's dedup requirement is Plan 10's; this plan's own redelivery-safety requirement, not spec-mandated text but a correctness necessity for at-least-once Pub/Sub) is covered by the `processado_em`-already-set test.
- **Placeholder scan:** none — every branch (OIDC reject, malformed push envelope, inbox row missing, already-processed, contract missing, invalid-state-transition, success with URs/indicadores, failure, subgarantido, wrong `tipoEvento`) has a real test with real assertions on the actual database state, not just the HTTP status code.
- **Type consistency:** `atualizacoes_contrato_do_evento`, `garantia_urs_do_evento`, `indicadores_do_evento` (Task 1) are called with identical argument shapes in Task 3's view and in Task 1's own tests. `verificar_push_oidc(request) -> bool` (Task 2) is imported as a module-level name (`from shared.pubsub_auth import verificar_push_oidc`) and referenced as `views.verificar_push_oidc` in Task 3's tests' `monkeypatch.setattr` — this only works because Task 3's view calls it as the bare imported name `verificar_push_oidc(request)`, which Python resolves through the `views` module's own namespace at call time, so patching `views.verificar_push_oidc` (not `shared.pubsub_auth.verificar_push_oidc`) is the correct target — same "module-level swap" pattern Plan 10 established, just via direct-name import here since (unlike Plan 10's `pubsub_client`) there's only one function from this module used in the view, so a qualified `shared.pubsub_auth.verificar_push_oidc(request)` call style isn't needed for testability.

**Next:** `2026-08-25-contratos-plan-12-reconciliacao-sla.md` (`reconciliar_pendentes` management command — contracts stuck in `AGUARDANDO_WEBHOOK`/`ATUALIZANDO` past the configurable SLA move to `PENDENTE_CONCILIACAO` and trigger an automatic `POST /contrato/consultar`, per §8's timeout branch and design §6's Cloud Scheduler job list; `sincronizar_dominio_arranjo` alongside it).
