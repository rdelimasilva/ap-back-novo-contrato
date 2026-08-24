# contratos-service — Plan 08: Local Validation Rules (C01-C20) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pure-function local validation matching SPEC-02's `C01`-`C20` catalog (§9) — every request rejected here never reaches the CERC, avoiding a specific, named CERC error code (`107xxx`) per rule.

**Architecture:** A single module, `apps/contratos/validation.py`, raising a typed `ValidationError(codigo, mensagem)`. **No I/O** — where a rule needs external state (the arranjo domain table, the SLC participant list, a contract already persisted locally, an existing effect count on a UR), that state is an explicit function parameter, never fetched by this module itself. The caller (a later plan's view/handler, which already has database access) fetches the data and passes it in — same pattern `ap-back-optin`'s `validar_arranjos`/`VAL005` already established for its one DB-shaped rule. This keeps every one of the 20 rules trivially unit-testable with zero mocking.

**Tech Stack:** Python 3.12 stdlib only (`re`, `decimal`, `datetime`).

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§4). Normative source: `SPEC-02-criacao-de-contratos-ap007.md` §2.1 (classificação de campos), §9 (C01-C20), §13.1 (critério de teste: um caso positivo e um negativo por regra). Series: plan 8 of ~11.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout — this module lives inside `apps/contratos/`, already created there).

## Global Constraints

- Documents are stored **without formatting**, zero-padded left: 14 digits (CNPJ), 11 digits (CPF), 8 digits (CNPJ raiz) — same convention as `ap-back-optin` (SPEC-02 doesn't redefine this; it's the shared document convention SPEC-02 §14 calls out as reused from SPEC-01).
- Money comparisons use `decimal.Decimal`, **never** `float` (design §3, SPEC-02 §13.3) — even in a validation threshold comparison, not just in stored values.
- Four rules are deliberately **not** self-contained DB lookups inside this module — `C14` (SLC participant list), `C17` (previously-stored contract), `C19` (arranjo domain), `C20` (existing effect count per UR) all take that external data as a parameter. This plan does not implement the *fetching* of that data (no `dominio_arranjo`/`garantia_ur` queries here) — that's the job of whatever view/handler plan calls these functions with real data from the database.
- `C18` has no corresponding CERC error code (SPEC-02 §9 lists `—` for it) — it's a local-only guideline, not a rule the CERC itself enforces, so keep its check minimal (presence, not a judicial-process-number format authority this codebase doesn't have).

---

### Task 1: `apps/contratos/validation.py`

**Files:**
- Create: `contratos/apps/contratos/validation.py`
- Test: `contratos/apps/contratos/tests/test_validation.py`

**Interfaces:**
- Produces: `ValidationError(codigo: str, mensagem: str)` and one function per rule, `validar_c01_documento` through `validar_c20_limite_efeitos_por_ur`, plus the shared helpers `normalizar_documento`, `tipo_documento`, `conjuntos_se_sobrepoem`, `vigencias_se_sobrepoem`. Later plans (state machine, request-handling views) import all of these.

- [ ] **Step 1: Write the failing test**

