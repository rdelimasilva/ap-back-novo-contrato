# contratos-service — Plan 12: Criar Contrato (API interna) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /api/v1/contratos/<financiador_id>` — the first internal-facing endpoint that actually creates a contract: validates locally (C01–C20, minus two documented gaps), calls `services.cerc.client.criar_contrato`, interprets the `207`, and persists the initial `contrato`/`garantia`/`contrato_parcela`/`contrato_contrato_anterior`/`contrato_domicilio` rows. This is the piece that was missing to make Plans 10/11's webhook pipeline do anything in practice — until now nothing in this codebase ever inserted a `contrato` row.

**Architecture:** Same three-piece shape as Plans 10/11: a pure validation-orchestration module, a DB-write module, and a thin view tying them together with the already-built `services.cerc.client`/`apps.contratos.state_machine`. Scope is deliberately narrow — **create only** (`tipoOperacao=C`). Update/inativar/baixar/resilição are explicitly out of scope for this plan (see Global Constraints for why).

Four architecture decisions this plan locks in, because they aren't obvious from the spec text or from a single task's diff:

1. **Tenant routing via URL, same as the webhook receiver.** `POST /api/v1/contratos/<financiador_id>` — confirmed with the user rather than assumed: real internal authentication/authorization (JWT with a `financiador_id` claim, matching how `ap-back-optin`'s Plan 08 already does it for its own internal API) is **explicitly deferred to a future plan**. Until that lands, this endpoint has no caller-identity check beyond knowing the URL — acceptable for now because nothing outside this codebase calls it yet, but it must not be treated as production-ready authn/authz. Track this as a known gap, not a silent omission.
2. **Idempotency via `referencia_externa`'s existing `UNIQUE` constraint, not a new `Idempotency-Key` mechanism.** `contrato.referencia_externa` is already `UNIQUE NOT NULL` (Plan 02's schema). Rather than building a separate `Idempotency-Key` header + response-cache table (which `ap-back-optin` has and this service doesn't yet), this plan reuses the CERC's own idempotency reasoning (SPEC-02 §7.3, error `107803 Contrato já informado`: "Idempotência: reconciliar e tratar como sucesso") one level up: if a `POST` arrives for a `referenciaExterna` that already has a `contrato` row, the view returns that row's current state instead of re-submitting to the CERC. `correlacao_id` passed to `services.cerc.client.criar_contrato` is the `referenciaExterna` itself — a natural, already-unique key, reusing the exact same "catch the DB's own unique-violation" pattern Plan 10 already established for `webhook_inbox.hash_dedupe`.
3. **`contrato_domicilio` is one row per CONTRACT, but the request carries `domicilioPagamento` per GARANTIA — a mismatch inherited verbatim from SPEC-02 itself.** SPEC-02 §4.2 puts `domicilioPagamento` inside each `garantias[]` entry (§4.3), but §11's schema (copied near-literally into `sql/schema/01-contratos-schema.sql`) gives `contrato_domicilio` a single-row-per-contract shape (`contrato_id TEXT PRIMARY KEY`). This plan does not redesign the schema (that's a bigger, separate decision this plan isn't authorized to make unilaterally) — it writes `contrato_domicilio` from the **first** garantia's `domicilioPagamento` and documents this as a known simplification. Every example in SPEC-02 (§4.5) only shows one garantia with one domicílio, so this covers the common case; a contract with multiple garantias whose domicílios genuinely differ will silently only persist the first one's domicílio locally (the CERC itself still receives and processes each garantia's own domicílio correctly — this gap is purely in what our own read-side database reflects).
4. **C14 (SLC participant list) is out of scope — no data source exists anywhere in this codebase**, not even an empty table. Building one means inventing where that list comes from (a CERC endpoint? A manually maintained config? Neither is specified anywhere in the specs this project has), which is a separate, real feature — not a validation-orchestration detail. **C19 (arranjo domain) IS implemented** (it queries the pre-existing `dominio_arranjo` table), but until a future `sincronizar_dominio_arranjo` job populates that table, it will reject every arranjo code except the `"99T"` wildcard — a known, temporary *operational* gap (correct code, empty data), not a defect in this plan.

