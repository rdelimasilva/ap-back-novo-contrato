# contratos-service — Plan 14: Inativar / Baixar Contrato (API interna) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /api/v1/contratos/<financiador_id>/inativar` and `POST /api/v1/contratos/<financiador_id>/baixar` — the first two internal-facing endpoints that mutate an *already-registered* contract (`tipoOperacao=I`/`B`), completing design §8's "Fase 1" list alongside Plan 12's `criar_contrato` (`atualizar`/`consultar` are separate, later plans — see Global Constraints). Both operations submit to the CERC, interpret the synchronous `207`, and persist the resulting waiting state (`INATIVANDO`/`BAIXANDO`) — the real outcome still arrives later via Plans 10/11's webhook pipeline, which (per Plan 13) already knows how to resolve these two states with zero changes of its own.

**Architecture:** Same three-piece shape as Plan 12 (pure logic in `state_machine.py`, a DB-write function in `contrato_repository.py`, a thin view), but far smaller: `I`/`B` don't create new data, they act on a contract this codebase already has fully persisted, so there is no validation orchestrator, no `Decimal`/`date` conversion, and no sub-table graph to write. `I` and `B` are exact mirrors of each other (same request shape, same state-machine shape, same repository call, differing only in `tipoOperacao` and which `services.cerc.client` function to call) — implemented as two thin `@require_POST` views sharing one private orchestration helper in `views.py`, the same way `apps/contratos/state_machine.py::estado_apos_webhook` already generalizes over waiting-state instead of duplicating per-operation logic.

Two architecture decisions this plan locks in, confirmed with the user rather than assumed:

1. **Scope: `I` and `B` only, in this one plan — not `P`/`R`.** The design doc (`2026-08-24-contratos-service-design.md` §8, "Fase 1 vs. fases seguintes") explicitly lists resilição parcial/total (`P`/`R`) under "**Fases seguintes (fora deste ciclo, não iniciar código)**" — out of scope for this cycle, alongside simulação (`S`). `Plan 13`'s state machine already treats all four operations symmetrically (`INATIVANDO`/`BAIXANDO`/`RESILINDO_PARCIAL`/`RESILINDO_TOTAL` all exist as constants today), but nothing routes to the `P`/`R` waiting states yet, and this plan does not change that — building `resilir_contrato` is explicitly a future plan's job, not a side effect of this one.
2. **Request payload: caller sends only `referenciaExterna` — nothing else.** SPEC-02 §4.1 lists every contract field as unconditionally `✔ obrigatório` in one table that doesn't distinguish by `tipoOperacao`, which reads as though `I`/`B` requests need the same full body as `C`. Confirmed with the user instead of assumed: since the target contract is **already fully persisted** in this codebase from its creation (Plan 12), the internal endpoint requires only the one field needed to *identify* which contract to act on (`referenciaExterna` — already the idempotency key `criar_contrato` uses, via the existing `buscar_contrato_por_referencia`). `identificadorContrato`, `documentoContratante`, and `cnpjParticipante` (from the URL) are read from the **stored** `contrato` row and used to build the CERC-facing payload — never re-accepted from the caller, which would just open a class of bugs where the caller's copy drifts from what's actually on file for an immutable field. **Known, documented risk carried forward, not resolved:** if the CERC's real API turns out to require more than these four fields for `I`/`B` (SPEC-02's text doesn't say either way), the CERC will reject the request structurally (`207 status=1` or `400`) — which this plan's design already treats as "operation not applied, contract stays `REGISTRADO`" (Global Constraint below), so a wrong assumption here fails safely rather than corrupting data. Whoever builds the future `resilir_contrato` (`P`/`R`) plan should re-read this paragraph rather than assume it's settled.

A third design point worth calling out because it isn't obvious from a single task's diff: **the synchronous-207-failure case needed a state-machine rule Plan 13 never had to write**, because Plan 13 only extended the *webhook* side (`estado_apos_webhook`) — it had no endpoint yet, so it never had to decide what happens when the CERC's *own 207 response* structurally rejects an `I`/`B` submission. This plan adds exactly one new pure function, `estado_apos_207_pos_registro`, applying Plan 13's already-confirmed rationale (a failed post-registration operation does not reject the underlying contract) symmetrically to this new, synchronous failure path: `status_207="1"` returns `REGISTRADO` (not `REJEITADO_ESTRUTURAL` — that would incorrectly imply the contract itself was never registered), `status_207="0"` delegates to the already-existing `estado_apos_operacao_pos_registro`. This is a direct, mechanical extension of Plan 13's own confirmed decision, not a new business call — see Task 1.

