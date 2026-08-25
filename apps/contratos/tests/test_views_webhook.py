import base64
import json

import pytest
from django.test import Client

from apps.contratos.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

FINANCIADOR_TESTE = "12345678000199"
URL = f"/api/v1/webhooks/contrato/{FINANCIADOR_TESTE}"


def _basic_auth_header():
    config = get_tenant_config(FINANCIADOR_TESTE)
    credenciais = f"{config['webhook_basic_user']}:{config['webhook_basic_password']}"
    return "Basic " + base64.b64encode(credenciais.encode()).decode()


def _envelope(referencia, data_hora="2026-08-17T12:00:00.000Z"):
    return {
        "tipoEvento": "contrato",
        "dataHoraEvento": data_hora,
        "evento": {"referenciaExterna": referencia, "protocolo": "proto-1", "status": "0"},
    }


def _limpar(envelope):
    h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
    get_db(FINANCIADOR_TESTE).table("webhook_inbox").delete().eq("hash_dedupe", h).execute()


@pytest.fixture
def publicados(monkeypatch):
    chamadas = []
    monkeypatch.setattr(pubsub_client, "publish_webhook_contrato", lambda *a, **k: chamadas.append(a))
    return chamadas


def test_webhook_sem_autenticacao_retorna_401(publicados):
    response = Client().post(URL, data=json.dumps(_envelope("CTR-TESTE-WEBHOOK-NOAUTH")), content_type="application/json")
    assert response.status_code == 401
    assert publicados == []


def test_webhook_com_credenciais_erradas_retorna_401(publicados):
    response = Client().post(
        URL, data=json.dumps(_envelope("CTR-TESTE-WEBHOOK-BADAUTH")), content_type="application/json",
        HTTP_AUTHORIZATION="Basic " + base64.b64encode(b"errado:errado").decode(),
    )
    assert response.status_code == 401
    assert publicados == []


def test_webhook_tenant_desconhecido_retorna_401(publicados):
    response = Client().post(
        "/api/v1/webhooks/contrato/00000000000000",
        data=json.dumps(_envelope("CTR-TESTE-WEBHOOK-TENANT")), content_type="application/json",
        HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 401


def test_webhook_get_retorna_405():
    response = Client().get(URL)
    assert response.status_code == 405


def test_webhook_corpo_nao_json_retorna_400():
    response = Client().post(
        URL, data="isto nao e json", content_type="text/plain", HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 400


def test_webhook_envelope_sem_campos_obrigatorios_retorna_400():
    response = Client().post(
        URL, data=json.dumps({"tipoEvento": "contrato"}), content_type="application/json",
        HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 400


def test_webhook_valido_persiste_no_inbox_e_publica(publicados):
    envelope = _envelope("CTR-TESTE-WEBHOOK-OK")
    _limpar(envelope)
    try:
        response = Client().post(
            URL, data=json.dumps(envelope), content_type="application/json",
            HTTP_AUTHORIZATION=_basic_auth_header(),
        )
        assert response.status_code == 202

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvo = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvo.data) == 1
        assert salvo.data[0]["tipo_evento"] == "contrato"
        assert salvo.data[0]["payload"] == envelope
        assert salvo.data[0]["processado_em"] is None

        assert len(publicados) == 1
        assert publicados[0][1] == FINANCIADOR_TESTE
    finally:
        _limpar(envelope)


def test_webhook_duplicado_nao_gera_segunda_linha_nem_publica_de_novo(publicados):
    envelope = _envelope("CTR-TESTE-WEBHOOK-DUP")
    _limpar(envelope)
    try:
        cliente = Client()
        r1 = cliente.post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())
        r2 = cliente.post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())

        assert r1.status_code == 202
        assert r2.status_code == 202

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvos = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvos.data) == 1
        assert len(publicados) == 1
    finally:
        _limpar(envelope)


def test_webhook_responde_202_mesmo_quando_publish_falha(monkeypatch):
    envelope = _envelope("CTR-TESTE-WEBHOOK-PUBFAIL")
    _limpar(envelope)

    def _falha(*args, **kwargs):
        raise RuntimeError("Pub/Sub indisponível")

    monkeypatch.setattr(pubsub_client, "publish_webhook_contrato", _falha)
    try:
        response = Client().post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())
        assert response.status_code == 202  # linha já persistida — publish é melhor-esforço, não derruba a resposta

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvo = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvo.data) == 1  # persistiu mesmo com o publish quebrado
    finally:
        _limpar(envelope)