**Tech Stack:** Django function-based view, `decimal.Decimal` for every monetary value (design §13.3's explicit, test-verified "no `float`/`double` in a value column" requirement — the validation orchestrator is where JSON `float`s get converted to `Decimal` for the first time in this codebase's create path), stdlib `datetime.date`/`uuid`.

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` §4 ("API interna... Fase 1: criar, atualizar, inativar, baixar, consultar"). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §4 (request shape, AP007A/AP007B), §9 (C01–C20), §11 (schema). Series: plan 12 of ~14.

**Depends on:** `2026-08-24-contratos-plan-07-cerc-client.md` (`services.cerc.client.criar_contrato`), `2026-08-24-contratos-plan-08-validation.md` (the individual `validar_cXX` functions this plan orchestrates — none of them are modified, only called), `2026-08-24-contratos-plan-09-state-machine.md` (`state_machine.estado_apos_207`), `2026-08-25-contratos-plan-10-webhook-receiver.md`/`-11-webhook-processor.md` (the async pipeline this plan finally feeds real data into).

## Global Constraints

- **This plan is create-only.** `atualizar`/`inativar`/`baixar`/`resilir` are NOT built here. Reasons: (a) update needs C17 (already implemented) but also needs a real design decision about auth/routing this plan already spent its one architecture question on; (b) inativar/baixar/resilir raise a genuine, unresolved question about `apps/contratos/state_machine.py`'s `estado_apos_operacao_pos_registro` — per that function's own design (Plan 09), a `REGISTRADO` contract moves DIRECTLY to a terminal state (`INATIVADO`/`BAIXADO`/etc.) on `tipoOperacao=I/B/P/R`, with no intermediate waiting-for-webhook state, which is worth re-examining against SPEC-02 §0's "everything is async" rule before building an API on top of it — that's a design question for a dedicated future plan, not something to resolve as a side effect of this one.
- Monetary values: `decimal.Decimal` end-to-end from the moment a JSON number is read, never a Python `float`, per design §13.3 (verified by test in this plan). Convert with `Decimal(str(valor))`, never `Decimal(valor)` directly on a float (which would preserve float's binary-imprecision artifacts).
- No Django ORM — all reads/writes go through `shared.cloudsql_client.get_db(financiador_id)`.
- `cnpjParticipante` is never read from the request body — it's always `financiador_id` from the URL (design §1.1: "financiador_id = o próprio cnpjParticipante"). A request body that includes a conflicting `cnpjParticipante` is ignored, not validated against — there's nothing to check it against since the URL is authoritative.
- Existing validators in `apps/contratos/validation.py` (C01–C20, Plan 08) are used exactly as they are — this plan adds a NEW orchestrating function that calls them in sequence; it does not modify any existing `validar_cXX` function.

---

### Task 1: `apps/contratos/contrato_validation_orquestrador.py` — validation orchestration

**Files:**
- Create: `contratos/apps/contratos/contrato_validation_orquestrador.py`
- Test: `contratos/apps/contratos/tests/test_contrato_validation_orquestrador.py`

**Interfaces:**
- Consumes: every `validar_cXX` function already in `apps/contratos/validation.py` (Plan 08) — `validar_c01_documento`, `validar_c02_repactuacao`, `validar_c03_repactuacao_sem_garantias`, `validar_c04_valor_monetario`, `validar_c05_modalidade_parcelado`, `validar_c06_tipo_distribuicao`, `validar_c07_regra_divisao_percentual`, `validar_c08_data_inicio_futura`, `validar_c09_ordem_datas`, `validar_c10_raiz_titular_igual_ufr`, `validar_c11_raiz_documento_unico`, `validar_c12_referencia_garantia_unica`, `validar_c13_sem_sobreposicao_garantias`, `validar_c15_numero_conta`, `validar_c16_domicilio_formatos`, `validar_c18_bloqueio_judicial`, `validar_c19_arranjos_no_dominio`, and `ValidationError`, `tipo_documento`.
- Produces: `validar_criacao_contrato(payload: dict, *, hoje, ativos_arranjos: set) -> dict` — raises `apps.contratos.validation.ValidationError` on the first rule violated (fail-fast, matching every individual `validar_cXX`'s own behavior); on success, returns a NEW dict (does not mutate `payload`) with every monetary field converted from whatever JSON produced (`int`/`float`) to `decimal.Decimal`, and every date string converted to `datetime.date`, ready for Task 2 to write directly to the database. Task 3 imports this.

- [ ] **Step 1: Write the failing test**

```python
# contratos/apps/contratos/tests/test_contrato_validation_orquestrador.py
from datetime import date
from decimal import Decimal

import pytest

from apps.contratos.contrato_validation_orquestrador import validar_criacao_contrato
from apps.contratos.validation import ValidationError

HOJE = date(2026, 8, 25)
ATIVOS_ARRANJOS = {"VCC", "MCC"}


def _payload_valido(**overrides):
    base = {
        "referenciaExterna": "CTR-2026-000001",
        "identificadorContrato": "OP-88231",
        "documentoContratante": "22751826000125",
        "repactuacao": "0",
        "identificacaoContratosAnteriores": [],
        "cnpjDetentor": "12345678000199",
        "tipoEfeito": "2",
        "saldoDevedor": 150000.00,
        "limiteOperacaoGarantida": 200000.00,
        "valorMantido": 180000.00,
        "dataAssinatura": "2026-08-15",
        "dataVencimento": "2027-08-15",
        "identificacaoGestaoEntidadeRegistradora": "1",
        "modalidadeOperacao": "2",
        "parcelas": [
            {"vencimento": "2026-09-15", "valor": 12500.00},
            {"vencimento": "2026-10-15", "valor": 12500.00},
        ],
        "carteira": "CARTEIRA-01",
        "tipoAvaliacao": "avaliacao_contrato_basica_ap",
        "garantias": [
            {
                "referenciaExterna": "CTR-2026-000001-G1",
                "domicilioPagamento": {
                    "numeroDocumentoTitular": "12345678000199",
                    "tipoConta": "CC",
                    "compe": "341",
                    "ispb": "60701190",
                    "agencia": "0001",
                    "numeroConta": "464561-6",
                },
                "definicaoUnidadeRecebivel": {
                    "listaCnpjCredenciadora": ["99T"],
                    "listaCodigoArranjoPagamento": ["VCC", "MCC"],
                    "documentoUsuarioFinalRecebedor": "22751826000125",
                    "documentoTitular": "22751826000125",
                    "dataInicio": "2026-08-26",
                    "dataFim": "2027-08-15",
                },
                "regrasDivisao": "1",
                "valorAOnerar": 180000.00,
                "tipoDistribuicao": "padrao_pro_rata_ap",
            },
        ],
    }
    base.update(overrides)
    return base


def test_validar_criacao_contrato_payload_valido_converte_monetarios_para_decimal():
    resultado = validar_criacao_contrato(_payload_valido(), hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)

    assert resultado["saldoDevedor"] == Decimal("150000.00")
    assert isinstance(resultado["saldoDevedor"], Decimal)
    assert resultado["limiteOperacaoGarantida"] == Decimal("200000.00")
    assert resultado["valorMantido"] == Decimal("180000.00")
    assert resultado["parcelas"][0]["valor"] == Decimal("12500.00")
    assert resultado["garantias"][0]["valorAOnerar"] == Decimal("180000.00")
    assert resultado["dataAssinatura"] == date(2026, 8, 15)
    assert resultado["dataVencimento"] == date(2027, 8, 15)
    assert resultado["garantias"][0]["definicaoUnidadeRecebivel"]["dataInicio"] == date(2026, 8, 26)


def test_validar_criacao_contrato_nao_muta_payload_original():
    payload = _payload_valido()
    validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert isinstance(payload["saldoDevedor"], float)  # original intocado


