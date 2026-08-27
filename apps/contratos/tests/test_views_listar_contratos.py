from django.test import Client

from apps.contratos.contrato_repository import buscar_contrato_por_referencia, inserir_contrato_criado, remover_contrato_rejeitado
from apps.contratos import state_machine
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL_LISTA = f"/api/v1/contratos/{FINANCIADOR_TESTE}"


def _payload_minimo(referencia_externa):
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-LISTA",
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


def test_listar_contratos_retorna_dados_como_lista():
    response = Client().get(URL_LISTA)
    assert response.status_code == 200
    corpo = response.json()
    assert isinstance(corpo["dados"], list)


def test_listar_contratos_inclui_contrato_recem_criado():
    referencia_externa = "CTR-TESTE-LISTA-1"
    _limpar(referencia_externa)
    try:
        payload_validado = {**_payload_minimo(referencia_externa), "garantias": [], "identificacaoContratosAnteriores": [], "parcelas": []}
        inserir_contrato_criado(
            FINANCIADOR_TESTE, payload_validado, status=state_machine.AGUARDANDO_WEBHOOK,
            protocolo="proto-lista-1", id_contrato_cerc="cerc-lista-1",
        )

        response = Client().get(URL_LISTA)
        assert response.status_code == 200
        dados = response.json()["dados"]
        encontrado = next((c for c in dados if c["referenciaExterna"] == referencia_externa), None)
        assert encontrado is not None
        assert encontrado["status"] == state_machine.AGUARDANDO_WEBHOOK
        assert encontrado["protocolo"] == "proto-lista-1"
        assert encontrado["saldoDevedor"] == 150000.00
    finally:
        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        if contrato:
            remover_contrato_rejeitado(FINANCIADOR_TESTE, contrato["id"])


def test_listar_contratos_filtro_status_exclui_outros_status():
    referencia_externa = "CTR-TESTE-LISTA-FILTRO"
    _limpar(referencia_externa)
    try:
        payload_validado = {**_payload_minimo(referencia_externa), "garantias": [], "identificacaoContratosAnteriores": [], "parcelas": []}
        inserir_contrato_criado(
            FINANCIADOR_TESTE, payload_validado, status=state_machine.REJEITADO_ESTRUTURAL,
            protocolo=None, id_contrato_cerc=None,
        )

        response = Client().get(f"{URL_LISTA}?status=REGISTRADO")
        assert response.status_code == 200
        referencias = [c["referenciaExterna"] for c in response.json()["dados"]]
        assert referencia_externa not in referencias
    finally:
        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        if contrato:
            remover_contrato_rejeitado(FINANCIADOR_TESTE, contrato["id"])


def test_contratos_metodo_nao_suportado_retorna_405():
    response = Client().put(URL_LISTA, data="{}", content_type="application/json")
    assert response.status_code == 405


def test_listar_contratos_limit_nao_inteiro_retorna_400():
    response = Client().get(f"{URL_LISTA}?limit=abc")
    assert response.status_code == 400
    corpo = response.json()
    assert "limit" in corpo.get("erro", "").lower()


def test_listar_contratos_limit_negativo_retorna_400():
    response = Client().get(f"{URL_LISTA}?limit=-5")
    assert response.status_code == 400
    corpo = response.json()
    assert "limit" in corpo.get("erro", "").lower()


def test_listar_contratos_limit_zero_retorna_400():
    response = Client().get(f"{URL_LISTA}?limit=0")
    assert response.status_code == 400
    corpo = response.json()
    assert "limit" in corpo.get("erro", "").lower()
