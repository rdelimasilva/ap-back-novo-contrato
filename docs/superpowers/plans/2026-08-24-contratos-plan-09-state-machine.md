# contratos-service — Plan 09: Contract State Machine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `apps/contratos/state_machine.py` — the single source of truth for every state a contract (and its guarantees) can be in, and which transitions between them are legal, per SPEC-02 §8's diagram and its derived business rules (§5.2).

**Architecture:** Pure functions and string constants — no I/O, no database, no HTTP. Each function takes the current state (and whatever data triggered the transition — a `207` item's `status`, a webhook's `status`, a timeout event, a post-registration `tipoOperacao`) and returns the next state, raising `EstadoInvalidoError` when the caller asks for a transition the diagram doesn't allow (e.g. a webhook arriving for a contract that's already `REGISTRADO`). Callers that actually persist a transition (Plan 10's webhook receiver, Plan 11's reconciliation job, and the internal API views) call these functions first and only then write the result — this module never touches `cerc_requisicao`/`contrato`/`garantia_ur` itself.

**Tech Stack:** Python 3.12 stdlib only — no new dependency.

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§4 — máquina de estados). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §8 (diagrama de estados, SLA de webhook, sub-estado de garantia) and §5.2 ("Regras de negócio derivadas (implementar)"). Series: plan 9 of ~11.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout — this module lives inside `apps/contratos/`, already created there). No dependency on Plan 05/06/07 — this module never touches the database or the CERC API, so it doesn't need `get_db`/`get_cerc_token`/`client`.

## Global Constraints

- State names are the exact uppercase strings SPEC-02 §8 uses (`ENVIANDO`, `AGUARDANDO_WEBHOOK`, `REJEITADO_ESTRUTURAL`, `REGISTRADO`, `REJEITADO`, `PENDENTE_CONCILIACAO`, `ATUALIZANDO`, `INATIVADO`, `BAIXADO`, `RESILIDO_PARCIAL`, `RESILIDO_TOTAL`) and guarantee sub-states (`NAO_APLICAVEL`, `SUFICIENTE`, `INSUFICIENTE`, `EXCESSO`) — these are stored verbatim in `contrato.status`/`contrato.status_garantia` (Plan 02 schema), so a mismatch here is a silent data-model bug, not just a naming preference.
- Per the diagram, a `tipoOperacao = A` (update) submitted against an already-`REGISTRADO` contract waits in `ATUALIZANDO` (not `AGUARDANDO_WEBHOOK`) for its webhook, then converges back to `REGISTRADO` on success — same webhook-success/failure/timeout rules apply to both waiting states.
- SLA numbers (30 min to `PENDENTE_CONCILIACAO`, 2 h to alert) are **not** this module's concern — this module only answers "what state does a timeout transition to," not "has 30 minutes actually passed." The actual timer/scheduling logic belongs to Plan 11 (reconciliation job), which is the caller that knows wall-clock time.
- The four SPEC-02 §5.2 "regras de negócio derivadas" (subgarantido, excedente, insucesso de UR, indicador crítico) are pure predicate functions in this same module — they classify already-known webhook data, they don't decide state transitions on their own (a subgarantido contract is still fully `REGISTRADO`; being subgarantido is orthogonal to the state machine, it's a flag the caller uses to decide whether to also emit a domain event/alert).

---

### Task 1: `apps/contratos/state_machine.py`

**Files:**
- Create: `contratos/apps/contratos/state_machine.py`
- Test: `contratos/apps/contratos/tests/test_state_machine.py`

**Interfaces:**
- Produces: state constants (`ENVIANDO`, `AGUARDANDO_WEBHOOK`, etc.); `EstadoInvalidoError(estado_atual, transicao)`; `estado_apos_207(tipo_operacao, status_207) -> str`; `estado_apos_400() -> str`; `estado_apos_webhook(estado_atual, status_webhook) -> str`; `estado_apos_timeout_sla(estado_atual) -> str`; `estado_apos_operacao_pos_registro(estado_atual, tipo_operacao) -> str`; `sub_estado_garantia(resultado_distribuicao_onus) -> str`; `eh_subgarantido(resultado_distribuicao_onus) -> bool`; `eh_candidato_liberacao_excedente(resultado_distribuicao_onus) -> bool`; `ur_teve_insucesso(indicador_oneracao) -> bool`; `indicador_critico(criticidade) -> bool`. Plan 10 (webhook receiver) and Plan 11 (reconciliation job) import all of these.

- [ ] **Step 1: Write the failing test**