def test_validar_criacao_contrato_c01_documento_contratante_invalido():
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(_payload_valido(documentoContratante="11111111111111"), hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C01"


def test_validar_criacao_contrato_c02_repactuacao_sem_contratos_anteriores():
    payload = _payload_valido(repactuacao="1", identificacaoContratosAnteriores=[], garantias=[])
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C02"


def test_validar_criacao_contrato_c03_repactuacao_com_garantias():
    payload = _payload_valido(repactuacao="1", identificacaoContratosAnteriores=["OP-ANTERIOR-1"])
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C03"


def test_validar_criacao_contrato_c04_saldo_devedor_abaixo_do_minimo():
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(_payload_valido(saldoDevedor=0.00), hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C04"


def test_validar_criacao_contrato_c05_parcelado_sem_parcelas():
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(_payload_valido(parcelas=[]), hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C05"


def test_validar_criacao_contrato_c06_tipo_distribuicao_com_gestao_diferente_de_1():
    payload = _payload_valido(identificacaoGestaoEntidadeRegistradora="2")
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C06"


def test_validar_criacao_contrato_c07_percentual_acima_de_100():
    payload = _payload_valido()
    payload["garantias"][0]["regrasDivisao"] = "2"
    payload["garantias"][0]["valorAOnerar"] = 120.00
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C07"


def test_validar_criacao_contrato_c08_data_inicio_no_passado():
    payload = _payload_valido()
    payload["garantias"][0]["definicaoUnidadeRecebivel"]["dataInicio"] = "2020-01-01"
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C08"


def test_validar_criacao_contrato_c09_data_fim_antes_da_data_inicio():
    payload = _payload_valido()
    payload["garantias"][0]["definicaoUnidadeRecebivel"]["dataFim"] = "2026-08-01"
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C09"


def test_validar_criacao_contrato_c10_raiz_titular_diferente_do_ufr():
    payload = _payload_valido()
    payload["garantias"][0]["definicaoUnidadeRecebivel"]["documentoUsuarioFinalRecebedor"] = "22751826"
    payload["garantias"][0]["definicaoUnidadeRecebivel"]["documentoTitular"] = "99999999"
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C10"


def test_validar_criacao_contrato_c12_referencia_garantia_duplicada():
    payload = _payload_valido()
    payload["garantias"].append(dict(payload["garantias"][0]))
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C12"


def test_validar_criacao_contrato_c13_sobreposicao_de_garantias():
    payload = _payload_valido()
    segunda = dict(payload["garantias"][0])
    segunda["referenciaExterna"] = "CTR-2026-000001-G2"
    payload["garantias"].append(segunda)
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C13"


def test_validar_criacao_contrato_c15_numero_conta_cc_sem_hifen():
    payload = _payload_valido()
    payload["garantias"][0]["domicilioPagamento"]["numeroConta"] = "4645616"
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C15"


def test_validar_criacao_contrato_c16_ispb_invalido():
    payload = _payload_valido()
    payload["garantias"][0]["domicilioPagamento"]["ispb"] = "123"
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C16"


def test_validar_criacao_contrato_c18_bloqueio_judicial_sem_identificador():
    payload = _payload_valido(tipoEfeito="4", identificadorContrato="")
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C18"


def test_validar_criacao_contrato_c19_arranjo_fora_do_dominio():
    payload = _payload_valido()
    payload["garantias"][0]["definicaoUnidadeRecebivel"]["listaCodigoArranjoPagamento"] = ["ARRANJO_INEXISTENTE"]
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "C19"


def test_validar_criacao_contrato_c19_aceita_99t_mesmo_fora_do_dominio_sincronizado():
    payload = _payload_valido()
    payload["garantias"][0]["definicaoUnidadeRecebivel"]["listaCodigoArranjoPagamento"] = ["99T"]
    validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=set())  # nenhum arranjo sincronizado ainda — "99T" sempre aceito
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_contrato_validation_orquestrador.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.contratos.contrato_validation_orquestrador'`

- [ ] **Step 3: Write `apps/contratos/contrato_validation_orquestrador.py`**

```python
"""Orquestra as validações C01-C20 (apps/contratos/validation.py, Plano 08)
contra o payload de CRIAÇÃO de contrato (SPEC-02 §4, tipoOperacao=C — sem
o campo tipoOperacao em si, quem chama decide isso). Fail-fast: levanta
ValidationError na primeira regra violada, mesmo padrão de cada
validar_cXX individual.

Fora do escopo (Plano 12, ver Global Constraints do plano):
- C14 (lista de participantes do SLC) — nenhuma fonte de dado existe
  neste código para essa lista; não inventada aqui.
- C17 (campos estáticos imutáveis) — só se aplica a tipoOperacao=A
  (atualização), fora do escopo deste plano (create-only).
- C20 (limite de 45 efeitos por UR) — para uma garantia NOVA (criação),
  qtd_efeitos_existente é sempre 0 (a garantia não existe até este
  contrato ser criado), então a regra é trivialmente satisfeita; não
  chamada aqui.

Converte todo valor monetário de int/float (o que json.loads produz)
para decimal.Decimal — nunca grava float numa coluna NUMERIC (design
§13.3) — e toda data em string AAAA-MM-DD para datetime.date. Retorna
um NOVO dict; o payload de entrada nunca é mutado.
"""

from datetime import date
from decimal import Decimal

from apps.contratos.validation import (
    ValidationError,
    tipo_documento,
    validar_c01_documento,
    validar_c02_repactuacao,
    validar_c03_repactuacao_sem_garantias,
    validar_c04_valor_monetario,
    validar_c05_modalidade_parcelado,
    validar_c06_tipo_distribuicao,
    validar_c07_regra_divisao_percentual,
    validar_c08_data_inicio_futura,
    validar_c09_ordem_datas,
    validar_c10_raiz_titular_igual_ufr,
    validar_c11_raiz_documento_unico,
    validar_c12_referencia_garantia_unica,
    validar_c13_sem_sobreposicao_garantias,
    validar_c15_numero_conta,
    validar_c16_domicilio_formatos,
    validar_c18_bloqueio_judicial,
    validar_c19_arranjos_no_dominio,
)


def _dec(valor) -> Decimal:
    return Decimal(str(valor))


def _eh_raiz(documento) -> bool:
    return bool(documento) and tipo_documento(documento) == "CNPJ_RAIZ"


def validar_criacao_contrato(payload: dict, *, hoje: date, ativos_arranjos: set) -> dict:
    validar_c01_documento(payload["documentoContratante"])
    validar_c02_repactuacao(payload.get("repactuacao"), payload.get("identificacaoContratosAnteriores"))
    validar_c03_repactuacao_sem_garantias(payload.get("repactuacao"), payload.get("garantias"))
    validar_c04_valor_monetario(_dec(payload["saldoDevedor"]), "saldoDevedor")
    validar_c04_valor_monetario(_dec(payload["limiteOperacaoGarantida"]), "limiteOperacaoGarantida")
    validar_c04_valor_monetario(_dec(payload["valorMantido"]), "valorMantido")
    validar_c05_modalidade_parcelado(payload.get("modalidadeOperacao"), payload.get("parcelas"))
    validar_c18_bloqueio_judicial(payload.get("tipoEfeito"), payload.get("identificadorContrato"))

    for parcela in payload.get("parcelas", []):
        validar_c04_valor_monetario(_dec(parcela["valor"]), "parcelas[].valor")

    garantias = payload.get("garantias", [])
    validar_c12_referencia_garantia_unica(garantias)
    validar_c13_sem_sobreposicao_garantias([
        {
            "credenciadoras": set(g["definicaoUnidadeRecebivel"]["listaCnpjCredenciadora"]),
            "arranjos": set(g["definicaoUnidadeRecebivel"]["listaCodigoArranjoPagamento"]),
            "ufr_titular": (
                g["definicaoUnidadeRecebivel"].get("documentoUsuarioFinalRecebedor"),
                g["definicaoUnidadeRecebivel"].get("documentoTitular"),
            ),
            "data_inicio": date.fromisoformat(g["definicaoUnidadeRecebivel"]["dataInicio"]),
            "data_fim": date.fromisoformat(g["definicaoUnidadeRecebivel"]["dataFim"]),
        }
        for g in garantias
    ])

    for g in garantias:
        definicao = g["definicaoUnidadeRecebivel"]
        domicilio = g["domicilioPagamento"]
        documento_ufr = definicao.get("documentoUsuarioFinalRecebedor")
        documento_titular = definicao.get("documentoTitular")

        validar_c06_tipo_distribuicao(g.get("tipoDistribuicao"), payload["identificacaoGestaoEntidadeRegistradora"])
        validar_c07_regra_divisao_percentual(g["regrasDivisao"], _dec(g["valorAOnerar"]))
        validar_c04_valor_monetario(_dec(g["valorAOnerar"]), "garantias[].valorAOnerar")
        validar_c08_data_inicio_futura(date.fromisoformat(definicao["dataInicio"]), hoje)
        validar_c09_ordem_datas(date.fromisoformat(definicao["dataInicio"]), date.fromisoformat(definicao["dataFim"]))

        eh_raiz = _eh_raiz(documento_ufr)
        validar_c10_raiz_titular_igual_ufr(documento_titular, documento_ufr, eh_raiz)
        validar_c11_raiz_documento_unico([d for d in (documento_ufr, documento_titular) if d], eh_raiz)

        validar_c15_numero_conta(domicilio["tipoConta"], domicilio["numeroConta"])
        validar_c16_domicilio_formatos(domicilio["ispb"], domicilio.get("compe"), domicilio["agencia"])
        validar_c19_arranjos_no_dominio(definicao["listaCodigoArranjoPagamento"], ativos_arranjos)

    return _converter_para_persistencia(payload)


def _converter_para_persistencia(payload: dict) -> dict:
    convertido = dict(payload)
    convertido["saldoDevedor"] = _dec(payload["saldoDevedor"])
    convertido["limiteOperacaoGarantida"] = _dec(payload["limiteOperacaoGarantida"])
    convertido["valorMantido"] = _dec(payload["valorMantido"])
    convertido["dataAssinatura"] = date.fromisoformat(payload["dataAssinatura"])
    convertido["dataVencimento"] = date.fromisoformat(payload["dataVencimento"])
    if payload.get("taxaJuros") is not None:
        convertido["taxaJuros"] = _dec(payload["taxaJuros"])

    convertido["parcelas"] = [
        {**p, "valor": _dec(p["valor"]), "vencimento": date.fromisoformat(p["vencimento"])}
        for p in payload.get("parcelas", [])
    ]

    convertido["garantias"] = []
    for g in payload.get("garantias", []):
        g_convertida = dict(g)
        g_convertida["valorAOnerar"] = _dec(g["valorAOnerar"])
        definicao = dict(g["definicaoUnidadeRecebivel"])
        definicao["dataInicio"] = date.fromisoformat(definicao["dataInicio"])
        definicao["dataFim"] = date.fromisoformat(definicao["dataFim"])
        g_convertida["definicaoUnidadeRecebivel"] = definicao
        convertido["garantias"].append(g_convertida)

    return convertido
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apps/contratos/tests/test_contrato_validation_orquestrador.py -v`
Expected: PASS (19 tests)

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/contratos/contrato_validation_orquestrador.py apps/contratos/tests/test_contrato_validation_orquestrador.py
git commit -m "feat: orchestrate C01-C20 local validation for contract creation (SPEC-02 §9)"
```

---

### Task 2: `apps/contratos/contrato_repository.py` — DB writes for contract creation

**Files:**
- Create: `contratos/apps/contratos/contrato_repository.py`
- Test: `contratos/apps/contratos/tests/test_contrato_repository.py`

**Interfaces:**
- Consumes: `shared.cloudsql_client.get_db`.
- Produces: `buscar_contrato_por_referencia(financiador_id: str, referencia_externa: str) -> dict | None` (idempotency lookup); `inserir_contrato_criado(financiador_id: str, payload_validado: dict, status: str, protocolo: str | None, id_contrato_cerc: str | None) -> dict` (writes `contrato` + `contrato_contrato_anterior` + `contrato_parcela` + `contrato_domicilio` + `garantia` rows in that order, respecting FK dependencies; returns the inserted `contrato` row as a dict). `payload_validado` is exactly what Task 1's `validar_criacao_contrato` returns (monetary fields already `Decimal`, dates already `date`). Task 3 imports both.

- [ ] **Step 1: Write the failing test**

```python
# contratos/apps/contratos/tests/test_contrato_repository.py
from datetime import date
from decimal import Decimal

import pytest

from apps.contratos.contrato_repository import buscar_contrato_por_referencia, inserir_contrato_criado
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"


def _payload_validado(referencia_externa="CTR-TESTE-REPO-1", com_domicilio=True, com_anteriores=False, com_parcelas=True):
    garantia = {
        "referenciaExterna": f"{referencia_externa}-G1",
        "regrasDivisao": "1",
        "valorAOnerar": Decimal("180000.00"),
        "tipoDistribuicao": "padrao_pro_rata_ap",
        "definicaoUnidadeRecebivel": {
            "listaCnpjCredenciadora": ["99T"],
            "listaCodigoArranjoPagamento": ["VCC", "MCC"],
            "documentoUsuarioFinalRecebedor": "22751826000125",
            "documentoTitular": "22751826000125",
            "dataInicio": date(2026, 8, 26),
            "dataFim": date(2027, 8, 15),
        },
    }
    if com_domicilio:
        garantia["domicilioPagamento"] = {
            "numeroDocumentoTitular": "12345678000199",
            "nomeTitular": "Titular Teste",
            "tipoConta": "CC",
            "compe": "341",
            "ispb": "60701190",
            "agencia": "0001",
            "numeroConta": "464561-6",
        }
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-REPO",
        "documentoContratante": "22751826000125",
        "repactuacao": "0",
        "identificacaoContratosAnteriores": ["OP-ANTERIOR-1"] if com_anteriores else [],
        "cnpjDetentor": FINANCIADOR_TESTE,
        "tipoEfeito": "2",
        "saldoDevedor": Decimal("150000.00"),
        "limiteOperacaoGarantida": Decimal("200000.00"),
        "valorMantido": Decimal("180000.00"),
        "dataAssinatura": date(2026, 8, 15),
        "dataVencimento": date(2027, 8, 15),
        "identificacaoGestaoEntidadeRegistradora": "1",
        "modalidadeOperacao": "2",
        "parcelas": [{"vencimento": date(2026, 9, 15), "valor": Decimal("12500.00")}] if com_parcelas else [],
        "carteira": "CARTEIRA-01",
        "tipoAvaliacao": "avaliacao_contrato_basica_ap",
        "garantias": [garantia],
    }


def _limpar(referencia_externa):
    db = get_db(FINANCIADOR_TESTE)
    existente = db.table("contrato").select("id").eq("referencia_externa", referencia_externa).execute()
    for row in existente.data:
        contrato_id = row["id"]
        garantias = db.table("garantia").select("id").eq("contrato_id", contrato_id).execute()
        for g in garantias.data:
            db.table("garantia_ur").delete().eq("garantia_id", g["id"]).execute()
        db.table("garantia").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_domicilio").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_parcela").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_contrato_anterior").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato").delete().eq("id", contrato_id).execute()


