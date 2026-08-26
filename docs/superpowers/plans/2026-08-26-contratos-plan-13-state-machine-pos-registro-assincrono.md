# contratos-service — Plan 13: Post-Registration Operations Wait for Webhook Confirmation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix a real inconsistency between `apps/contratos/state_machine.py` (Plan 09) and SPEC-02 §0's core rule ("nenhum contrato pode ser considerado registrado com base no HTTP 207" — extended here to mean "no post-registration operation is definitive before the CERC's webhook confirms it either"). Today, `estado_apos_operacao_pos_registro` takes `tipoOperacao=I/B/P/R` (inativar/baixar/resilição parcial/total) and returns the TERMINAL state (`INATIVADO`/`BAIXADO`/etc.) directly, with no waiting period — unlike `tipoOperacao=A`, which correctly waits in `ATUALIZANDO` for its own webhook. This plan makes all four post-registration operations wait the same way `A` already does, confirmed as the intended behavior with the user (see Architecture Decision below) rather than assumed.

**Architecture:** This is a pure-logic-only change — `apps/contratos/state_machine.py` has zero I/O, so this plan touches exactly that one module and its test file. No database, no view, no other module changes. It is deliberately scoped this small: resolving the architecture question is a prerequisite for a future plan that builds the actual `inativar`/`baixar`/`resilir` internal API endpoints (mirroring Plan 12's `criar_contrato`) — that future plan needs a settled, correct state machine to build on, and shouldn't have to make this call as a side effect of unrelated view code.

**Architecture Decision (confirmed with the user, not assumed):** post-registration operations (`I`/`B`/`P`/`R`) wait for webhook confirmation before being considered definitive — the same semantics `A` (update) already has. A failed confirmation (`status=1`) does **not** reject the underlying contract (unlike a failed creation/update, where `REJEITADO` is correct) — it returns the contract to `REGISTRADO`, since the contract's original registration is still valid; only the attempted post-registration operation itself didn't take effect. This was a genuine product decision, not a technical detail — the rejected alternative (mark the operation as done immediately, synchronously, per the SPEC-02 §8 diagram's literal arrows) would let this system report a contract as `INATIVADO` even if the CERC later refuses that inativação, a real business-correctness risk the user chose not to accept.

**A design property worth calling out because it simplifies the blast radius of this change:** the new waiting states (`INATIVANDO`/`BAIXANDO`/`RESILINDO_PARCIAL`/`RESILINDO_TOTAL`) fully encode which operation is pending — a caller resolving a webhook against one of these states doesn't need to separately know or pass `tipoOperacao`, because `estado_atual` already tells `estado_apos_webhook` everything it needs to pick the right success/failure targets. This means **Plan 11's webhook processor (`apps/contratos/views.py::processar_webhook_contrato`) needs zero code changes** — it already calls `state_machine.estado_apos_webhook(contrato["status"], evento["status"])` generically, and that call will correctly handle the four new states the moment this plan lands, with no other file touched. Verified by reading that call site directly (see Global Constraints).

**Known, deliberately out-of-scope follow-up risk (for whichever future plan builds the `inativar`/`baixar` API and makes these states reachable for the first time):** `apps/contratos/webhook_processor.py::atualizacoes_contrato_do_evento` (Plan 11) reads `evento["resultadoDistribuicaoOnus"]` unconditionally when `evento["status"] == "0"` — a field that makes sense for a creation/update confirmation (URs reached, distribution result) but whose presence in an inativação/baixa confirmation's webhook payload is **not confirmed by anything in SPEC-02** read so far. If that field is absent from an inativação webhook, `apps/contratos/views.py::processar_webhook_contrato`'s existing quarantine `try/except` (added in Plan 11's own final review) will catch the resulting `KeyError`, mark the `webhook_inbox` row `processado_em`+`erro`, and return `204` — so this fails *safely* (no crash, no data corruption), but the contract would then sit forever in `INATIVANDO`/etc. with no further progress until a future plan teaches `webhook_processor.py` to distinguish this payload shape. This plan does not attempt that — these states aren't reachable by anything yet (no endpoint sets a contract to `INATIVANDO` today), so the risk is dormant until the future `inativar`/`baixar` API plan makes it live; that plan's own brief should re-read this paragraph before assuming Plan 11's webhook processor "just works" for these states.

**Tech Stack:** Pure Python, no new dependencies.

**Spec:** `SPEC-02-criacao-de-contratos-ap007.md` §0 (the async rule this fixes an inconsistency with), §8 (state diagram — note its literal arrows for I/B/P/R are a simplification this plan deliberately departs from, per the confirmed Architecture Decision above).

**Depends on:** `2026-08-24-contratos-plan-09-state-machine.md` (the module and tests this plan modifies — every function's existing behavior for `AGUARDANDO_WEBHOOK`/`ATUALIZANDO`/creation/update must be preserved exactly; this plan only adds new behavior for `I`/`B`/`P`/`R`, it does not change `C`/`A` semantics at all).

## Global Constraints

- `apps/contratos/views.py::processar_webhook_contrato` (Plan 11) is verified to need NO changes: its call `state_machine.estado_apos_webhook(contrato["status"], evento["status"])` is generic over `estado_atual` and will automatically support the four new states once `state_machine.py`'s internal transition table is extended. Do not modify `views.py` in this plan — if a task's own testing suggests it needs to, stop and reconsider, since that would mean this plan's design assumption was wrong.
- Every existing test in `apps/contratos/tests/test_state_machine.py` for `AGUARDANDO_WEBHOOK`/`ATUALIZANDO`-related behavior (creation/update flows) must keep passing UNCHANGED — this plan only adds new transitions and generalizes internal implementation, it never changes `C`/`A` semantics.
- The one EXISTING test that must change its expected values (not its existence): `test_estado_apos_operacao_pos_registro` currently asserts `estado_apos_operacao_pos_registro(REGISTRADO, tipo_operacao)` returns the terminal state directly (`"INATIVADO"`, `"BAIXADO"`, etc.) — this plan changes that function's return values to the new WAITING states (`"INATIVANDO"`, `"BAIXANDO"`, etc.), so this specific test's expected values must be updated to match. No other existing test's expected value changes.
- State name convention: waiting states use the `-NDO`/`-INDO` gerund suffix already established by `ATUALIZANDO` (not, say, `AGUARDANDO_INATIVACAO` or similar) — `INATIVANDO`, `BAIXANDO`, `RESILINDO_PARCIAL`, `RESILINDO_TOTAL`. `ESTADOS_TERMINAIS` (the terminal-state set) does NOT change — the four terminal states (`INATIVADO`/`BAIXADO`/`RESILIDO_PARCIAL`/`RESILIDO_TOTAL`) are still reached, just no longer immediately.

---

### Task 1: extend `apps/contratos/state_machine.py` with waiting states for I/B/P/R

**Files:**
- Modify: `contratos/apps/contratos/state_machine.py`
- Modify: `contratos/apps/contratos/tests/test_state_machine.py`

**Interfaces:**
- Produces: four new state constants `INATIVANDO`, `BAIXANDO`, `RESILINDO_PARCIAL`, `RESILINDO_TOTAL`; a new module-level set `ESTADOS_AGUARDANDO_WEBHOOK` (all six states a contract can be waiting in: `AGUARDANDO_WEBHOOK`, `ATUALIZANDO`, and the four new ones) for `estado_apos_timeout_sla`'s validity check. `estado_apos_operacao_pos_registro(estado_atual, tipo_operacao) -> str` now returns the WAITING state, not the terminal one (breaking change to this function's return values, not its signature — no other module in this codebase calls it yet, confirmed by grep before writing this plan, so this is safe). `estado_apos_webhook(estado_atual, status_webhook) -> str` now accepts all six waiting states as valid `estado_atual` input (previously only two), and picks the correct success/failure target pair based on which waiting state it's resolving. `estado_apos_timeout_sla(estado_atual) -> str` now accepts all six waiting states (previously only two) — still always returns `PENDENTE_CONCILIACAO` regardless of which one.

- [ ] **Step 1: Write the failing/updated tests**

Replace the full contents of `apps/contratos/tests/test_state_machine.py` with:

```python
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


def test_estado_apos_webhook_falha_de_atualizando_vai_para_rejeitado():
    assert sm.estado_apos_webhook(sm.ATUALIZANDO, status_webhook="1") == sm.REJEITADO


def test_estado_apos_webhook_de_estado_nao_esperado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_webhook(sm.REGISTRADO, status_webhook="0")


# INATIVANDO / BAIXANDO / RESILINDO_PARCIAL / RESILINDO_TOTAL -> (webhook) ->
# terminal específico (sucesso) | REGISTRADO (falha — a operação pós-registro
# não vingou, mas o contrato original continua registrado)

@pytest.mark.parametrize("estado_espera,terminal_sucesso", [
    (sm.INATIVANDO, sm.INATIVADO),
    (sm.BAIXANDO, sm.BAIXADO),
    (sm.RESILINDO_PARCIAL, sm.RESILIDO_PARCIAL),
    (sm.RESILINDO_TOTAL, sm.RESILIDO_TOTAL),
])
def test_estado_apos_webhook_sucesso_de_operacao_pos_registro_vai_para_terminal(estado_espera, terminal_sucesso):
    assert sm.estado_apos_webhook(estado_espera, status_webhook="0") == terminal_sucesso


@pytest.mark.parametrize("estado_espera", [
    sm.INATIVANDO, sm.BAIXANDO, sm.RESILINDO_PARCIAL, sm.RESILINDO_TOTAL,
])
def test_estado_apos_webhook_falha_de_operacao_pos_registro_volta_para_registrado(estado_espera):
    assert sm.estado_apos_webhook(estado_espera, status_webhook="1") == sm.REGISTRADO


# timeout SLA -> PENDENTE_CONCILIACAO (de qualquer estado de espera)

def test_estado_apos_timeout_sla_de_aguardando_webhook():
    assert sm.estado_apos_timeout_sla(sm.AGUARDANDO_WEBHOOK) == sm.PENDENTE_CONCILIACAO


def test_estado_apos_timeout_sla_de_atualizando():
    assert sm.estado_apos_timeout_sla(sm.ATUALIZANDO) == sm.PENDENTE_CONCILIACAO


@pytest.mark.parametrize("estado_espera", [
    sm.INATIVANDO, sm.BAIXANDO, sm.RESILINDO_PARCIAL, sm.RESILINDO_TOTAL,
])
def test_estado_apos_timeout_sla_de_operacao_pos_registro(estado_espera):
    assert sm.estado_apos_timeout_sla(estado_espera) == sm.PENDENTE_CONCILIACAO


def test_estado_apos_timeout_sla_de_estado_nao_esperado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_timeout_sla(sm.REGISTRADO)


# REGISTRADO -> (op I/B/P/R) -> estado de ESPERA (não mais terminal direto —
# mudança deste plano: a operação pós-registro também espera o webhook,
# igual à atualização)

@pytest.mark.parametrize("tipo_operacao,esperado", [
    ("I", "INATIVANDO"),
    ("B", "BAIXANDO"),
    ("P", "RESILINDO_PARCIAL"),
    ("R", "RESILINDO_TOTAL"),
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

- [ ] **Step 2: Run tests to verify the new/changed ones fail**

Run: `pytest apps/contratos/tests/test_state_machine.py -v`
Expected: FAIL — `test_estado_apos_operacao_pos_registro` fails (asserts old terminal values against what the current code still returns, which happens to already match — so this one currently PASSES; it will start testing the NEW expectation once you also update the module in Step 3, and must still pass after). The genuinely NEW tests (`test_estado_apos_webhook_falha_de_atualizando_vai_para_rejeitado`, the two parametrized `..._de_operacao_pos_registro` webhook tests, and the parametrized timeout test) FAIL with `AttributeError: module 'apps.contratos.state_machine' has no attribute 'INATIVANDO'` (the new constants don't exist yet).

- [ ] **Step 3: Update `apps/contratos/state_machine.py`**

Replace the full contents of `apps/contratos/state_machine.py` with:

```python
"""Máquina de estados do contrato — SPEC-02 §8, regras derivadas em §5.2.

Funções puras — nenhuma chamada a banco/CERC. Quem persiste uma transição
(webhook receiver, job de reconciliação, views internas) chama estas
funções primeiro e só então grava o resultado; este módulo nunca toca
`contrato`/`garantia_ur`/`cerc_requisicao` diretamente.

Diagrama (§8, com uma correção do Plano 13 — ver abaixo):

    PUT /v15/contratos (C)
              |
              v
        ENVIANDO
    +---------+----------+
207 status=0        207 status=1 / 400
    |                     |
    v                     v
AGUARDANDO_WEBHOOK   REJEITADO_ESTRUTURAL
    |
+-----------+----------------+---------------------+
webhook    webhook       timeout SLA          (op = A)
status=0   status=1      (sem webhook)             |
    |           |              |                    v
    v           v              v              ATUALIZANDO -> (webhook) -> REGISTRADO | REJEITADO
REGISTRADO  REJEITADO   PENDENTE_CONCILIACAO
    |
    +-- op I --> INATIVANDO        -> (webhook) -> INATIVADO        | REGISTRADO
    +-- op B --> BAIXANDO          -> (webhook) -> BAIXADO          | REGISTRADO
    +-- op P --> RESILINDO_PARCIAL -> (webhook) -> RESILIDO_PARCIAL | REGISTRADO
    +-- op R --> RESILINDO_TOTAL   -> (webhook) -> RESILIDO_TOTAL   | REGISTRADO

Correção do Plano 13: a SPEC-02 §8 desenha "op I/B/P/R" indo direto de
REGISTRADO pro estado terminal, sem espera — mas isso contradiz a regra
central da §0 ("nenhuma operação é definitiva antes da confirmação
assíncrona"). Decisão confirmada com o usuário (não assumida): operações
pós-registro esperam o webhook igual à atualização (op=A) já espera hoje.
Uma falha na confirmação NÃO rejeita o contrato original (ele já estava
REGISTRADO antes da tentativa) — volta pra REGISTRADO, só a operação
específica não se efetivou.

O SLA em si (30min pra PENDENTE_CONCILIACAO, alerta após 2h) não é
responsabilidade deste módulo — ele só responde "qual o próximo estado
quando um timeout acontece", não "quanto tempo já passou". Isso é do
job de reconciliação (Plano futuro), que é quem sabe a hora real.
"""

ENVIANDO = "ENVIANDO"
AGUARDANDO_WEBHOOK = "AGUARDANDO_WEBHOOK"
REJEITADO_ESTRUTURAL = "REJEITADO_ESTRUTURAL"
REGISTRADO = "REGISTRADO"
REJEITADO = "REJEITADO"
PENDENTE_CONCILIACAO = "PENDENTE_CONCILIACAO"
ATUALIZANDO = "ATUALIZANDO"
INATIVANDO = "INATIVANDO"
BAIXANDO = "BAIXANDO"
RESILINDO_PARCIAL = "RESILINDO_PARCIAL"
RESILINDO_TOTAL = "RESILINDO_TOTAL"
INATIVADO = "INATIVADO"
BAIXADO = "BAIXADO"
RESILIDO_PARCIAL = "RESILIDO_PARCIAL"
RESILIDO_TOTAL = "RESILIDO_TOTAL"

ESTADOS_TERMINAIS = {
    REJEITADO_ESTRUTURAL, REJEITADO, INATIVADO, BAIXADO, RESILIDO_PARCIAL, RESILIDO_TOTAL,
}

ESTADOS_AGUARDANDO_WEBHOOK = {
    AGUARDANDO_WEBHOOK, ATUALIZANDO, INATIVANDO, BAIXANDO, RESILINDO_PARCIAL, RESILINDO_TOTAL,
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


_TRANSICOES_WEBHOOK = {
    AGUARDANDO_WEBHOOK: (REGISTRADO, REJEITADO),
    ATUALIZANDO: (REGISTRADO, REJEITADO),
    INATIVANDO: (INATIVADO, REGISTRADO),
    BAIXANDO: (BAIXADO, REGISTRADO),
    RESILINDO_PARCIAL: (RESILIDO_PARCIAL, REGISTRADO),
    RESILINDO_TOTAL: (RESILIDO_TOTAL, REGISTRADO),
}


def estado_apos_webhook(estado_atual: str, status_webhook: str) -> str:
    """§8: webhook status=0 -> sucesso; status=1 -> falha. O par
    (sucesso, falha) depende de QUAL espera está sendo resolvida — carregado
    no próprio `estado_atual`, então o caller nunca precisa saber qual
    tipoOperacao originou a espera (Plano 11's webhook processor não precisa
    de nenhuma mudança por causa disso — ver design do Plano 13).

    Para criação/atualização (AGUARDANDO_WEBHOOK/ATUALIZANDO), uma falha
    derruba pra REJEITADO — o registro nunca existiu de verdade. Para
    operações pós-registro (INATIVANDO/BAIXANDO/RESILINDO_*), uma falha NÃO
    rejeita o contrato — ele já estava REGISTRADO antes da tentativa, então
    volta pra REGISTRADO; só a operação específica não se efetivou."""
    try:
        sucesso, falha = _TRANSICOES_WEBHOOK[estado_atual]
    except KeyError:
        raise EstadoInvalidoError(estado_atual, f"webhook (status={status_webhook})")
    return sucesso if status_webhook == "0" else falha


def estado_apos_timeout_sla(estado_atual: str) -> str:
    """§8: nenhum webhook em 30min (configurável) -> PENDENTE_CONCILIACAO,
    de qualquer um dos estados de espera (criação, atualização ou operação
    pós-registro — o timeout não distingue qual)."""
    if estado_atual not in ESTADOS_AGUARDANDO_WEBHOOK:
        raise EstadoInvalidoError(estado_atual, "timeout SLA")
    return PENDENTE_CONCILIACAO


_OPERACAO_PARA_ESTADO_ESPERA = {
    "I": INATIVANDO,
    "B": BAIXANDO,
    "P": RESILINDO_PARCIAL,
    "R": RESILINDO_TOTAL,
}


def estado_apos_operacao_pos_registro(estado_atual: str, tipo_operacao: str) -> str:
    """§8: a partir de REGISTRADO, tipoOperacao I/B/P/R entra num estado de
    ESPERA pelo webhook que confirma a operação — mesmo raciocínio de
    ATUALIZANDO: nenhuma operação pós-registro é definitiva antes da
    confirmação assíncrona (SPEC-02 §0, decisão confirmada no Plano 13).
    Não retorna mais o estado terminal diretamente."""
    if estado_atual != REGISTRADO:
        raise EstadoInvalidoError(estado_atual, f"operação {tipo_operacao}")
    if tipo_operacao not in _OPERACAO_PARA_ESTADO_ESPERA:
        raise ValueError(f"tipoOperacao '{tipo_operacao}' não leva a um estado pós-registro")
    return _OPERACAO_PARA_ESTADO_ESPERA[tipo_operacao]


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

- [ ] **Step 4: Run tests to verify they all pass**

Run: `pytest apps/contratos/tests/test_state_machine.py -v`
Expected: PASS (43 collected tests total — the original file collected 29; this plan adds 1 new single test (`..._falha_de_atualizando_vai_para_rejeitado`), 1 new 4-way parametrized test (`..._sucesso_de_operacao_pos_registro_vai_para_terminal`), 1 new 4-way parametrized test (`..._falha_de_operacao_pos_registro_volta_para_registrado`), 1 new single test (`..._timeout_sla_de_atualizando`), and 1 new 4-way parametrized test (`..._timeout_sla_de_operacao_pos_registro`) — net +14 collected tests. The one pre-existing parametrized test whose VALUES change, not its count, is `test_estado_apos_operacao_pos_registro`, still 4 cases.)

- [ ] **Step 5: Verify Plan 11's webhook processor genuinely needs no changes**

Run: `pytest apps/contratos/tests/test_views_webhook_processor.py -v` — this file's tests exercise `processar_webhook_contrato`, which calls `state_machine.estado_apos_webhook` generically. Confirm every existing test in that file still passes with ZERO changes to `apps/contratos/views.py` — this is the concrete proof that the "no other file needs to change" design claim in this plan's Architecture section actually holds, not just a hopeful assertion.

Expected: PASS, unchanged, no modifications needed to `views.py`.

- [ ] **Step 6: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass. Full suite was 212 before this plan; `test_state_machine.py` alone goes from 29 to 43 collected tests (net +14), so the full suite total should land at 226.

- [ ] **Step 7: Commit**

```bash
git add apps/contratos/state_machine.py apps/contratos/tests/test_state_machine.py
git commit -m "fix: post-registration operations (I/B/P/R) wait for webhook confirmation, not immediate (SPEC-02 §0)"
```

---

## Self-Review Notes

- **Spec coverage:** the inconsistency between SPEC-02 §0 (async rule) and §8's literal diagram (immediate I/B/P/R transitions) is resolved per an explicit, confirmed product decision, not a unilateral technical call — documented in the plan's own Architecture Decision section and in the module's docstring for future readers. All four operations (I/B/P/R) get symmetric treatment (wait → success/failure pair), matching how `A` already works. The failure-returns-to-REGISTRADO (not REJEITADO) semantics for post-registration operations is a deliberate, documented design choice with its own rationale (the original registration is still valid even if a later operation on it fails).
- **Placeholder scan:** none — every new state has both a success-path and failure-path test, plus the timeout-from-every-waiting-state case.
- **Blast radius verification:** confirmed (Step 5) that `apps/contratos/views.py::processar_webhook_contrato` (Plan 11) needs zero changes — its generic call to `estado_apos_webhook` transparently gains support for the four new states. No other module in this codebase calls `estado_apos_operacao_pos_registro` yet (verified by grep before writing this plan), so changing its return values from terminal states to waiting states is safe — there is no existing caller whose behavior silently changes.

**Next:** the future plan that builds `POST /api/v1/contratos/<financiador_id>/inativar` (or however it ends up routed — mirroring Plan 12's `criar_contrato` shape) must (a) re-read this plan's "Known, deliberately out-of-scope follow-up risk" paragraph about `webhook_processor.py::atualizacoes_contrato_do_evento` before assuming the webhook confirmation path "just works" for these operations, and (b) decide what a "baixar"/"resilir" request payload actually needs to contain (SPEC-02 §4.1's `tipoOperacao` enum covers this, but the request-shape details for `B`/`P`/`R` specifically haven't been re-read closely since the original SPEC-02 read at the start of this project — worth a fresh pass before writing that plan's request-validation logic).