**Tech Stack:** Django function-based views, same conventions as Plan 12 (`shared.cloudsql_client.get_db`, no ORM, `respx` for mocking the CERC HTTP call in tests).

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` §4/§8 ("API interna... Fase 1: criar, atualizar, inativar, baixar, consultar"). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §0 (async rule), §4.1 (`tipoOperacao` enum), §8 (state diagram). Series: plan 14 of ~14+.

**Depends on:** `2026-08-25-contratos-plan-12-criar-contrato.md` (`buscar_contrato_por_referencia`, the `criar_contrato` view this plan's views sit beside in `views.py`/`urls.py`, the CERC-quarantine-logging pattern this plan reuses verbatim), `2026-08-26-contratos-plan-13-...md` (the state machine this plan extends by exactly one function — every existing transition Plan 13 built must keep working unchanged), `services/cerc/client.py` (`inativar_contrato`/`baixar_contrato`, already implemented, unused until this plan).

## Global Constraints

- **This plan is `I`/`B` only.** `atualizar` (`A`), `consultar`, `simular` (`S`), and `resilir` (`P`/`R`) are NOT built here — see Architecture Decision 1 above for why `P`/`R` specifically is out of scope (design doc §8 explicitly defers it).
- A synchronous `207 status=1` for an `I`/`B` submission means **the operation was rejected, not the contract** — the contract's `status` returns to (or stays at) `REGISTRADO`, never `REJEITADO_ESTRUTURAL`. This mirrors the already-confirmed webhook-failure rule from Plan 13 (`estado_apos_webhook` on `INATIVANDO`/`BAIXANDO` failure → `REGISTRADO`), applied to the new synchronous path. HTTP response is `422` in this case, mirroring `criar_contrato`'s `REJEITADO_ESTRUTURAL` → `422` convention.
- No Django ORM — all reads/writes go through `shared.cloudsql_client.get_db(financiador_id)`, same as every other view in this file.
- `cnpjParticipante` is never read from the request body — always `financiador_id` from the URL, same rule Plan 12 established (design §1.1: "financiador_id = o próprio cnpjParticipante").
- Idempotency: keyed on `referenciaExterna` + the contract's *current* `status`, not a separate `Idempotency-Key` header — same reasoning Plan 12 used (SPEC-02 §7.3's `107803` idempotency rationale), extended here to a three-way outcome instead of Plan 12's two-way one (see Task 3's `situacao_operacao_pos_registro`): proceed (from `REGISTRADO`), idempotent replay (already in this exact operation's waiting or terminal state), or conflict (any other state, including the *other* operation's states — you cannot `baixar` a contract that's `INATIVANDO`).
- `apps/contratos/webhook_processor.py` and `apps/contratos/views.py::processar_webhook_contrato` (Plan 11) need **zero changes** — Plan 13 already verified `estado_apos_webhook` handles `INATIVANDO`/`BAIXANDO` generically. Confirmed again in Task 3 (existing webhook-processor tests must keep passing unmodified).

---

### Task 1: extend `apps/contratos/state_machine.py` — 207 outcome and pre-flight situação for post-registration ops

**Files:**
- Modify: `contratos/apps/contratos/state_machine.py`
- Modify: `contratos/apps/contratos/tests/test_state_machine.py`

**Interfaces:**
- Consumes: `REGISTRADO`, `EstadoInvalidoError`, `_OPERACAO_PARA_ESTADO_ESPERA`, `estado_apos_operacao_pos_registro` (all already exist, Plan 13).
- Produces: `estado_apos_207_pos_registro(tipo_operacao: str, status_207: str) -> str` (used by Task 3's view after calling the CERC); `situacao_operacao_pos_registro(estado_atual: str, tipo_operacao: str) -> str`, returning one of the three string literals `"PROSSEGUIR"`, `"REPLAY"`, `"CONFLITO"` (used by Task 3's view *before* calling the CERC, to decide whether to submit, replay, or refuse). Both are pure, no I/O. Task 3 imports both.

- [ ] **Step 1: Write the failing tests**

Append to `apps/contratos/tests/test_state_machine.py` (do not remove or modify any existing test in this file — every test from Plan 13 must keep passing unchanged):

```python
# --- Plano 14: 207 síncrono de uma operação pós-registro (I/B/P/R) ---

@pytest.mark.parametrize("tipo_operacao,estado_espera", [
    ("I", sm.INATIVANDO), ("B", sm.BAIXANDO), ("P", sm.RESILINDO_PARCIAL), ("R", sm.RESILINDO_TOTAL),
])
def test_estado_apos_207_pos_registro_sucesso_vai_para_espera(tipo_operacao, estado_espera):
    assert sm.estado_apos_207_pos_registro(tipo_operacao, status_207="0") == estado_espera


@pytest.mark.parametrize("tipo_operacao", ["I", "B", "P", "R"])
def test_estado_apos_207_pos_registro_falha_volta_para_registrado(tipo_operacao):
    # Rejeição ESTRUTURAL da OPERAÇÃO (207 status=1), não do contrato — o
    # contrato já estava REGISTRADO antes desta tentativa e continua sendo.
    # Mesma razão do Plano 13 para a falha via webhook, aplicada aqui ao
    # caminho síncrono do 207.
    assert sm.estado_apos_207_pos_registro(tipo_operacao, status_207="1") == sm.REGISTRADO


