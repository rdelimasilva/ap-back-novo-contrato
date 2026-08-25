import base64
import json
import uuid
from datetime import date, datetime

import pytest
from django.test import Client
from ulid import ULID

from apps.contratos import views
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/webhooks/contrato/processar"


def _push_envelope(webhook_inbox_id, financiador_id=FINANCIADOR_TESTE):
    dados = json.dumps({"webhook_inbox_id": webhook_inbox_id, "financiador_id": financiador_id}).encode()
    return json.dumps({
        "message": {"data": base64.b64encode(dados).decode(), "messageId": "msg-1", "publishTime": "2026-08-25T12:00:00Z"},
        "subscription": "projects/registradora-506000/subscriptions/contratos-webhook-inbox-sub",
    })


def _criar_contrato(status, referencia_externa):
    contrato_id = str(uuid.uuid4())
    get_db(FINANCIADOR_TESTE).table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": referencia_externa,
        "identificador_contrato": "OP-TESTE",
        "status": status,
        "cnpj_participante": FINANCIADOR_TESTE,
        "documento_contratante": "22751826000125",
        "cnpj_detentor": FINANCIADOR_TESTE,
        "tipo_efeito": "2",
        "modalidade_operacao": "2",
        "gestao_entidade_registradora": "1",
        "saldo_devedor": 150000.00,
        "limite_operacao_garantida": 200000.00,
        "valor_mantido": 180000.00,
        "data_assinatura": date(2026, 8, 15),
        "data_vencimento": date(2027, 8, 15),
        "repactuacao": False,
    }).execute()
    return contrato_id


def _criar_garantia(contrato_id, referencia_externa):
    garantia_id = str(uuid.uuid4())
    get_db(FINANCIADOR_TESTE).table("garantia").insert({
        "id": garantia_id,
        "contrato_id": contrato_id,
        "referencia_externa": referencia_externa,
        "regras_divisao": "1",
        "valor_a_onerar": 180000.00,
        "def_lista_credenciadoras": ["99T"],
        "def_lista_arranjos": ["VCC", "MCC"],
        "def_data_inicio": date(2026, 8, 18),
        "def_data_fim": date(2027, 8, 15),
    }).execute()
    return garantia_id


def _criar_webhook_inbox(payload, processado_em=None):
    webhook_id = str(ULID())
    get_db(FINANCIADOR_TESTE).table("webhook_inbox").insert({
        "id": webhook_id,
        "tipo_evento": payload["tipoEvento"],
        "data_hora_evento": datetime.fromisoformat(payload["dataHoraEvento"]),
        "payload": payload,
        "hash_dedupe": webhook_id,  # único por linha de teste, dedupe não é o que este teste exercita
        "processado_em": datetime.fromisoformat(processado_em) if processado_em else None,
    }).execute()
    return webhook_id


def _limpar(contrato_id=None, garantia_id=None, webhook_inbox_id=None):
    db = get_db(FINANCIADOR_TESTE)
    if contrato_id:
        db.table("indicador_consistencia").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
    if garantia_id:
        db.table("garantia_ur").delete().eq("garantia_id", garantia_id).execute()
        db.table("garantia").delete().eq("id", garantia_id).execute()
    if contrato_id:
        db.table("contrato").delete().eq("id", contrato_id).execute()
    if webhook_inbox_id:
        db.table("webhook_inbox").delete().eq("id", webhook_inbox_id).execute()


def _envelope_sucesso(referencia_externa, referencia_garantia, resultado="1"):
    return {
        "tipoEvento": "contrato",
        "dataHoraEvento": "2026-08-25T12:00:00.000Z",
        "evento": {
            "referenciaExterna": referencia_externa,
            "protocolo": "proto-1",
            "status": "0",
            "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
            "quantidadeUnidadesRecebiveisAlcancadas": 1,
            "valorUnidadesRecebiveisAlcancadas": 5000.00,
            "resultadoDistribuicaoOnus": resultado,
            "garantiasAlcancadas": [{
                "referenciaExterna": referencia_garantia,
                "unidadesRecebiveisAlcancadas": [{
                    "cnpjCredenciadora": "11111111000111",
                    "documentoUsuarioFinalRecebedor": "22222222000122",
                    "documentoTitular": "22222222000122",
                    "codigoArranjoPagamento": "VCC",
                    "dataLiquidacao": "2026-09-01",
                    "constituicao": "1",
                    "valorConstituidoTotal": 5000.00,
                    "valorBloqueado": 0.00,
                    "indicadorOneracao": "1",
                    "regrasDivisao": "1",
                    "valorOnerado": 5000.00,
                    "valorConstituidoEfeito": 5000.00,
                }],
            }],
            "indicadoresConsistencia": [
                {"indicador": "estabilidade_agenda", "resultado": "estável", "parametros": [], "criticidade": "0"},
            ],
        },
    }


def _envelope_falha(referencia_externa):
    return {
        "tipoEvento": "contrato",
        "dataHoraEvento": "2026-08-25T12:00:00.000Z",
        "evento": {
            "referenciaExterna": referencia_externa, "protocolo": "proto-1", "status": "1",
            "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
            "erros": [{"codigo": "107501", "mensagem": "UFR sem vínculo"}],
        },
    }


@pytest.fixture(autouse=True)
def _oidc_ok(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)


