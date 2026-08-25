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