```python
# contratos/apps/contratos/tests/test_validation.py
import datetime
from decimal import Decimal

import pytest

from apps.contratos.validation import (
    ValidationError,
    normalizar_documento,
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
    validar_c14_ispb_no_slc,
    validar_c15_numero_conta,
    validar_c16_domicilio_formatos,
    validar_c17_campos_estaticos_imutaveis,
    validar_c18_bloqueio_judicial,
    validar_c19_arranjos_no_dominio,
    validar_c20_limite_efeitos_por_ur,
)


# C01 — documento (formato, DV, zero-pad)

def test_c01_cnpj_valido_normaliza_e_classifica():
    documento, tipo = validar_c01_documento("11.222.333/0001-81")
    assert documento == "11222333000181"
    assert tipo == "CNPJ"


def test_c01_cpf_dv_invalido_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c01_documento("111.111.111-11")
    assert exc.value.codigo == "C01"


# C02 — repactuacao=1 exige identificacaoContratosAnteriores

def test_c02_repactuacao_com_contratos_anteriores_ok():
    validar_c02_repactuacao("1", ["OP-0001"])  # não levanta


def test_c02_repactuacao_sem_contratos_anteriores_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c02_repactuacao("1", [])
    assert exc.value.codigo == "C02"


# C03 — repactuacao=1 exige garantias[] vazio

def test_c03_repactuacao_sem_garantias_ok():
    validar_c03_repactuacao_sem_garantias("1", [])


def test_c03_repactuacao_com_garantias_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c03_repactuacao_sem_garantias("1", [{"referenciaExterna": "G1"}])
    assert exc.value.codigo == "C03"


# C04 — valores monetários >= 0.01

def test_c04_valor_valido_ok():
    validar_c04_valor_monetario(Decimal("0.01"), "saldoDevedor")


def test_c04_valor_abaixo_do_minimo_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c04_valor_monetario(Decimal("0.00"), "saldoDevedor")
    assert exc.value.codigo == "C04"


# C05 — modalidadeOperacao=2 (parcelado) exige parcelas[]

def test_c05_parcelado_com_parcelas_ok():
    validar_c05_modalidade_parcelado("2", [{"vencimento": "2026-09-15", "valor": Decimal("100.00")}])


def test_c05_parcelado_sem_parcelas_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c05_modalidade_parcelado("2", [])
    assert exc.value.codigo == "C05"


# C06 — tipoDistribuicao sse gestão=1

def test_c06_tipo_distribuicao_com_gestao_1_ok():
    validar_c06_tipo_distribuicao("padrao_pro_rata_ap", "1")


def test_c06_tipo_distribuicao_com_gestao_2_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c06_tipo_distribuicao("padrao_pro_rata_ap", "2")
    assert exc.value.codigo == "C06"


def test_c06_gestao_1_sem_tipo_distribuicao_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c06_tipo_distribuicao(None, "1")
    assert exc.value.codigo == "C06"


# C07 — regrasDivisao=2 (percentual) <= 100

def test_c07_percentual_valido_ok():
    validar_c07_regra_divisao_percentual("2", Decimal("100"))


def test_c07_percentual_acima_de_100_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c07_regra_divisao_percentual("2", Decimal("120"))
    assert exc.value.codigo == "C07"


# C08 — dataInicio >= hoje

def test_c08_data_inicio_hoje_ok():
    hoje = datetime.date(2026, 8, 24)
    validar_c08_data_inicio_futura(hoje, hoje)


def test_c08_data_inicio_no_passado_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c08_data_inicio_futura(datetime.date(2026, 8, 23), datetime.date(2026, 8, 24))
    assert exc.value.codigo == "C08"


# C09 — dataFim >= dataInicio

def test_c09_datas_em_ordem_ok():
    validar_c09_ordem_datas(datetime.date(2026, 8, 24), datetime.date(2027, 8, 24))


def test_c09_data_fim_antes_do_inicio_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c09_ordem_datas(datetime.date(2027, 8, 24), datetime.date(2026, 8, 24))
    assert exc.value.codigo == "C09"


# C10 — CNPJ raiz exige documentoTitular == documentoUsuarioFinalRecebedor

def test_c10_raiz_com_titular_igual_ufr_ok():
    validar_c10_raiz_titular_igual_ufr("22751826000125", "22751826000125", eh_raiz=True)


def test_c10_raiz_com_titular_diferente_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c10_raiz_titular_igual_ufr("22751826000125", "99999999000191", eh_raiz=True)
    assert exc.value.codigo == "C10"


# C11 — CNPJ raiz exige documento único na definição

def test_c11_raiz_documento_unico_ok():
    validar_c11_raiz_documento_unico(["22751826"], eh_raiz=True)


def test_c11_raiz_mais_de_um_documento_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c11_raiz_documento_unico(["22751826", "99999999"], eh_raiz=True)
    assert exc.value.codigo == "C11"


# C12 — referenciaExterna de garantia única no contrato

def test_c12_referencias_unicas_ok():
    validar_c12_referencia_garantia_unica([{"referenciaExterna": "G1"}, {"referenciaExterna": "G2"}])


def test_c12_referencia_duplicada_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c12_referencia_garantia_unica([{"referenciaExterna": "G1"}, {"referenciaExterna": "G1"}])
    assert exc.value.codigo == "C12"


# C13 — sem sobreposição entre definições de garantia do mesmo contrato

def _garantia(credenciadoras, arranjos, ufr_titular, inicio, fim):
    return {
        "credenciadoras": set(credenciadoras),
        "arranjos": set(arranjos),
        "ufr_titular": ufr_titular,
        "data_inicio": inicio,
        "data_fim": fim,
    }


def test_c13_garantias_sem_sobreposicao_ok():
    garantias = [
        _garantia(["11111111000191"], ["VCC"], "UFR1", datetime.date(2026, 1, 1), datetime.date(2026, 6, 30)),
        _garantia(["22222222000191"], ["MCC"], "UFR1", datetime.date(2026, 1, 1), datetime.date(2026, 6, 30)),
    ]
    validar_c13_sem_sobreposicao_garantias(garantias)


def test_c13_garantias_com_sobreposicao_total_rejeita():
    garantias = [
        _garantia(["11111111000191"], ["VCC"], "UFR1", datetime.date(2026, 1, 1), datetime.date(2026, 6, 30)),
        _garantia(["11111111000191"], ["VCC"], "UFR1", datetime.date(2026, 3, 1), datetime.date(2026, 9, 30)),
    ]
    with pytest.raises(ValidationError) as exc:
        validar_c13_sem_sobreposicao_garantias(garantias)
    assert exc.value.codigo == "C13"


def test_c13_curinga_99t_sobrepoe_com_qualquer_credenciadora():
    garantias = [
        _garantia(["99T"], ["VCC"], "UFR1", datetime.date(2026, 1, 1), datetime.date(2026, 6, 30)),
        _garantia(["11111111000191"], ["VCC"], "UFR1", datetime.date(2026, 3, 1), datetime.date(2026, 9, 30)),
    ]
    with pytest.raises(ValidationError) as exc:
        validar_c13_sem_sobreposicao_garantias(garantias)
    assert exc.value.codigo == "C13"


# C14 — ISPB pertence à lista de participantes do SLC

def test_c14_ispb_no_slc_ok():
    validar_c14_ispb_no_slc("60701190", {"60701190", "00000000"})


def test_c14_ispb_fora_do_slc_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c14_ispb_no_slc("99999999", {"60701190"})
    assert exc.value.codigo == "C14"


# C15 — numeroConta com/sem hífen conforme tipoConta

def test_c15_conta_corrente_com_hifen_ok():
    validar_c15_numero_conta("CC", "464561-6")


def test_c15_conta_corrente_sem_hifen_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c15_numero_conta("CC", "4645616")
    assert exc.value.codigo == "C15"


def test_c15_conta_pagamento_sem_hifen_ok():
    validar_c15_numero_conta("PG", "4645616")


def test_c15_conta_pagamento_com_hifen_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c15_numero_conta("PG", "464561-6")
    assert exc.value.codigo == "C15"


# C16 — formatos de ispb/compe/agencia

def test_c16_formatos_validos_ok():
    validar_c16_domicilio_formatos("60701190", "341", "0001")


def test_c16_ispb_com_tamanho_errado_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c16_domicilio_formatos("607011900", "341", "0001")
    assert exc.value.codigo == "C16"


# C17 — campos estáticos imutáveis em tipoOperacao=A

def test_c17_atualizacao_sem_alterar_campos_estaticos_ok():
    contrato_atual = {"dataVencimento": "2027-08-15", "modalidadeOperacao": "2"}
    payload = {"dataVencimento": "2027-08-15", "cnpjDetentor": "99999999000191"}
    validar_c17_campos_estaticos_imutaveis(payload, contrato_atual)


def test_c17_atualizacao_alterando_campo_estatico_rejeita():
    contrato_atual = {"dataVencimento": "2027-08-15"}
    payload = {"dataVencimento": "2028-01-01"}
    with pytest.raises(ValidationError) as exc:
        validar_c17_campos_estaticos_imutaveis(payload, contrato_atual)
    assert exc.value.codigo == "C17"


# C18 — bloqueio judicial exige identificadorContrato

def test_c18_bloqueio_judicial_com_identificador_ok():
    validar_c18_bloqueio_judicial("4", "PROC-2026-0001")


def test_c18_bloqueio_judicial_sem_identificador_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c18_bloqueio_judicial("4", "")
    assert exc.value.codigo == "C18"


# C19 — arranjos no domínio vigente

def test_c19_arranjo_no_dominio_ok():
    validar_c19_arranjos_no_dominio(["VCC"], {"VCC", "MCC"})


def test_c19_arranjo_fora_do_dominio_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c19_arranjos_no_dominio(["XYZ"], {"VCC", "MCC"})
    assert exc.value.codigo == "C19"


def test_c19_curinga_99t_sempre_aceito():
    validar_c19_arranjos_no_dominio(["99T"], set())


# C20 — limite de 45 efeitos por UR

def test_c20_dentro_do_limite_ok():
    validar_c20_limite_efeitos_por_ur(qtd_efeitos_existente=44)


def test_c20_excede_o_limite_rejeita():
    with pytest.raises(ValidationError) as exc:
        validar_c20_limite_efeitos_por_ur(qtd_efeitos_existente=45)
    assert exc.value.codigo == "C20"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest apps/contratos/tests/test_validation.py -v`
