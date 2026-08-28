from datetime import datetime, timezone

from django.test import Client

from apps.contratos import state_machine
from apps.contratos.contrato_repository import buscar_contrato_por_referencia, inserir_contrato_criado, remover_contrato_rejeitado
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"


def _payload_validado(referencia_externa):
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-DETALHE",
        "documentoContratante": "22751826000125",
        "cnpjDetentor": FINANCIADOR_TESTE,
        "tipoEfeito": "2",
        "saldoDevedor": 150000.00,
        "limiteOperacaoGarantida": 200000.00,
        "valorMantido": 180000.00,
        "dataAssinatura": "2026-08-15",
        "dataVencimento": "2027-08-15",
        "identificacaoGestaoEntidadeRegistradora": "2",
        "modalidadeOperacao": "1",
        "repactuacao": "0",
        "identificacaoContratosAnteriores": [],
        "parcelas": [],
        "garantias": [{
            "referenciaExterna": f"{referencia_externa}-G1",
            "regrasDivisao": "1",
            "valorAOnerar": 180000.00,
            "tipoDistribuicao": None,
            "definicaoUnidadeRecebivel": {
                "listaCnpjCredenciadora": ["99T"],
                "listaCodigoArranjoPagamento": ["99T"],
                "documentoUsuarioFinalRecebedor": "22751826000125",
                "documentoTitular": "22751826000125",
                "dataInicio": "2026-08-26",
                "dataFim": "2027-08-15",
            },
        }],
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


def test_detalhar_contrato_inexistente_retorna_404():
    response = Client().get(f"/api/v1/contratos/{FINANCIADOR_TESTE}/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_detalhar_contrato_financiador_desconhecido_retorna_404():
    response = Client().get("/api/v1/contratos/99999999000199/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_detalhar_contrato_traz_garantias_e_urs():
    referencia_externa = "CTR-TESTE-DETALHE-1"
    _limpar(referencia_externa)
    try:
        payload_validado = _payload_validado(referencia_externa)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload_validado, status=state_machine.REGISTRADO,
            protocolo="proto-detalhe-1", id_contrato_cerc="cerc-detalhe-1",
        )

        garantia_id = get_db(FINANCIADOR_TESTE).table("garantia").select("id").eq("contrato_id", contrato["id"]).execute().data[0]["id"]
        get_db(FINANCIADOR_TESTE).table("garantia_ur").insert({
            "garantia_id": garantia_id, "cnpj_credenciadora": "11111111000111",
            "documento_ufr": "22751826000125", "documento_titular": "22751826000125",
            "codigo_arranjo": "VCC", "data_liquidacao": "2026-09-15", "constituicao": "1",
            "valor_constituido_total": 5000.00, "valor_bloqueado": 0.00,
            "indicador_oneracao": "1", "regras_divisao": "1",
            "valor_onerado": 5000.00, "valor_constituido_efeito": 5000.00,
            "origem": "WEBHOOK", "snapshot_em": datetime.now(timezone.utc),
        }).execute()

        response = Client().get(f"/api/v1/contratos/{FINANCIADOR_TESTE}/{contrato['id']}")
        assert response.status_code == 200
        corpo = response.json()
        assert corpo["referenciaExterna"] == referencia_externa
        assert corpo["status"] == state_machine.REGISTRADO
        assert len(corpo["garantias"]) == 1
        assert corpo["garantias"][0]["referenciaExterna"] == f"{referencia_externa}-G1"
        assert len(corpo["garantias"][0]["unidadesRecebiveisAlcancadas"]) == 1
        assert corpo["garantias"][0]["unidadesRecebiveisAlcancadas"][0]["cnpjCredenciadora"] == "11111111000111"
        assert corpo["indicadoresConsistencia"] == []
        assert isinstance(corpo["garantias"][0]["valorAOnerar"], float)
        assert corpo["garantias"][0]["valorAOnerar"] == 180000.00
        assert isinstance(corpo["garantias"][0]["unidadesRecebiveisAlcancadas"][0]["valorOnerado"], float)
        assert corpo["garantias"][0]["unidadesRecebiveisAlcancadas"][0]["valorOnerado"] == 5000.00
    finally:
        _limpar(referencia_externa)