```python
# contratos/apps/contratos/tests/test_state_machine.py
import pytest

from apps.contratos import state_machine as sm


# ENVIANDO -> (207) -> AGUARDANDO_WEBHOOK | ATUALIZANDO | REJEITADO_ESTRUTURAL

def test_estado_apos_207_sucesso_criacao_vai_para_aguardando_webhook():
    assert sm.estado_apos_207(tipo_operacao="C", status_207="0") == sm.AGUARDANDO_WEBHOOK


def test_estado_apos_207_sucesso_atualizacao_vai_para_atualizando():
    assert sm.estado_apos_207(tipo_operacao="A", status_207="0") == sm.ATUALIZANDO


def test_estado_apos_207_erro_estrutural_vai_para_rejeitado_estrutural():
    assert sm.estado_apos_207(tipo_operacao="C", status_207="1") == sm.REJEITADO_ESTRUTURAL


def test_estado_apos_400_vai_para_rejeitado_estrutural():
    assert sm.estado_apos_400() == sm.REJEITADO_ESTRUTURAL


# AGUARDANDO_WEBHOOK / ATUALIZANDO -> (webhook) -> REGISTRADO | REJEITADO

def test_estado_apos_webhook_sucesso_de_aguardando_webhook_vai_para_registrado():
    assert sm.estado_apos_webhook(sm.AGUARDANDO_WEBHOOK, status_webhook="0") == sm.REGISTRADO


def test_estado_apos_webhook_sucesso_de_atualizando_vai_para_registrado():
    assert sm.estado_apos_webhook(sm.ATUALIZANDO, status_webhook="0") == sm.REGISTRADO


def test_estado_apos_webhook_falha_vai_para_rejeitado():
    assert sm.estado_apos_webhook(sm.AGUARDANDO_WEBHOOK, status_webhook="1") == sm.REJEITADO


def test_estado_apos_webhook_de_estado_nao_esperado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_webhook(sm.REGISTRADO, status_webhook="0")


# timeout SLA -> PENDENTE_CONCILIACAO

def test_estado_apos_timeout_sla_de_aguardando_webhook():
    assert sm.estado_apos_timeout_sla(sm.AGUARDANDO_WEBHOOK) == sm.PENDENTE_CONCILIACAO


def test_estado_apos_timeout_sla_de_estado_nao_esperado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_timeout_sla(sm.REGISTRADO)


# REGISTRADO -> (op I/B/P/R) -> estado terminal específico

@pytest.mark.parametrize("tipo_operacao,esperado", [
    ("I", "INATIVADO"),
    ("B", "BAIXADO"),
    ("P", "RESILIDO_PARCIAL"),
    ("R", "RESILIDO_TOTAL"),
])
def test_estado_apos_operacao_pos_registro(tipo_operacao, esperado):
    assert sm.estado_apos_operacao_pos_registro(sm.REGISTRADO, tipo_operacao) == esperado


def test_estado_apos_operacao_pos_registro_de_estado_nao_registrado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_operacao_pos_registro(sm.AGUARDANDO_WEBHOOK, "I")


def test_estado_apos_operacao_pos_registro_com_operacao_invalida_levanta_erro():
    with pytest.raises(ValueError):
        sm.estado_apos_operacao_pos_registro(sm.REGISTRADO, "C")


# Sub-estado de garantia (resultadoDistribuicaoOnus)

@pytest.mark.parametrize("resultado,esperado", [
    ("0", "NAO_APLICAVEL"),
    ("1", "SUFICIENTE"),
    ("2", "INSUFICIENTE"),
    ("3", "EXCESSO"),
])
def test_sub_estado_garantia(resultado, esperado):
    assert sm.sub_estado_garantia(resultado) == esperado


def test_sub_estado_garantia_valor_invalido_levanta_erro():
    with pytest.raises(ValueError):
        sm.sub_estado_garantia("9")


# Regras de negócio derivadas (§5.2)

def test_eh_subgarantido_quando_insuficiente():
    assert sm.eh_subgarantido("2") is True


def test_eh_subgarantido_quando_suficiente_e_falso():
    assert sm.eh_subgarantido("1") is False


def test_eh_candidato_liberacao_excedente_quando_excesso():
    assert sm.eh_candidato_liberacao_excedente("3") is True


def test_eh_candidato_liberacao_excedente_quando_nao_excesso_e_falso():
    assert sm.eh_candidato_liberacao_excedente("1") is False


def test_ur_teve_insucesso_quando_indicador_zero():
    assert sm.ur_teve_insucesso("0") is True


def test_ur_teve_insucesso_quando_indicador_positivo_e_falso():
    assert sm.ur_teve_insucesso("1") is False


def test_indicador_critico_quando_criticidade_alta():
    assert sm.indicador_critico("2") is True
    assert sm.indicador_critico("3") is True


def test_indicador_critico_quando_criticidade_baixa_e_falso():
    assert sm.indicador_critico("0") is False
    assert sm.indicador_critico("1") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_state_machine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.contratos.state_machine'`