Expected: FAIL with `ImportError`/`ModuleNotFoundError` (none of the `validar_c0*` names exist in `apps/contratos/validation.py` yet).

- [ ] **Step 3: Write `apps/contratos/validation.py`**

```python
"""Validações locais pré-CERC — SPEC-02 §9 (C01-C20).

Funções puras — nenhuma chamada a rede/CERC nem ao banco. Onde uma regra
depende de dado externo (domínio de arranjos sincronizado, contrato já
persistido, lista de participantes do SLC, contagem de efeitos já
aplicados numa UR), essa informação é parâmetro explícito da função — a
camada que já acessa o banco (uma view/handler de um plano futuro) busca
o dado e chama a função daqui, em vez desta função acessar o banco por
conta própria (mesmo padrão do ap-back-optin: validar_arranjos/VAL005).
"""

import re
from decimal import Decimal


class ValidationError(Exception):
    def __init__(self, codigo: str, mensagem: str):
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(f"{codigo}: {mensagem}")


# --- Documento (normalizador CPF/CNPJ/raiz — convenção compartilhada com ap-back-optin) ---

def normalizar_documento(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        raise ValidationError("C01", "documento vazio")
    if len(digits) <= 8:
        return digits.zfill(8)
    if len(digits) <= 11:
        return digits.zfill(11)
    return digits.zfill(14)


def tipo_documento(documento: str) -> str:
    tamanho = len(documento)
    if tamanho == 8:
        return "CNPJ_RAIZ"
    if tamanho == 11:
        return "CPF"
    if tamanho == 14:
        return "CNPJ"
    raise ValidationError("C01", f"documento com tamanho inválido: {tamanho}")


def _digito_verificador(base: str, pesos: list) -> str:
    soma = sum(int(d) * p for d, p in zip(base, pesos))
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def _validar_cpf(cpf: str) -> bool:
    if cpf == cpf[0] * 11:
        return False
    dv1 = _digito_verificador(cpf[:9], list(range(10, 1, -1)))
    dv2 = _digito_verificador(cpf[:9] + dv1, list(range(11, 1, -1)))
    return cpf[-2:] == dv1 + dv2


def _validar_cnpj(cnpj: str) -> bool:
    if cnpj == cnpj[0] * 14:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    dv1 = _digito_verificador(cnpj[:12], pesos1)
    dv2 = _digito_verificador(cnpj[:12] + dv1, pesos2)
    return cnpj[-2:] == dv1 + dv2


def validar_c01_documento(raw: str) -> tuple:
    """C01 — 11/14 dígitos (ou 8 pra raiz), DV válido, zero-pad. Evita 107008/107015/107017."""
    documento = normalizar_documento(raw)
    tipo = tipo_documento(documento)
    if tipo == "CPF" and not _validar_cpf(documento):
        raise ValidationError("C01", "dígito verificador de CPF inválido")
    if tipo == "CNPJ" and not _validar_cnpj(documento):
        raise ValidationError("C01", "dígito verificador de CNPJ inválido")
    return documento, tipo


def validar_c02_repactuacao(repactuacao: str, identificacao_contratos_anteriores) -> None:
    """C02 — repactuacao=1 exige identificacaoContratosAnteriores não vazio. Evita 107011."""
    if repactuacao == "1" and not identificacao_contratos_anteriores:
        raise ValidationError("C02", "repactuacao=1 exige identificacaoContratosAnteriores")


def validar_c03_repactuacao_sem_garantias(repactuacao: str, garantias: list) -> None:
    """C03 — repactuacao=1 exige garantias[] vazio. Evita 107816."""
    if repactuacao == "1" and garantias:
        raise ValidationError("C03", "repactuacao=1 não pode ter garantias[] especificadas")


def validar_c04_valor_monetario(valor: Decimal, campo: str) -> None:
    """C04 — valores monetários >= 0.01. Evita 107021/107023/107025."""
    if valor is None or valor < Decimal("0.01"):
        raise ValidationError("C04", f"{campo} deve ser >= 0.01")


def validar_c05_modalidade_parcelado(modalidade_operacao: str, parcelas: list) -> None:
    """C05 — modalidadeOperacao=2 (parcelado) exige parcelas[] não vazio. Evita 107034."""
    if modalidade_operacao == "2" and not parcelas:
        raise ValidationError("C05", "modalidadeOperacao=2 exige parcelas[] não vazio")


def validar_c06_tipo_distribuicao(tipo_distribuicao, identificacao_gestao_entidade_registradora: str) -> None:
    """C06 — tipoDistribuicao presente se e somente se gestão=1. Evita 107224/107503."""
    gestao_registradora = identificacao_gestao_entidade_registradora == "1"
    if tipo_distribuicao and not gestao_registradora:
        raise ValidationError("C06", "tipoDistribuicao só pode ser informado quando gestão=1")
    if gestao_registradora and not tipo_distribuicao:
        raise ValidationError("C06", "tipoDistribuicao obrigatório quando gestão=1")


def validar_c07_regra_divisao_percentual(regras_divisao: str, valor_a_onerar: Decimal) -> None:
    """C07 — regrasDivisao=2 (percentual) não pode exceder 100. Evita 107825."""
    if regras_divisao == "2" and valor_a_onerar > Decimal("100"):
        raise ValidationError("C07", "regrasDivisao=2 (percentual) não pode exceder 100")


def validar_c08_data_inicio_futura(data_inicio, hoje) -> None:
    """C08 — definicao.dataInicio >= hoje. Evita 107813."""
    if data_inicio < hoje:
        raise ValidationError("C08", "dataInicio não pode ser no passado")


def validar_c09_ordem_datas(data_inicio, data_fim) -> None:
    """C09 — definicao.dataFim >= definicao.dataInicio. Evita 107217."""
    if data_fim < data_inicio:
        raise ValidationError("C09", "dataFim menor que dataInicio")


def validar_c10_raiz_titular_igual_ufr(documento_titular, documento_usuario_final_recebedor, eh_raiz: bool) -> None:
    """C10 — CNPJ raiz exige documentoTitular == documentoUsuarioFinalRecebedor. Evita 107814."""
    if eh_raiz and documento_titular != documento_usuario_final_recebedor:
        raise ValidationError("C10", "CNPJ raiz exige documentoTitular == documentoUsuarioFinalRecebedor")


def validar_c11_raiz_documento_unico(documentos: list, eh_raiz: bool) -> None:
    """C11 — CNPJ raiz exige único documento na definição. Evita 107815."""
    if eh_raiz and len(set(documentos)) > 1:
        raise ValidationError("C11", "CNPJ raiz deve ser o único documento especificado na definição")


def validar_c12_referencia_garantia_unica(garantias: list) -> None:
    """C12 — referenciaExterna das garantias única dentro do contrato. Evita 107505."""
    referencias = [g["referenciaExterna"] for g in garantias]
    if len(referencias) != len(set(referencias)):
        raise ValidationError("C12", "referenciaExterna de garantia duplicada no mesmo contrato")


def conjuntos_se_sobrepoem(a: set, b: set) -> bool:
    """Compara duas listas (credenciadoras ou arranjos) tratando '99T' como curinga
    universal (SPEC-02 §4.4/§9 C13): se qualquer lado contiver '99T', há sobreposição total."""
    if "99T" in a or "99T" in b:
        return True
    return bool(a & b)


def vigencias_se_sobrepoem(inicio_a, fim_a, inicio_b, fim_b) -> bool:
    """Interseção de intervalos fechados [inicio, fim]."""
    return inicio_a <= fim_b and inicio_b <= fim_a


def validar_c13_sem_sobreposicao_garantias(garantias: list) -> None:
    """C13 — sem sobreposição entre definições de garantia do mesmo contrato (mesma
    credenciadora x arranjo x UFR/titular x interseção de datas, tratando 99T como
    universo). Evita 107823.

    `garantias` é uma lista de dicts com chaves: credenciadoras (set), arranjos
    (set), ufr_titular (valor hashable identificando o UFR/titular do filtro),
    data_inicio, data_fim (date).
    """
    for i, a in enumerate(garantias):
        for b in garantias[i + 1:]:
            if not conjuntos_se_sobrepoem(a["credenciadoras"], b["credenciadoras"]):
                continue
            if not conjuntos_se_sobrepoem(a["arranjos"], b["arranjos"]):
                continue
            if a["ufr_titular"] != b["ufr_titular"]:
                continue
            if not vigencias_se_sobrepoem(a["data_inicio"], a["data_fim"], b["data_inicio"], b["data_fim"]):
                continue
            raise ValidationError("C13", "sobreposição entre definições de garantia do mesmo contrato")


def validar_c14_ispb_no_slc(ispb: str, participantes_slc: set) -> None:
    """C14 — ISPB do domicílio pertence à lista de participantes do SLC. Recusa por
    regra do SLC (sem código 107xxx específico). `participantes_slc` vem de uma
    lista sincronizada — buscada pelo caller, não por este módulo."""
    if ispb not in participantes_slc:
        raise ValidationError("C14", f"ISPB {ispb} não pertence à lista de participantes do SLC")


def validar_c15_numero_conta(tipo_conta: str, numero_conta: str) -> None:
    """C15 — numeroConta com DV e hífen p/ CC/CD/PP; sem hífen p/ PG. Evita 107236."""
    tem_hifen = "-" in numero_conta
    if tipo_conta in ("CC", "CD", "PP") and not tem_hifen:
        raise ValidationError("C15", f"numeroConta de conta {tipo_conta} exige dígito verificador separado por hífen")
    if tipo_conta == "PG" and tem_hifen:
        raise ValidationError("C15", "numeroConta de conta PG não deve ter hífen")


def validar_c16_domicilio_formatos(ispb: str, compe, agencia: str) -> None:
    """C16 — ispb=8 dígitos; compe=3 dígitos; agencia <= 8 dígitos sem DV. Evita 107230-107234."""
    if not (ispb and len(ispb) == 8 and ispb.isdigit()):
        raise ValidationError("C16", "ispb deve ter exatamente 8 dígitos")
    if compe and not (len(compe) == 3 and compe.isdigit()):
        raise ValidationError("C16", "compe deve ter exatamente 3 dígitos")
    if not (agencia and agencia.isdigit() and len(agencia) <= 8):
        raise ValidationError("C16", "agencia deve ter até 8 dígitos, sem dígito verificador")


CAMPOS_ESTATICOS = {
    "repactuacao",
    "documentoContratante",
    "identificacaoContratosAnteriores",
    "dataAssinatura",
    "dataVencimento",
    "modalidadeOperacao",
    "parcelas",
}


def validar_c17_campos_estaticos_imutaveis(payload: dict, contrato_atual: dict) -> None:
    """C17 — em tipoOperacao=A, nenhum campo estático (SPEC-02 §2.1) pode ser
    alterado. Evita 107807. `contrato_atual` é o registro já persistido
    localmente — buscado pelo caller antes de montar a atualização, não por
    este módulo."""
    for campo in CAMPOS_ESTATICOS:
        if campo in payload and campo in contrato_atual and payload[campo] != contrato_atual[campo]:
            raise ValidationError("C17", f"campo estático '{campo}' não pode ser alterado")


def validar_c18_bloqueio_judicial(tipo_efeito: str, identificador_contrato: str) -> None:
    """C18 — tipoEfeito=4 (bloqueio judicial) exige identificadorContrato com o
    número do processo judicial. Sem código de erro CERC associado (SPEC-02 §9
    lista '—') — checagem apenas de presença, não de formato de processo."""
    if tipo_efeito == "4" and not identificador_contrato:
        raise ValidationError("C18", "bloqueio judicial exige identificadorContrato com o número do processo")


def validar_c19_arranjos_no_dominio(lista_arranjos: list, ativos: set) -> None:
    """C19 — arranjos pertencem ao domínio vigente sincronizado. Evita 107212. '99T'
    sempre aceito sem checar domínio. `ativos` vem de dominio_arranjo — buscado
    pelo caller, não por este módulo."""
    for codigo in lista_arranjos:
        if codigo != "99T" and codigo not in ativos:
            raise ValidationError("C19", f"arranjo fora do domínio vigente: {codigo}")


def validar_c20_limite_efeitos_por_ur(qtd_efeitos_existente: int, novos_efeitos: int = 1) -> None:
    """C20 — estimativa de efeitos por UR <= 45, quando a informação local existir.
    Evita 107842. `qtd_efeitos_existente` vem de garantia_ur — buscado pelo
    caller, não por este módulo."""
    if qtd_efeitos_existente + novos_efeitos > 45:
        raise ValidationError("C20", "limite de 45 efeitos por UR seria excedido")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest apps/contratos/tests/test_validation.py -v`