# --- Plano 14: situação de uma NOVA requisição I/B/P/R, antes de chamar a CERC ---

@pytest.mark.parametrize("tipo_operacao", ["I", "B", "P", "R"])
def test_situacao_operacao_pos_registro_de_registrado_e_prosseguir(tipo_operacao):
    assert sm.situacao_operacao_pos_registro(sm.REGISTRADO, tipo_operacao) == "PROSSEGUIR"


@pytest.mark.parametrize("tipo_operacao,estado_espera,estado_terminal", [
    ("I", sm.INATIVANDO, sm.INATIVADO),
    ("B", sm.BAIXANDO, sm.BAIXADO),
    ("P", sm.RESILINDO_PARCIAL, sm.RESILIDO_PARCIAL),
    ("R", sm.RESILINDO_TOTAL, sm.RESILIDO_TOTAL),
])
def test_situacao_operacao_pos_registro_repetida_e_replay(tipo_operacao, estado_espera, estado_terminal):
    # Requisição repetida da MESMA operação — já em espera, ou já concluída —
    # é replay idempotente, não conflito.
    assert sm.situacao_operacao_pos_registro(estado_espera, tipo_operacao) == "REPLAY"
    assert sm.situacao_operacao_pos_registro(estado_terminal, tipo_operacao) == "REPLAY"


@pytest.mark.parametrize("tipo_operacao,estado_atual", [
    # Contrato ainda não chegou a REGISTRADO.
    ("I", sm.AGUARDANDO_WEBHOOK), ("I", sm.ATUALIZANDO), ("I", sm.REJEITADO_ESTRUTURAL),
    ("I", sm.REJEITADO), ("I", sm.PENDENTE_CONCILIACAO),
    # A OUTRA operação está em curso ou já concluída — não pode inativar um
    # contrato que está sendo baixado (ou já foi baixado), e vice-versa.
    ("I", sm.BAIXANDO), ("I", sm.BAIXADO),
    ("B", sm.INATIVANDO), ("B", sm.INATIVADO),
])
def test_situacao_operacao_pos_registro_de_outro_estado_e_conflito(tipo_operacao, estado_atual):
    assert sm.situacao_operacao_pos_registro(estado_atual, tipo_operacao) == "CONFLITO"
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `pytest apps/contratos/tests/test_state_machine.py -v`
Expected: every pre-existing test (43 from Plan 13) still PASSES. Every new test FAILS with `AttributeError: module 'apps.contratos.state_machine' has no attribute 'estado_apos_207_pos_registro'` (or `'situacao_operacao_pos_registro'`).

- [ ] **Step 3: Add the two functions to `apps/contratos/state_machine.py`**

Add this block after `estado_apos_operacao_pos_registro` (i.e. right before the `_RESULTADO_PARA_SUBESTADO` section) — do not modify anything above it:

```python
def estado_apos_207_pos_registro(tipo_operacao: str, status_207: str) -> str:
    """§8 (Plano 14): resultado SÍNCRONO (207) de uma submissão I/B/P/R —
    distinto de `estado_apos_operacao_pos_registro`, que só decide o estado
    de ESPERA a entrar quando a CERC aceita (status=0); esta função também
    cobre o caminho de rejeição síncrona (status=1), que Plano 13 nunca
    precisou tratar (não existia endpoint algum ainda).

    status=0 -> delega para estado_apos_operacao_pos_registro (mesmo destino
    de espera). status=1 -> rejeição ESTRUTURAL da OPERAÇÃO, não do
    contrato — REGISTRADO (nunca REJEITADO_ESTRUTURAL, que implicaria que o
    contrato em si nunca foi registrado). Mesma razão do Plano 13 para a
    falha via webhook (INATIVANDO/BAIXANDO/... -> REGISTRADO em status=1),
    aplicada aqui ao caminho síncrono."""
    if status_207 == "1":
        return REGISTRADO
    return estado_apos_operacao_pos_registro(REGISTRADO, tipo_operacao)


_OPERACAO_PARA_ESTADO_TERMINAL = {
    "I": INATIVADO,
    "B": BAIXADO,
    "P": RESILIDO_PARCIAL,
    "R": RESILIDO_TOTAL,
}


def situacao_operacao_pos_registro(estado_atual: str, tipo_operacao: str) -> str:
    """§8 (Plano 14): o que fazer com uma NOVA requisição I/B/P/R, ANTES de
    chamar a CERC — decide se o endpoint deve prosseguir, responder com um
    replay idempotente, ou recusar por conflito.

    "PROSSEGUIR": estado_atual == REGISTRADO — pode submeter à CERC.
    "REPLAY": estado_atual já é o estado de ESPERA ou o TERMINAL desta MESMA
    tipo_operacao (ex.: tipo_operacao="I" e estado_atual em
    {INATIVANDO, INATIVADO}) — requisição repetida, não chama a CERC de
    novo, quem chama devolve o estado atual como está.
    "CONFLITO": qualquer outro estado_atual — inclui tanto "o contrato ainda
    não chegou a REGISTRADO" quanto "a OUTRA operação pós-registro está em
    curso ou já concluída" (ex.: tentar inativar um contrato BAIXANDO)."""
    if estado_atual == REGISTRADO:
        return "PROSSEGUIR"
    if estado_atual == _OPERACAO_PARA_ESTADO_ESPERA.get(tipo_operacao) or estado_atual == _OPERACAO_PARA_ESTADO_TERMINAL.get(tipo_operacao):
        return "REPLAY"
    return "CONFLITO"
```