def test_buscar_contrato_por_referencia_inexistente_retorna_none():
    assert buscar_contrato_por_referencia(FINANCIADOR_TESTE, "CTR-TESTE-REPO-INEXISTENTE") is None


def test_inserir_contrato_criado_grava_todas_as_subtabelas():
    referencia_externa = "CTR-TESTE-REPO-COMPLETO"
    _limpar(referencia_externa)
    try:
        payload = _payload_validado(referencia_externa, com_anteriores=True, com_parcelas=True)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="AGUARDANDO_WEBHOOK", protocolo="proto-1", id_contrato_cerc="cerc-1",
        )

        assert contrato["referencia_externa"] == referencia_externa
        assert contrato["status"] == "AGUARDANDO_WEBHOOK"
        assert contrato["protocolo_cerc"] == "proto-1"
        assert contrato["cnpj_participante"] == FINANCIADOR_TESTE
        assert contrato["saldo_devedor"] == Decimal("150000.00")

        db = get_db(FINANCIADOR_TESTE)
        anteriores = db.table("contrato_contrato_anterior").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(anteriores.data) == 1
        assert anteriores.data[0]["identificador_anterior"] == "OP-ANTERIOR-1"

        parcelas = db.table("contrato_parcela").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(parcelas.data) == 1
        assert parcelas.data[0]["valor"] == Decimal("12500.00")

        domicilio = db.table("contrato_domicilio").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(domicilio.data) == 1
        assert domicilio.data[0]["ispb"] == "60701190"

        garantias = db.table("garantia").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(garantias.data) == 1
        assert garantias.data[0]["referencia_externa"] == f"{referencia_externa}-G1"
        assert garantias.data[0]["def_lista_arranjos"] == ["VCC", "MCC"]

        encontrado = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert encontrado["id"] == contrato["id"]
    finally:
        _limpar(referencia_externa)