- [ ] **Step 3: Write `apps/contratos/state_machine.py`**

```python
"""Máquina de estados do contrato — SPEC-02 §8, regras derivadas em §5.2.

Funções puras — nenhuma chamada a banco/CERC. Quem persiste uma transição
(webhook receiver, job de reconciliação, views internas) chama estas
funções primeiro e só então grava o resultado; este módulo nunca toca
`contrato`/`garantia_ur`/`cerc_requisicao` diretamente.

Diagrama (§8):

    PUT /v15/contratos (C)
              │
              ▼
        ENVIANDO
    ┌─────────┴──────────┐
207 status=0        207 status=1 / 400
    │                     │
    ▼                     ▼
AGUARDANDO_WEBHOOK   REJEITADO_ESTRUTURAL
    │
┌───────────┼────────────────┬─────────────────────┐
webhook    webhook       timeout SLA          (op = A)
status=0   status=1      (sem webhook)             │
    │           │              │                    ▼
    ▼           ▼              ▼              ATUALIZANDO ──► REGISTRADO
REGISTRADO  REJEITADO   PENDENTE_CONCILIACAO
    │
    ├── op I ──► INATIVADO
    ├── op B ──► BAIXADO
    ├── op P ──► RESILIDO_PARCIAL
    └── op R ──► RESILIDO_TOTAL

O SLA em si (30min pra PENDENTE_CONCILIACAO, alerta após 2h) não é
responsabilidade deste módulo — ele só responde "qual o próximo estado
quando um timeout acontece", não "quanto tempo já passou". Isso é do
job de reconciliação (Plano 11), que é quem sabe a hora real.
"""

ENVIANDO = "ENVIANDO"
AGUARDANDO_WEBHOOK = "AGUARDANDO_WEBHOOK"
REJEITADO_ESTRUTURAL = "REJEITADO_ESTRUTURAL"
REGISTRADO = "REGISTRADO"
REJEITADO = "REJEITADO"
PENDENTE_CONCILIACAO = "PENDENTE_CONCILIACAO"
ATUALIZANDO = "ATUALIZANDO"
INATIVADO = "INATIVADO"
BAIXADO = "BAIXADO"
RESILIDO_PARCIAL = "RESILIDO_PARCIAL"
RESILIDO_TOTAL = "RESILIDO_TOTAL"

ESTADOS_TERMINAIS = {
    REJEITADO_ESTRUTURAL, REJEITADO, INATIVADO, BAIXADO, RESILIDO_PARCIAL, RESILIDO_TOTAL,
}

NAO_APLICAVEL = "NAO_APLICAVEL"
SUFICIENTE = "SUFICIENTE"
INSUFICIENTE = "INSUFICIENTE"
EXCESSO = "EXCESSO"


class EstadoInvalidoError(Exception):
    def __init__(self, estado_atual: str, transicao: str):
        self.estado_atual = estado_atual
        self.transicao = transicao
        super().__init__(f"transição '{transicao}' inválida a partir de '{estado_atual}'")


def estado_apos_207(tipo_operacao: str, status_207: str) -> str:
    """§8: 207 status=0 -> AGUARDANDO_WEBHOOK (ou ATUALIZANDO se tipoOperacao=A,
    já que a atualização espera o webhook num ramo próprio do diagrama);
    status=1 -> REJEITADO_ESTRUTURAL."""
    if status_207 == "1":
        return REJEITADO_ESTRUTURAL
    if tipo_operacao == "A":
        return ATUALIZANDO
    return AGUARDANDO_WEBHOOK


def estado_apos_400() -> str:
    """§8: erro estrutural síncrono (HTTP 400) — mesmo destino que 207 status=1."""
    return REJEITADO_ESTRUTURAL


def estado_apos_webhook(estado_atual: str, status_webhook: str) -> str:
    """§8: webhook status=0 -> REGISTRADO (a partir de AGUARDANDO_WEBHOOK ou
    ATUALIZANDO); status=1 -> REJEITADO."""
    if estado_atual not in (AGUARDANDO_WEBHOOK, ATUALIZANDO):
        raise EstadoInvalidoError(estado_atual, f"webhook (status={status_webhook})")
    return REGISTRADO if status_webhook == "0" else REJEITADO


def estado_apos_timeout_sla(estado_atual: str) -> str:
    """§8: nenhum webhook em 30min (configurável) -> PENDENTE_CONCILIACAO."""
    if estado_atual not in (AGUARDANDO_WEBHOOK, ATUALIZANDO):
        raise EstadoInvalidoError(estado_atual, "timeout SLA")
    return PENDENTE_CONCILIACAO


_OPERACAO_PARA_ESTADO_POS_REGISTRO = {
    "I": INATIVADO,
    "B": BAIXADO,
    "P": RESILIDO_PARCIAL,
    "R": RESILIDO_TOTAL,
}


def estado_apos_operacao_pos_registro(estado_atual: str, tipo_operacao: str) -> str:
    """§8: a partir de REGISTRADO, tipoOperacao I/B/P/R leva a um estado
    terminal específico."""
    if estado_atual != REGISTRADO:
        raise EstadoInvalidoError(estado_atual, f"operação {tipo_operacao}")
    if tipo_operacao not in _OPERACAO_PARA_ESTADO_POS_REGISTRO:
        raise ValueError(f"tipoOperacao '{tipo_operacao}' não leva a um estado pós-registro")
    return _OPERACAO_PARA_ESTADO_POS_REGISTRO[tipo_operacao]


_RESULTADO_PARA_SUBESTADO = {
    "0": NAO_APLICAVEL,
    "1": SUFICIENTE,
    "2": INSUFICIENTE,
    "3": EXCESSO,
}


def sub_estado_garantia(resultado_distribuicao_onus: str) -> str:
    """§8: sub-estado de garantia derivado de resultadoDistribuicaoOnus."""
    try:
        return _RESULTADO_PARA_SUBESTADO[resultado_distribuicao_onus]
    except KeyError:
        raise ValueError(f"resultadoDistribuicaoOnus inválido: {resultado_distribuicao_onus}")


def eh_subgarantido(resultado_distribuicao_onus: str) -> bool:
    """§5.2: resultadoDistribuicaoOnus=2 (insuficiente) -> contrato registrado mas
    subgarantido; o caller deve emitir o evento de domínio ContratoSubgarantido
    e alertar a operação/crédito."""
    return resultado_distribuicao_onus == "2"


def eh_candidato_liberacao_excedente(resultado_distribuicao_onus: str) -> bool:
    """§5.2: resultadoDistribuicaoOnus=3 (em excesso) -> candidato a liberação de
    excedente (AP026, fora do escopo desta fase) — apenas sinaliza."""
    return resultado_distribuicao_onus == "3"


def ur_teve_insucesso(indicador_oneracao: str) -> bool:
    """§5.2: indicadorOneracao=0 numa UR -> insucesso naquela UR; o caller deve
    contabilizar e expor no detalhe."""
    return indicador_oneracao == "0"


def indicador_critico(criticidade: str) -> bool:
    """§5.2: criticidade >= 2 em qualquer indicador de consistência -> destacar
    na resposta interna e notificar o time de crédito."""
    return int(criticidade) >= 2
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apps/contratos/tests/test_state_machine.py -v`
Expected: PASS (22 tests) — pure functions, no database/network.

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/contratos/state_machine.py apps/contratos/tests/test_state_machine.py
git commit -m "feat: contract state machine (SPEC-02 §8) + derived business rules (§5.2)"
```

---

## Self-Review Notes

- **Spec coverage:** every edge in the §8 diagram (`ENVIANDO`→`AGUARDANDO_WEBHOOK`/`ATUALIZANDO`→`REJEITADO_ESTRUTURAL`, webhook success/failure, timeout SLA, the four post-registration operations) and all four §5.2 derived business rules (subgarantido, excedente, UR insucesso, indicador crítico) are covered by name, with the exact state-name strings the Plan 02 schema's `contrato.status` column will store.
- **Placeholder scan:** none — every transition function is a real implementation with a real test, including the illegal-transition guard (`EstadoInvalidoError`) which has its own negative test rather than being an unused defensive branch.
- **Type consistency:** all state/sub-state constants are plain strings matching SPEC-02 §8's names exactly (not an `enum.Enum` — kept consistent with how `contrato.status`/`status_garantia` are plain `TEXT` columns in Plan 02's schema, no serialization step needed between this module and the database). `EstadoInvalidoError(estado_atual, transicao)` is the exception type Plan 10/11 must catch or let propagate.

**Next:** `2026-08-24-contratos-plan-10-webhook.md` (webhook receiver — `POST /api/v1/webhooks/contrato`, `webhook_inbox`, Pub/Sub publish).