def test_processor_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL, data=_push_envelope("qualquer-id"), content_type="application/json")
    assert response.status_code == 401


def test_processor_envelope_pubsub_malformado_retorna_400():
    response = Client().post(URL, data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


def test_processor_webhook_inbox_nao_encontrado_retorna_404():
    response = Client().post(URL, data=_push_envelope("id-inexistente"), content_type="application/json")
    assert response.status_code == 404


def test_processor_ja_processado_e_idempotente():
    webhook_id = _criar_webhook_inbox(_envelope_falha("CTR-TESTE-PROC-DUP"), processado_em="2026-08-25T12:05:00Z")
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_contrato_nao_encontrado_retorna_500():
    webhook_id = _criar_webhook_inbox(_envelope_sucesso("CTR-TESTE-PROC-SEMCONTRATO", "G1"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 500

        linha = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha.data[0]["processado_em"] is None  # deixado para nova entrega do Pub/Sub
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_evento_sucesso_atualiza_contrato_e_persiste_urs_e_indicadores():
    referencia_externa = "CTR-TESTE-PROC-OK"
    referencia_garantia = "CTR-TESTE-PROC-OK-G1"
    contrato_id = _criar_contrato("AGUARDANDO_WEBHOOK", referencia_externa)
    garantia_id = _criar_garantia(contrato_id, referencia_garantia)
    webhook_id = _criar_webhook_inbox(_envelope_sucesso(referencia_externa, referencia_garantia, resultado="1"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        contrato = db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]
        assert contrato["status"] == "REGISTRADO"
        assert contrato["status_garantia"] == "SUFICIENTE"
        assert contrato["resultado_distribuicao"] == "1"
        assert contrato["qtd_urs_alcancadas"] == 1

        urs = db.table("garantia_ur").select("*").eq("garantia_id", garantia_id).execute()
        assert len(urs.data) == 1
        assert urs.data[0]["cnpj_credenciadora"] == "11111111000111"
        assert urs.data[0]["origem"] == "WEBHOOK"

        indicadores = db.table("indicador_consistencia").select("*").eq("contrato_id", contrato_id).execute()
        assert len(indicadores.data) == 1
        assert indicadores.data[0]["indicador"] == "estabilidade_agenda"

        eventos = db.table("contrato_evento").select("*").eq("contrato_id", contrato_id).execute()
        assert any(e["tipo"] == "webhook_recebido" for e in eventos.data)

        linha_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha_inbox.data[0]["processado_em"] is not None
    finally:
        _limpar(contrato_id=contrato_id, garantia_id=garantia_id, webhook_inbox_id=webhook_id)


def test_processor_evento_falha_marca_rejeitado_sem_escrever_urs():
    referencia_externa = "CTR-TESTE-PROC-FALHA"
    contrato_id = _criar_contrato("AGUARDANDO_WEBHOOK", referencia_externa)
    webhook_id = _criar_webhook_inbox(_envelope_falha(referencia_externa))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        contrato = db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]
        assert contrato["status"] == "REJEITADO"
        assert contrato["qtd_urs_alcancadas"] is None
    finally:
        _limpar(contrato_id=contrato_id, webhook_inbox_id=webhook_id)


def test_processor_resultado_insuficiente_emite_evento_subgarantido():
    referencia_externa = "CTR-TESTE-PROC-SUBGARANTIDO"
    referencia_garantia = "CTR-TESTE-PROC-SUBGARANTIDO-G1"
    contrato_id = _criar_contrato("AGUARDANDO_WEBHOOK", referencia_externa)
    garantia_id = _criar_garantia(contrato_id, referencia_garantia)
    webhook_id = _criar_webhook_inbox(_envelope_sucesso(referencia_externa, referencia_garantia, resultado="2"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        eventos = get_db(FINANCIADOR_TESTE).table("contrato_evento").select("*").eq("contrato_id", contrato_id).execute()
        assert any(e["tipo"] == "ContratoSubgarantido" for e in eventos.data)
    finally:
        _limpar(contrato_id=contrato_id, garantia_id=garantia_id, webhook_inbox_id=webhook_id)


def test_processor_estado_invalido_marca_processado_sem_atualizar_contrato():
    referencia_externa = "CTR-TESTE-PROC-ESTADOINVALIDO"
    contrato_id = _criar_contrato("REGISTRADO", referencia_externa)  # já registrado — webhook de novo é inesperado
    webhook_id = _criar_webhook_inbox(_envelope_falha(referencia_externa))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        contrato = db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]
        assert contrato["status"] == "REGISTRADO"  # inalterado

        linha_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha_inbox.data[0]["processado_em"] is not None
        assert linha_inbox.data[0]["erro"] is not None
    finally:
        _limpar(contrato_id=contrato_id, webhook_inbox_id=webhook_id)


def test_processor_tipo_evento_diferente_de_contrato_e_ignorado_mas_marcado_processado():
    payload = {"tipoEvento": "agenda", "dataHoraEvento": "2026-08-25T12:00:00.000Z", "evento": {"algumCampo": "valor"}}
    webhook_id = _criar_webhook_inbox(payload)
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        linha_inbox = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha_inbox.data[0]["processado_em"] is not None
    finally:
        _limpar(webhook_inbox_id=webhook_id)
