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