def test_inserir_contrato_criado_sem_anteriores_nem_parcelas_nao_gera_linhas():
    referencia_externa = "CTR-TESTE-REPO-MINIMO"
    _limpar(referencia_externa)
    try:
        payload = _payload_validado(referencia_externa, com_anteriores=False, com_parcelas=False)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="AGUARDANDO_WEBHOOK", protocolo="proto-2", id_contrato_cerc=None,
        )

        db = get_db(FINANCIADOR_TESTE)
        anteriores = db.table("contrato_contrato_anterior").select("*").eq("contrato_id", contrato["id"]).execute()
        assert anteriores.data == []
        parcelas = db.table("contrato_parcela").select("*").eq("contrato_id", contrato["id"]).execute()
        assert parcelas.data == []
    finally:
        _limpar(referencia_externa)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_contrato_repository.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'apps.contratos.contrato_repository'`

- [ ] **Step 3: Write `apps/contratos/contrato_repository.py`**

```python
"""Escritas em `contrato`/`contrato_contrato_anterior`/`contrato_parcela`/
`contrato_domicilio`/`garantia` para o fluxo de criação (Plano 12).
`payload_validado` é exatamente o que
apps.contratos.contrato_validation_orquestrador.validar_criacao_contrato
retorna — valores monetários já Decimal, datas já date.

domicilio: SPEC-02 §4.2 carrega domicilioPagamento POR GARANTIA, mas o
schema (§11, sql/schema/01-contratos-schema.sql) tem contrato_domicilio
como UMA linha por CONTRATO — mismatch herdado da própria spec, não
inventado aqui. Grava o domicílio da PRIMEIRA garantia; ver Global
Constraints do Plano 12.
"""