- [ ] **Step 4: Run tests to verify they all pass**

Run: `pytest apps/contratos/tests/test_state_machine.py -v`
Expected: PASS. 43 (Plano 13) + 8 (`estado_apos_207_pos_registro`, 4 success + 4 failure) + 4 (`situacao_operacao_pos_registro` PROSSEGUIR) + 4 (REPLAY — 4 parametrized cases, each asserting both the espera and the terminal state internally) + 9 (CONFLITO) = 68 collected tests total in this file (43 + 25 new).

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all pass. Full suite was 226 before this plan (confirmed); `test_state_machine.py` goes from 43 to 68 (net +25), so the full suite total should land at 251.

- [ ] **Step 6: Commit**

```bash
git add apps/contratos/state_machine.py apps/contratos/tests/test_state_machine.py
git commit -m "feat: state machine support for I/B 207 outcome and pre-flight conflict check (SPEC-02 §8)"
```

---

### Task 2: `apps/contratos/contrato_repository.py` — persist a post-registration status change

**Files:**
- Modify: `contratos/apps/contratos/contrato_repository.py`
- Modify: `contratos/apps/contratos/tests/test_contrato_repository.py`

**Interfaces:**
- Consumes: `shared.cloudsql_client.get_db` (already imported in this module).
- Produces: `atualizar_status_pos_registro(financiador_id: str, contrato_id: str, novo_status: str, protocolo: str | None) -> dict` — `UPDATE contrato SET status=..., protocolo_cerc=...` and returns the updated row. No sub-tables to touch (unlike `inserir_contrato_criado` — `I`/`B` create no new `garantia`/`contrato_parcela`/etc. rows). Task 3 imports this.

- [ ] **Step 1: Write the failing test**

Append to `apps/contratos/tests/test_contrato_repository.py`:

```python
from apps.contratos.contrato_repository import atualizar_status_pos_registro


def _inserir_contrato_registrado_minimo(referencia_externa: str, identificador_contrato: str = "OP-TESTE-POS-REGISTRO") -> dict:
    """Insere uma linha `contrato` mínima, já REGISTRADA, direto na tabela —
    sem passar por `inserir_contrato_criado` (que exige um payload_validado
    completo, com garantias/parcelas, irrelevante para testar uma função que
    só faz UPDATE de duas colunas)."""
    db = get_db(FINANCIADOR_TESTE)
    contrato_id = str(uuid.uuid4())
    db.table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": referencia_externa,
        "identificador_contrato": identificador_contrato,
        "protocolo_cerc": "proto-original",
        "status": "REGISTRADO",
        "cnpj_participante": FINANCIADOR_TESTE,
        "documento_contratante": "22751826000125",
        "cnpj_detentor": FINANCIADOR_TESTE,
        "tipo_efeito": "2",
        "modalidade_operacao": "2",
        "gestao_entidade_registradora": "1",
        "saldo_devedor": Decimal("150000.00"),
        "limite_operacao_garantida": Decimal("200000.00"),
        "valor_mantido": Decimal("180000.00"),
        "data_assinatura": date(2026, 8, 15),
        "data_vencimento": date(2027, 8, 15),
        "repactuacao": False,
    }).execute()
    return db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]


def test_atualizar_status_pos_registro_grava_novo_status_e_protocolo():
    referencia_externa = "CTR-TESTE-REPO-POS-REGISTRO-1"
    contrato = _inserir_contrato_registrado_minimo(referencia_externa)
    try:
        atualizado = atualizar_status_pos_registro(
            FINANCIADOR_TESTE, contrato["id"], novo_status="INATIVANDO", protocolo="proto-novo",
        )
        assert atualizado["status"] == "INATIVANDO"
        assert atualizado["protocolo_cerc"] == "proto-novo"
        # nenhuma outra coluna foi tocada
        assert atualizado["identificador_contrato"] == contrato["identificador_contrato"]
        assert atualizado["documento_contratante"] == contrato["documento_contratante"]
    finally:
        get_db(FINANCIADOR_TESTE).table("contrato").delete().eq("id", contrato["id"]).execute()
```

