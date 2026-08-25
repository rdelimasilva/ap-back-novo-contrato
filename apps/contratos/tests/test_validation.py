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