import uuid

from shared.cloudsql_client import get_db


def buscar_contrato_por_referencia(financiador_id: str, referencia_externa: str) -> dict | None:
    resultado = get_db(financiador_id).table("contrato").select("*").eq("referencia_externa", referencia_externa).execute()
    return resultado.data[0] if resultado.data else None


def inserir_contrato_criado(
    financiador_id: str, payload_validado: dict, status: str, protocolo, id_contrato_cerc,
) -> dict:
    db = get_db(financiador_id)
    contrato_id = str(uuid.uuid4())

    inserido = db.table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": payload_validado["referenciaExterna"],
        "identificador_contrato": payload_validado["identificadorContrato"],
        "protocolo_cerc": protocolo,
        "id_contrato_cerc": id_contrato_cerc,
        "status": status,
        "cnpj_participante": financiador_id,
        "documento_contratante": payload_validado["documentoContratante"],
        "cnpj_detentor": payload_validado["cnpjDetentor"],
        "tipo_efeito": payload_validado["tipoEfeito"],
        "modalidade_operacao": payload_validado["modalidadeOperacao"],
        "gestao_entidade_registradora": payload_validado["identificacaoGestaoEntidadeRegistradora"],
        "saldo_devedor": payload_validado["saldoDevedor"],
        "limite_operacao_garantida": payload_validado["limiteOperacaoGarantida"],
        "valor_mantido": payload_validado["valorMantido"],
        "data_assinatura": payload_validado["dataAssinatura"],
        "data_vencimento": payload_validado["dataVencimento"],
        "repactuacao": payload_validado.get("repactuacao") == "1",
        "carteira": payload_validado.get("carteira"),
        "tipo_avaliacao": payload_validado.get("tipoAvaliacao"),
        "taxa_juros": payload_validado.get("taxaJuros"),
        "indexador": payload_validado.get("indexador"),
    }).execute()
    contrato = inserido.data[0]

    for identificador_anterior in payload_validado.get("identificacaoContratosAnteriores", []) or []:
        db.table("contrato_contrato_anterior").insert({
            "contrato_id": contrato_id, "identificador_anterior": identificador_anterior,
        }).execute()

    for parcela in payload_validado.get("parcelas", []) or []:
        db.table("contrato_parcela").insert({
            "contrato_id": contrato_id, "vencimento": parcela["vencimento"], "valor": parcela["valor"],
        }).execute()

    garantias = payload_validado.get("garantias", []) or []
    if garantias and garantias[0].get("domicilioPagamento"):
        domicilio = garantias[0]["domicilioPagamento"]
        db.table("contrato_domicilio").insert({
            "contrato_id": contrato_id,
            "numero_documento_titular": domicilio["numeroDocumentoTitular"],
            "nome_titular": domicilio.get("nomeTitular"),
            "tipo_conta": domicilio["tipoConta"],
            "compe": domicilio.get("compe"),
            "ispb": domicilio["ispb"],
            "agencia": domicilio.get("agencia"),
            "numero_conta": domicilio["numeroConta"],
        }).execute()

    for g in garantias:
        definicao = g["definicaoUnidadeRecebivel"]
        db.table("garantia").insert({
            "id": str(uuid.uuid4()),
            "contrato_id": contrato_id,
            "referencia_externa": g["referenciaExterna"],
            "regras_divisao": g["regrasDivisao"],
            "valor_a_onerar": g["valorAOnerar"],
            "tipo_distribuicao": g.get("tipoDistribuicao"),
            "def_lista_credenciadoras": definicao["listaCnpjCredenciadora"],
            "def_lista_arranjos": definicao["listaCodigoArranjoPagamento"],
            "def_documento_ufr": definicao.get("documentoUsuarioFinalRecebedor"),
            "def_documento_titular": definicao.get("documentoTitular"),
            "def_data_inicio": definicao["dataInicio"],
            "def_data_fim": definicao["dataFim"],
        }).execute()

    return contrato
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apps/contratos/tests/test_contrato_repository.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/contratos/contrato_repository.py apps/contratos/tests/test_contrato_repository.py
git commit -m "feat: persist a created contract's full sub-table graph (SPEC-02 §11)"
```

---

### Task 3: `POST /api/v1/contratos/<financiador_id>` — the create-contract view

**Files:**
- Modify: `contratos/apps/contratos/views.py`
- Modify: `contratos/apps/contratos/urls.py`
- Test: `contratos/apps/contratos/tests/test_views_criar_contrato.py`

**Interfaces:**
- Consumes: `apps.contratos.contrato_validation_orquestrador.validar_criacao_contrato` (Task 1), `apps.contratos.contrato_repository.buscar_contrato_por_referencia`/`.inserir_contrato_criado` (Task 2), `apps.contratos.validation.ValidationError`, `apps.contratos.state_machine.estado_apos_207`, `services.cerc.client.criar_contrato`/`CercApiError`, `shared.cloudsql_client.get_db` (for reading `dominio_arranjo`).
- Produces: view function `criar_contrato(request, financiador_id: str)`, routed at `contratos/<str:financiador_id>` (constrained the same way Plan 10's final review constrained the webhook receiver route — `financiador_id` is always a 14-digit CNPJ).

- [ ] **Step 1: Write the failing tests**

```python
# contratos/apps/contratos/tests/test_views_criar_contrato.py
import json