`uuid` is already imported at module level in this test file (used by earlier Plan 12 tests); if it isn't, add `import uuid` to the top alongside the existing `from datetime import date` / `from decimal import Decimal` imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_contrato_repository.py -v`
Expected: FAIL with `ImportError: cannot import name 'atualizar_status_pos_registro'`.

- [ ] **Step 3: Add the function to `apps/contratos/contrato_repository.py`**

Append at the end of the file:

```python
def atualizar_status_pos_registro(financiador_id: str, contrato_id: str, novo_status: str, protocolo: str | None) -> dict:
    """Persiste o resultado de uma operação pós-registro (I/B/P/R, Plano 14)
    sobre um contrato JÁ REGISTRADO — grava o status resultante (estado de
    ESPERA em sucesso, ou de volta a REGISTRADO em rejeição estrutural
    síncrona, ver state_machine.estado_apos_207_pos_registro) e o protocolo
    mais recente. Ao contrário de `inserir_contrato_criado`, esta operação
    não cria nenhuma linha em sub-tabela — o contrato e suas garantias já
    existem por inteiro desde a criação."""
    atualizado = get_db(financiador_id).table("contrato").update({
        "status": novo_status, "protocolo_cerc": protocolo,
    }).eq("id", contrato_id).execute()
    return atualizado.data[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apps/contratos/tests/test_contrato_repository.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/contratos/contrato_repository.py apps/contratos/tests/test_contrato_repository.py
git commit -m "feat: persist post-registration status changes for an already-registered contract (SPEC-02 §8)"
```

---

### Task 3: `apps/contratos/views.py` + `urls.py` — `inativar_contrato` / `baixar_contrato` endpoints

**Files:**
- Modify: `contratos/apps/contratos/views.py`
- Modify: `contratos/apps/contratos/urls.py`
- Create: `contratos/apps/contratos/tests/test_views_pos_registro.py`

**Interfaces:**
- Consumes: `state_machine.situacao_operacao_pos_registro`, `state_machine.estado_apos_207_pos_registro` (Task 1); `contrato_repository.atualizar_status_pos_registro` (Task 2); `contrato_repository.buscar_contrato_por_referencia` (already imported in `views.py`, Plan 12); `services.cerc.client.inativar_contrato`, `services.cerc.client.baixar_contrato`, `services.cerc.client.CercApiError` (already implemented, unused until now).
- Produces: `POST /api/v1/contratos/<financiador_id>/inativar` and `POST /api/v1/contratos/<financiador_id>/baixar` — both accept `{"referenciaExterna": "..."}` and return `{"id", "status", "referenciaExterna", "protocolo"[, "erros"]}`.

- [ ] **Step 1: Write the failing tests**

Create `contratos/apps/contratos/tests/test_views_pos_registro.py`:

