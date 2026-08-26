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


@pytest.mark.parametrize("campo_ausente", [
    "referenciaExterna",
    "identificadorContrato",
    "cnpjDetentor",
    "tipoEfeito",
    "modalidadeOperacao",
    "identificacaoGestaoEntidadeRegistradora",
])
def test_validar_criacao_contrato_campo_obrigatorio_nivel_contrato_ausente(campo_ausente):
    payload = _payload_valido()
    del payload[campo_ausente]
    with pytest.raises(ValidationError) as exc:
        validar_criacao_contrato(payload, hoje=HOJE, ativos_arranjos=ATIVOS_ARRANJOS)
    assert exc.value.codigo == "CAMPO_OBRIGATORIO"