import httpx
import pytest
import respx
from django.test import Client

from apps.contratos.contrato_repository import buscar_contrato_por_referencia
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = f"/api/v1/contratos/{FINANCIADOR_TESTE}"


def _payload(referencia_externa="CTR-TESTE-VIEW-1"):
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-VIEW",
        "documentoContratante": "22751826000125",
        "repactuacao": "0",
        "cnpjDetentor": FINANCIADOR_TESTE,
        "tipoEfeito": "2",
        "saldoDevedor": 150000.00,
        "limiteOperacaoGarantida": 200000.00,
        "valorMantido": 180000.00,
        "dataAssinatura": "2026-08-15",
        "dataVencimento": "2027-08-15",
        "identificacaoGestaoEntidadeRegistradora": "1",
        "modalidadeOperacao": "2",
        "parcelas": [{"vencimento": "2026-09-15", "valor": 12500.00}],
        "garantias": [
            {
                "referenciaExterna": f"{referencia_externa}-G1",
                "domicilioPagamento": {
                    "numeroDocumentoTitular": FINANCIADOR_TESTE, "tipoConta": "CC", "compe": "341",
                    "ispb": "60701190", "agencia": "0001", "numeroConta": "464561-6",
                },
                "definicaoUnidadeRecebivel": {
                    "listaCnpjCredenciadora": ["99T"], "listaCodigoArranjoPagamento": ["99T"],
                    "documentoUsuarioFinalRecebedor": "22751826000125", "documentoTitular": "22751826000125",
                    "dataInicio": "2026-08-26", "dataFim": "2027-08-15",
                },
                "regrasDivisao": "1", "valorAOnerar": 180000.00, "tipoDistribuicao": "padrao_pro_rata_ap",
            },
        ],
    }


def _limpar(referencia_externa):
    db = get_db(FINANCIADOR_TESTE)
    existente = db.table("contrato").select("id").eq("referencia_externa", referencia_externa).execute()
    for row in existente.data:
        contrato_id = row["id"]
        for g in db.table("garantia").select("id").eq("contrato_id", contrato_id).execute().data:
            db.table("garantia_ur").delete().eq("garantia_id", g["id"]).execute()
        db.table("garantia").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_domicilio").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_parcela").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_contrato_anterior").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato").delete().eq("id", contrato_id).execute()


def _mock_token():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )


def test_criar_contrato_get_retorna_405():
    response = Client().get(URL)
    assert response.status_code == 405


def test_criar_contrato_corpo_nao_json_retorna_400():
    response = Client().post(URL, data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


def test_criar_contrato_validacao_local_falha_retorna_422():
    payload = _payload("CTR-TESTE-VIEW-INVALIDO")
    payload["saldoDevedor"] = 0.00
    response = Client().post(URL, data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 422
    corpo = response.json()
    assert corpo["codigo"] == "C04"


@respx.mock
def test_criar_contrato_sucesso_207_status_0_persiste_aguardando_webhook():
    referencia_externa = "CTR-TESTE-VIEW-OK"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-1",
                "idDoContrato": "cerc-view-1", "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
                "status": "0", "erros": [],
            }])
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 202
        corpo = response.json()
        assert corpo["status"] == "AGUARDANDO_WEBHOOK"
        assert corpo["referenciaExterna"] == referencia_externa

        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert contrato["status"] == "AGUARDANDO_WEBHOOK"
        assert contrato["protocolo_cerc"] == "proto-view-1"
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_207_status_1_persiste_rejeitado_estrutural_e_retorna_422():
    referencia_externa = "CTR-TESTE-VIEW-REJEITADO"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-2",
                "dataHoraProcessamento": "2026-08-25T12:00:00.000Z", "status": "1",
                "erros": [{"codigo": "107501", "mensagem": "UFR sem vínculo"}],
            }])
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 422
        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert contrato["status"] == "REJEITADO_ESTRUTURAL"
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_referencia_externa_repetida_e_idempotente_nao_chama_cerc_de_novo():
    referencia_externa = "CTR-TESTE-VIEW-DUP"
    _limpar(referencia_externa)
    try:
        _mock_token()
        rota = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-3",
                "dataHoraProcessamento": "2026-08-25T12:00:00.000Z", "status": "0", "erros": [],
            }])
        )

        cliente = Client()
        r1 = cliente.post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")
        r2 = cliente.post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert r1.status_code == 202
        assert r2.status_code == 202
        assert rota.call_count == 1  # segunda chamada não bate na CERC de novo
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_erro_cerc_retorna_502():
    referencia_externa = "CTR-TESTE-VIEW-502"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(500, json={"erro": "indisponível"})
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 502
        assert buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa) is None
    finally:
        _limpar(referencia_externa)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_views_criar_contrato.py -v`
Expected: FAIL — route/view don't exist yet (404s / `AttributeError`).

- [ ] **Step 3: Add to `apps/contratos/views.py`**

The current top of `apps/contratos/views.py` (from Plans 10/11) already has `from datetime import datetime, timezone` and `from apps.contratos import state_machine` — do NOT add a second `state_machine` import. Change the existing `from datetime import datetime, timezone` line to also bring in `date`:

```python
from datetime import date, datetime, timezone
```

Then add these NEW imports alongside the existing ones (do not duplicate `state_machine`):

```python
from apps.contratos.contrato_repository import buscar_contrato_por_referencia, inserir_contrato_criado
from apps.contratos.contrato_validation_orquestrador import validar_criacao_contrato
from apps.contratos.validation import ValidationError
from services.cerc.client import CercApiError, criar_contrato as cerc_criar_contrato
```

In the view body, use `date.today()` directly (no alias needed — `date` doesn't collide with anything else already imported in this file).

Add this view function:

```python
@require_POST
def criar_contrato(request, financiador_id: str):
    """POST /api/v1/contratos/<financiador_id> — cria um contrato
    (tipoOperacao=C). Valida localmente (C01-C20 aplicáveis a criação),
    submete à CERC, interpreta o 207 e persiste o estado inicial. O
    resultado REAL do registro chega depois pelo webhook (Planos 10/11)
    — este endpoint responde 202 (aceito, processamento assíncrono), não
    201/200 (SPEC-02 §0)."""
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)

    referencia_externa = payload.get("referenciaExterna")
    if referencia_externa:
        existente = buscar_contrato_por_referencia(financiador_id, referencia_externa)
        if existente:
            return JsonResponse({
                "id": existente["id"], "status": existente["status"], "referenciaExterna": referencia_externa,
            }, status=202)

    db = get_db(financiador_id)
    ativos = db.table("dominio_arranjo").select("codigo").eq("ativo", True).execute()
    ativos_arranjos = {row["codigo"] for row in ativos.data}

    try:
        payload_validado = validar_criacao_contrato(payload, hoje=date.today(), ativos_arranjos=ativos_arranjos)
    except ValidationError as erro:
        return JsonResponse({"codigo": erro.codigo, "erro": erro.mensagem}, status=422)
    except (KeyError, TypeError) as erro:
        return JsonResponse({"erro": f"campo obrigatório ausente ou mal formado: {erro}"}, status=400)

    payload_cerc = {**payload, "cnpjParticipante": financiador_id}
    try:
        resultado = cerc_criar_contrato(financiador_id, payload_cerc, correlacao_id=referencia_externa)
    except CercApiError:
        logger.exception("[CriarContrato] CERC respondeu erro (financiador=%s, referencia=%s)", financiador_id, referencia_externa)
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)
    except Exception:
        logger.exception("[CriarContrato] falha inesperada ao chamar a CERC (financiador=%s, referencia=%s)", financiador_id, referencia_externa)
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)

    item = resultado[0]
    novo_status = state_machine.estado_apos_207(tipo_operacao="C", status_207=item["status"])

    contrato = inserir_contrato_criado(
        financiador_id, payload_validado, status=novo_status,
        protocolo=item.get("protocolo"), id_contrato_cerc=item.get("idDoContrato"),
    )

    status_http = 202 if novo_status != state_machine.REJEITADO_ESTRUTURAL else 422
    return JsonResponse({
        "id": contrato["id"], "status": novo_status, "referenciaExterna": referencia_externa,
        "protocolo": item.get("protocolo"),
    }, status=status_http)
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
    re_path(r"^contratos/(?P<financiador_id>\d{14})$", views.criar_contrato),
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest apps/contratos/tests/test_views_criar_contrato.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass (existing suites untouched).