```python
import json
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from django.test import Client

from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"

# (tipoOperacao, sufixo da URL, estado de ESPERA, estado TERMINAL, o "outro"
# estado de espera/terminal — usado nos testes de conflito cruzado I<->B)
OPERACOES = [
    ("I", "inativar", "INATIVANDO", "INATIVADO", "BAIXANDO"),
    ("B", "baixar", "BAIXANDO", "BAIXADO", "INATIVANDO"),
]


def _url(sufixo):
    return f"/api/v1/contratos/{FINANCIADOR_TESTE}/{sufixo}"


def _mock_token():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )


def _inserir_contrato(referencia_externa: str, status: str, identificador_contrato: str = "OP-TESTE-POS-REGISTRO-VIEW") -> dict:
    db = get_db(FINANCIADOR_TESTE)
    contrato_id = str(uuid.uuid4())
    db.table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": referencia_externa,
        "identificador_contrato": identificador_contrato,
        "protocolo_cerc": "proto-original",
        "status": status,
        "cnpj_participante": FINANCIADOR_TESTE,
        "documento_contratante": "22751826000125",
        "cnpj_detentor": FINANCIADOR_TESTE,
        "tipo_efeito": "2",
        "modalidade_operacao": "2",
        "gestao_entidade_registradora": "1",
        "saldo_devedor": Decimal("150000.00"),
        "limite_operacao_garantida": Decimal("200000.00"),
        "valor_mantido": Decimal("180000.00"),
        "data_assinatura": date(2026, 8, 15),
        "data_vencimento": date(2027, 8, 15),
        "repactuacao": False,
    }).execute()
    return db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]


def _limpar(contrato_id):
    db = get_db(FINANCIADOR_TESTE)
    db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
    db.table("contrato").delete().eq("id", contrato_id).execute()


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_get_retorna_405(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().get(_url(sufixo))
    assert response.status_code == 405


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_corpo_nao_json_retorna_400(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().post(_url(sufixo), data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
@pytest.mark.parametrize("corpo_json", ["[]", '"string"', "42", "null"])
def test_corpo_json_nao_objeto_retorna_400(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro, corpo_json):
    response = Client().post(_url(sufixo), data=corpo_json, content_type="application/json")
    assert response.status_code == 400


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_referencia_externa_ausente_retorna_422(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().post(_url(sufixo), data=json.dumps({}), content_type="application/json")
    assert response.status_code == 422
    assert response.json()["codigo"] == "CAMPO_OBRIGATORIO"


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_contrato_nao_encontrado_retorna_404(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().post(
        _url(sufixo), data=json.dumps({"referenciaExterna": "CTR-NUNCA-EXISTIU"}), content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_estado_incompativel_retorna_409(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-409"
    contrato = _inserir_contrato(referencia_externa, status="AGUARDANDO_WEBHOOK")
    try:
        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")
        assert response.status_code == 409
    finally:
        _limpar(contrato["id"])


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,outro_estado_espera", OPERACOES)
def test_conflito_com_a_outra_operacao_em_curso_retorna_409(tipo_operacao, sufixo, estado_espera, estado_terminal, outro_estado_espera):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-409-CRUZADO"
    contrato = _inserir_contrato(referencia_externa, status=outro_estado_espera)
    try:
        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")
        assert response.status_code == 409
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_sucesso_207_status_0_persiste_estado_de_espera(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-OK"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": f"proto-{sufixo}-1",
                "dataHoraProcessamento": "2026-08-26T12:00:00.000Z", "status": "0", "erros": [],
            }])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 202
        corpo = response.json()
        assert corpo["status"] == estado_espera
        assert corpo["protocolo"] == f"proto-{sufixo}-1"

        atualizado = get_db(FINANCIADOR_TESTE).table("contrato").select("*").eq("id", contrato["id"]).execute().data[0]
        assert atualizado["status"] == estado_espera
        assert atualizado["protocolo_cerc"] == f"proto-{sufixo}-1"
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_207_status_1_volta_para_registrado_e_retorna_422(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-REJEITADO"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        erros_cerc = [{"codigo": "107xxx", "mensagem": "operação recusada"}]
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": f"proto-{sufixo}-2",
                "dataHoraProcessamento": "2026-08-26T12:00:00.000Z", "status": "1", "erros": erros_cerc,
            }])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 422
        corpo = response.json()
        assert corpo["status"] == "REGISTRADO"
        assert corpo["erros"] == erros_cerc

        atualizado = get_db(FINANCIADOR_TESTE).table("contrato").select("*").eq("id", contrato["id"]).execute().data[0]
        assert atualizado["status"] == "REGISTRADO"
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_replay_a_partir_do_estado_de_espera_nao_chama_a_cerc_de_novo(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-REPLAY-ESPERA"
    contrato = _inserir_contrato(referencia_externa, status=estado_espera)
    try:
        rota = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{"referenciaExterna": referencia_externa, "protocolo": "nao-deveria-usar", "status": "0", "erros": []}])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 202
        assert response.json()["status"] == estado_espera
        assert rota.call_count == 0
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_replay_a_partir_do_estado_terminal_nao_chama_a_cerc_de_novo(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-REPLAY-TERMINAL"
    contrato = _inserir_contrato(referencia_externa, status=estado_terminal)
    try:
        rota = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{"referenciaExterna": referencia_externa, "protocolo": "nao-deveria-usar", "status": "0", "erros": []}])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 202
        assert response.json()["status"] == estado_terminal
        assert rota.call_count == 0
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_erro_cerc_retorna_502(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-502"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(return_value=httpx.Response(500, json={"erro": "indisponível"}))

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 502
        atualizado = get_db(FINANCIADOR_TESTE).table("contrato").select("*").eq("id", contrato["id"]).execute().data[0]
        assert atualizado["status"] == "REGISTRADO"  # nada mudou localmente
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_207_com_array_vazio_retorna_500(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-207-VAZIO"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(return_value=httpx.Response(207, json=[]))

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 500
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_falha_ao_persistir_apos_207_retorna_500(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro, monkeypatch):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-FALHA-PERSISTIR"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-boom",
                "dataHoraProcessamento": "2026-08-26T12:00:00.000Z", "status": "0", "erros": [],
            }])
        )

        def _explode(*args, **kwargs):
            raise RuntimeError("banco caiu no meio da persistência")

        monkeypatch.setattr("apps.contratos.views.atualizar_status_pos_registro", _explode)

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 500
        corpo = response.json()
        assert corpo["protocolo"] == "proto-boom"
        assert corpo["referenciaExterna"] == referencia_externa
    finally:
        _limpar(contrato["id"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest apps/contratos/tests/test_views_pos_registro.py -v`