Expected: PASS (38 tests) — pure functions, no database/network, should run in well under a second.

- [ ] **Step 5: Run the full suite and Django check**

Run: `pytest -v` then `python manage.py check`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add apps/contratos/validation.py apps/contratos/tests/test_validation.py
git commit -m "feat: local validation rules C01-C20 (SPEC-02 §9)"
```

---

## Self-Review Notes

- **Spec coverage:** all 20 rules from SPEC-02 §9 (C01-C20) implemented, each with a positive and a negative test case per §13.1's explicit requirement. `C14`/`C17`/`C19`/`C20` take their external-data dependency as an explicit parameter rather than fetching it — deliberate, matches `ap-back-optin`'s established pattern for its one DB-shaped rule (`VAL005`/`validar_arranjos`), and keeps this entire plan I/O-free.
- **Placeholder scan:** none — every rule has a real implementation and a real test, no rule is stubbed out or deferred without a working (parameterized) implementation.
- **Type consistency:** `ValidationError(codigo, mensagem)` — every rule raises this with its own `C0X`/`C1X`/`C2X` code, matching the exact IDs SPEC-02 §9 uses, so a later plan's error-mapping/test-coverage check (design doc §13.3: "todos os códigos 107xxx... mapeados em enum, com teste de cobertura do catálogo") can trace each `C0X` straight back to the spec table. Money values are typed `Decimal` throughout — no function in this module accepts or returns `float`.

**Next:** `2026-08-24-contratos-plan-09-state-machine.md` (contract state machine — SPEC-02 §8).