- [ ] **Step 7: Commit**

```bash
git add apps/contratos/views.py apps/contratos/urls.py apps/contratos/tests/test_views_criar_contrato.py
git commit -m "feat: POST /api/v1/contratos/<financiador_id> — create contract (SPEC-02 §4, tipoOperacao=C)"
```

---

## Self-Review Notes

- **Spec coverage:** 17 applicable-to-creation validators (C01, C02, C03, C04, C05, C06, C07, C08, C09, C10, C11, C12, C13, C15, C16, C18, C19) are all orchestrated. 16 of them have a dedicated negative test in Task 1's suite. **C11 does not** — with the orchestrator calling `validar_c11_raiz_documento_unico` with exactly the two documents `(documento_ufr, documento_titular)`, and C10 already requiring those two to be equal whenever `eh_raiz` is true, C11's own failure condition (`len(set(documentos)) > 1` while `eh_raiz`) can only ever be reached by *also* failing C10 first — making it effectively unreachable given this specific 2-argument call shape. This is a known, minor integration gap (not a bug — the rule is still correctly wired, it just has no independent failure mode with only two documents to check), left as-is rather than forcing an artificial test that can't fail for a real reason; worth revisiting if a future spec clarification shows C11 should receive a broader document set. C14/C17/C20 are explicitly out of scope with documented reasons (no data source; update-only; trivially-zero-for-new-garantia respectively). The `207`→state transition (`ENVIANDO`→`AGUARDANDO_WEBHOOK`/`REJEITADO_ESTRUTURAL`) is exercised for both `status="0"` and `status="1"`. Idempotency via `referencia_externa` is tested (second POST doesn't re-hit the CERC). `cnpjParticipante` is always taken from the URL, never trusted from the body (Global Constraints).
- **Placeholder scan:** none — every branch (400 bad JSON, 422 local validation, 422 structural rejection from CERC, 502 CERC failure, 202 success, 202 idempotent replay) has a real test with real assertions on persisted database state, not just the HTTP status.
- **Type consistency:** `validar_criacao_contrato(payload, *, hoje, ativos_arranjos) -> dict` (Task 1) is called identically in Task 3's view and in Task 1's own tests. `inserir_contrato_criado(financiador_id, payload_validado, status, protocolo, id_contrato_cerc) -> dict` (Task 2) is called with the same argument order/names in Task 3's view and Task 2's own tests. `buscar_contrato_por_referencia(financiador_id, referencia_externa) -> dict | None` likewise.

**Next:** the state-machine question flagged in Global Constraints (`estado_apos_operacao_pos_registro`'s no-intermediate-waiting-state design for `I`/`B`/`P`/`R`) needs to be resolved — either by confirming that design is intentional (SPEC-02 §0's "everything is async" applies only to `C`/`A`, not to terminal operations) or by extending `state_machine.py` with a waiting state for those too — before a follow-up plan builds `atualizar`/`inativar`/`baixar`/`resilir` on top of it. `2026-08-25-contratos-plan-13-*.md` (not yet named) should start with that question, not with code.