Expected: FAIL — `django.urls.exceptions.NoReverseMatch` or a `404` from every test (the routes don't exist yet), or an `ImportError`/`AttributeError` once the view module is touched. All fail for the same underlying reason: `inativar_contrato`/`baixar_contrato` views and their URL routes don't exist yet.

- [ ] **Step 3: Update `apps/contratos/views.py`**

Change the import line for the CERC client (currently `from services.cerc.client import CercApiError, criar_contrato as cerc_criar_contrato`) to:

```python
from services.cerc.client import (
    CercApiError,
    baixar_contrato as cerc_baixar_contrato,
    criar_contrato as cerc_criar_contrato,
    inativar_contrato as cerc_inativar_contrato,
)
```

Change the `contrato_repository` import to also bring in `atualizar_status_pos_registro`:

```python
from apps.contratos.contrato_repository import (
    atualizar_status_pos_registro,
    buscar_contrato_por_referencia,
    inserir_contrato_criado,
    remover_contrato_rejeitado,
)
```

Append this block at the end of the file (after `criar_contrato`):

```python
def _operacao_pos_registro(request, financiador_id: str, tipo_operacao: str, cerc_fn):
    """Núcleo comum de `inativar_contrato`/`baixar_contrato` (Plano 14) —
    `I`/`B` são espelhos exatos um do outro (mesma forma de payload, mesma
    máquina de estados), diferindo só em `tipo_operacao` e em qual função do
    cliente CERC chamar. Segue o mesmo padrão de quarentena pós-CERC de
    `criar_contrato` acima: uma vez que a CERC aceitou a submissão, qualquer
    falha ao interpretar/persistir localmente vira 500 logado com protocolo +
    referência (dado real já commitado do lado da CERC), nunca uma exceção
    não tratada."""
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"erro": "corpo deve ser um objeto JSON"}, status=400)

    referencia_externa = payload.get("referenciaExterna")
    if not referencia_externa:
        return JsonResponse({"codigo": "CAMPO_OBRIGATORIO", "erro": "'referenciaExterna' é obrigatório"}, status=422)

    contrato = buscar_contrato_por_referencia(financiador_id, referencia_externa)
    if contrato is None:
        return JsonResponse({"erro": f"contrato referenciaExterna={referencia_externa} não encontrado"}, status=404)

    situacao = state_machine.situacao_operacao_pos_registro(contrato["status"], tipo_operacao)
    if situacao == "CONFLITO":
        return JsonResponse({
            "erro": f"contrato está em '{contrato['status']}', operação '{tipo_operacao}' não permitida a partir deste estado",
        }, status=409)
    if situacao == "REPLAY":
        return JsonResponse({
            "id": contrato["id"], "status": contrato["status"], "referenciaExterna": referencia_externa,
        }, status=202)

    # "PROSSEGUIR": campos-chave lidos do que JÁ ESTÁ PERSISTIDO, nunca do
    # corpo da requisição — identificadorContrato/documentoContratante são
    # imutáveis (SPEC-02 §2.1) e este contrato já foi criado pelo Plano 12;
    # decisão de arquitetura deste plano (ver seção "Architecture Decision").
    payload_cerc = {
        "identificadorContrato": contrato["identificador_contrato"],
        "referenciaExterna": referencia_externa,
        "documentoContratante": contrato["documento_contratante"],
        "cnpjParticipante": financiador_id,
    }
    try:
        resultado = cerc_fn(financiador_id, payload_cerc, correlacao_id=referencia_externa)
    except CercApiError:
        logger.exception(
            "[OperacaoPosRegistro] CERC respondeu erro (financiador=%s, referencia=%s, operacao=%s)",
            financiador_id, referencia_externa, tipo_operacao,
        )
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)
    except Exception:
        logger.exception(
            "[OperacaoPosRegistro] falha inesperada ao chamar a CERC (financiador=%s, referencia=%s, operacao=%s)",
            financiador_id, referencia_externa, tipo_operacao,
        )
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)

    protocolo = None
    try:
        if not resultado:
            raise ValueError("resposta 207 da CERC não trouxe nenhum item")
        item = resultado[0]
        protocolo = item.get("protocolo") if isinstance(item, dict) else None
        novo_status = state_machine.estado_apos_207_pos_registro(tipo_operacao, item["status"])

        atualizado = atualizar_status_pos_registro(financiador_id, contrato["id"], novo_status, protocolo)

        get_db(financiador_id).table("contrato_evento").insert({
            "contrato_id": contrato["id"], "tipo": f"operacao_pos_registro_{tipo_operacao}",
            "payload": item, "ocorrido_em": datetime.now(timezone.utc),
        }).execute()

        corpo = {
            "id": atualizado["id"], "status": novo_status,
            "referenciaExterna": referencia_externa, "protocolo": protocolo,
        }
        if novo_status == state_machine.REGISTRADO:
            # A operação em si foi recusada estruturalmente (207 status=1) —
            # devolve por que, mesmo padrão de criar_contrato para
            # REJEITADO_ESTRUTURAL.
            corpo["erros"] = item.get("erros") or []

        status_http = 202 if novo_status != state_machine.REGISTRADO else 422
        return JsonResponse(corpo, status=status_http)
    except Exception:
        logger.exception(
            "[OperacaoPosRegistro] SUBMISSÃO JÁ ACEITA PELA CERC mas falhou ao interpretar/persistir "
            "localmente — CONCILIAR MANUALMENTE (financiador=%s, referencia=%s, operacao=%s, protocolo=%s)",
            financiador_id, referencia_externa, tipo_operacao, protocolo,
        )
        return JsonResponse({
            "erro": "operação submetida à CERC mas não persistida localmente; conciliação manual necessária",
            "referenciaExterna": referencia_externa, "protocolo": protocolo,
        }, status=500)


@require_POST
def inativar_contrato(request, financiador_id: str):
    """POST /api/v1/contratos/<financiador_id>/inativar — tipoOperacao=I."""
    return _operacao_pos_registro(request, financiador_id, "I", cerc_inativar_contrato)


@require_POST
def baixar_contrato(request, financiador_id: str):
    """POST /api/v1/contratos/<financiador_id>/baixar — tipoOperacao=B."""
    return _operacao_pos_registro(request, financiador_id, "B", cerc_baixar_contrato)
```

- [ ] **Step 4: Update `apps/contratos/urls.py`**

Replace the file with:

```python
from django.urls import path, re_path
from . import views

urlpatterns = [
    path("health", views.health),
    re_path(r"^webhooks/contrato/(?P<financiador_id>\d{14})$", views.webhook_contrato),
    path("webhooks/contrato/processar", views.processar_webhook_contrato),
    re_path(r"^contratos/(?P<financiador_id>\d{14})$", views.criar_contrato),
    re_path(r"^contratos/(?P<financiador_id>\d{14})/inativar$", views.inativar_contrato),
    re_path(r"^contratos/(?P<financiador_id>\d{14})/baixar$", views.baixar_contrato),
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest apps/contratos/tests/test_views_pos_registro.py -v`
Expected: PASS. 14 test functions, 13 of them parametrized only over the 2 operations (`I`/`B`) = 26, plus `test_corpo_json_nao_objeto_retorna_400` parametrized over 2 operations × 4 malformed bodies = 8. Total: 34 collected tests.

- [ ] **Step 6: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all pass, including every pre-existing test in `test_views_criar_contrato.py` and `test_views_webhook_processor.py` (Plan 11's webhook processor needs zero changes — verify this explicitly, it's this plan's own version of Plan 13's Step 5 blast-radius check). Full suite was 252 after Task 1 + Task 2 (226 + 25 + 1 = 252); adding 34 from this task should land at 286.

- [ ] **Step 7: Commit**

```bash
git add apps/contratos/views.py apps/contratos/urls.py apps/contratos/tests/test_views_pos_registro.py
git commit -m "feat: POST /api/v1/contratos/<financiador_id>/{inativar,baixar} (SPEC-02 §4, tipoOperacao=I/B)"
```

---

## Self-Review Notes

- **Spec coverage:** design doc §8's Fase 1 list now has `criar` (Plan 12) + `inativar`/`baixar` (this plan) done; `atualizar` and `consultar` remain as their own future plans (deliberately not bundled here — each has its own distinct shape: `atualizar` needs C17 static-field validation, `consultar` is a read-only `POST /contrato/consultar` with no state machine involvement at all). `P`/`R` (resilição) confirmed out of scope for this cycle per the design doc's own "fora deste ciclo, não iniciar código" list.
- **Placeholder scan:** none — every new function has both a success-path and failure-path test; the three-way `situacao_operacao_pos_registro` result has a test for all three outcomes, including the cross-operation conflict case (`baixar` while `INATIVANDO`, `inativar` while `BAIXANDO`) that's easy to miss.
- **Blast radius verification:** Task 3, Step 6 explicitly re-runs `test_views_webhook_processor.py` and the full suite to confirm Plan 11's webhook processor needs zero changes — same verification Plan 13 did for the state-machine change, repeated here for the new endpoints since they're what finally makes `INATIVANDO`/`BAIXANDO` reachable for the first time (Plan 13 built the states but nothing set them; this plan is that "something").
- **Type/name consistency check:** `situacao_operacao_pos_registro` returns bare strings (`"PROSSEGUIR"`/`"REPLAY"`/`"CONFLITO"`), not new module-level constants — deliberate, since these three values are internal control-flow signals for Task 3's view, not domain states that ever get persisted to `contrato.status` or appear in any external payload (unlike `ESTADOS_TERMINAIS`/`ENVIANDO`/etc., which are). Every call site in this plan (Task 3's `_operacao_pos_registro`) compares against the literal strings, matching the test assertions in Task 1.

**Next:** `atualizar_contrato` (`tipoOperacao=A`) is the next design-doc-scoped Fase 1 item — it needs its own validation pass for C17 (static-field immutability) that neither this plan nor Plan 12 built, and reuses `state_machine.estado_apos_207`'s existing `ATUALIZANDO` branch (already implemented since Plan 09/13, never yet called by any view). `consultar_contrato` (`POST /contrato/consultar`) is the other remaining Fase 1 item, and is also what the still-unbuilt `reconciliar_pendentes` SLA job (design doc §6) will need once it exists.
