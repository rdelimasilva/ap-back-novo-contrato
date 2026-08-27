import json
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from django.test import Client

from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"

# (tipoOperacao, sufixo da URL, estado de ESPERA, estado TERMINAL, o "outro"
# estado de espera/terminal — usado nos testes de conflito cruzado I<->B)
OPERACOES = [
    ("I", "inativar", "INATIVANDO", "INATIVADO", "BAIXANDO"),
    ("B", "baixar", "BAIXANDO", "BAIXADO", "INATIVANDO"),
]


def _url(sufixo):
    return f"/api/v1/contratos/{FINANCIADOR_TESTE}/{sufixo}"


def _mock_token():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )


def _inserir_contrato(referencia_externa: str, status: str, identificador_contrato: str = "OP-TESTE-POS-REGISTRO-VIEW") -> dict:
    db = get_db(FINANCIADOR_TESTE)
    contrato_id = str(uuid.uuid4())
    db.table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": referencia_externa,
        "identificador_contrato": identificador_contrato,
        "protocolo_cerc": "proto-original",
        "status": status,
        "cnpj_participante": FINANCIADOR_TESTE,
        "documento_contratante": "22751826000125",
        "cnpj_detentor": FINANCIADOR_TESTE,
        "tipo_efeito": "2",
        "modalidade_operacao": "2",
        "gestao_entidade_registradora": "1",
        "saldo_devedor": Decimal("150000.00"),
        "limite_operacao_garantida": Decimal("200000.00"),
        "valor_mantido": Decimal("180000.00"),
        "data_assinatura": date(2026, 8, 15),
        "data_vencimento": date(2027, 8, 15),
        "repactuacao": False,
    }).execute()
    return db.table("contrato").select("*").eq("id", contrato_id).execute().data[0]


def _limpar(contrato_id):
    db = get_db(FINANCIADOR_TESTE)
    db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
    db.table("contrato").delete().eq("id", contrato_id).execute()


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_get_retorna_405(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().get(_url(sufixo))
    assert response.status_code == 405


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_corpo_nao_json_retorna_400(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().post(_url(sufixo), data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
@pytest.mark.parametrize("corpo_json", ["[]", '"string"', "42", "null"])
def test_corpo_json_nao_objeto_retorna_400(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro, corpo_json):
    response = Client().post(_url(sufixo), data=corpo_json, content_type="application/json")
    assert response.status_code == 400


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_referencia_externa_ausente_retorna_422(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().post(_url(sufixo), data=json.dumps({}), content_type="application/json")
    assert response.status_code == 422
    assert response.json()["codigo"] == "CAMPO_OBRIGATORIO"


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_contrato_nao_encontrado_retorna_404(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    response = Client().post(
        _url(sufixo), data=json.dumps({"referenciaExterna": "CTR-NUNCA-EXISTIU"}), content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_estado_incompativel_retorna_409(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-409"
    contrato = _inserir_contrato(referencia_externa, status="AGUARDANDO_WEBHOOK")
    try:
        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")
        assert response.status_code == 409
    finally:
        _limpar(contrato["id"])


@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,outro_estado_espera", OPERACOES)
def test_conflito_com_a_outra_operacao_em_curso_retorna_409(tipo_operacao, sufixo, estado_espera, estado_terminal, outro_estado_espera):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-409-CRUZADO"
    contrato = _inserir_contrato(referencia_externa, status=outro_estado_espera)
    try:
        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")
        assert response.status_code == 409
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_sucesso_207_status_0_persiste_estado_de_espera(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-OK"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": f"proto-{sufixo}-1",
                "dataHoraProcessamento": "2026-08-26T12:00:00.000Z", "status": "0", "erros": [],
            }])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 202
        corpo = response.json()
        assert corpo["status"] == estado_espera
        assert corpo["protocolo"] == f"proto-{sufixo}-1"

        atualizado = get_db(FINANCIADOR_TESTE).table("contrato").select("*").eq("id", contrato["id"]).execute().data[0]
        assert atualizado["status"] == estado_espera
        assert atualizado["protocolo_cerc"] == f"proto-{sufixo}-1"
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_207_status_1_volta_para_registrado_e_retorna_422(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-REJEITADO"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        erros_cerc = [{"codigo": "107xxx", "mensagem": "operação recusada"}]
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": f"proto-{sufixo}-2",
                "dataHoraProcessamento": "2026-08-26T12:00:00.000Z", "status": "1", "erros": erros_cerc,
            }])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 422
        corpo = response.json()
        assert corpo["status"] == "REGISTRADO"
        assert corpo["erros"] == erros_cerc

        atualizado = get_db(FINANCIADOR_TESTE).table("contrato").select("*").eq("id", contrato["id"]).execute().data[0]
        assert atualizado["status"] == "REGISTRADO"
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_replay_a_partir_do_estado_de_espera_nao_chama_a_cerc_de_novo(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-REPLAY-ESPERA"
    contrato = _inserir_contrato(referencia_externa, status=estado_espera)
    try:
        rota = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{"referenciaExterna": referencia_externa, "protocolo": "nao-deveria-usar", "status": "0", "erros": []}])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 202
        assert response.json()["status"] == estado_espera
        assert rota.call_count == 0
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_replay_a_partir_do_estado_terminal_nao_chama_a_cerc_de_novo(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-REPLAY-TERMINAL"
    contrato = _inserir_contrato(referencia_externa, status=estado_terminal)
    try:
        rota = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{"referenciaExterna": referencia_externa, "protocolo": "nao-deveria-usar", "status": "0", "erros": []}])
        )

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 202
        assert response.json()["status"] == estado_terminal
        assert rota.call_count == 0
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_erro_cerc_retorna_502(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-502"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(return_value=httpx.Response(500, json={"erro": "indisponível"}))

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 502
        atualizado = get_db(FINANCIADOR_TESTE).table("contrato").select("*").eq("id", contrato["id"]).execute().data[0]
        assert atualizado["status"] == "REGISTRADO"  # nada mudou localmente
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_207_com_array_vazio_retorna_500(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-207-VAZIO"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(return_value=httpx.Response(207, json=[]))

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 500
    finally:
        _limpar(contrato["id"])


@respx.mock
@pytest.mark.parametrize("tipo_operacao,sufixo,estado_espera,estado_terminal,_outro", OPERACOES)
def test_falha_ao_persistir_apos_207_retorna_500(tipo_operacao, sufixo, estado_espera, estado_terminal, _outro, monkeypatch):
    referencia_externa = f"CTR-TESTE-VIEW-{sufixo}-FALHA-PERSISTIR"
    contrato = _inserir_contrato(referencia_externa, status="REGISTRADO")
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-boom",
                "dataHoraProcessamento": "2026-08-26T12:00:00.000Z", "status": "0", "erros": [],
            }])
        )

        def _explode(*args, **kwargs):
            raise RuntimeError("banco caiu no meio da persistência")

        monkeypatch.setattr("apps.contratos.views.atualizar_status_pos_registro", _explode)

        response = Client().post(_url(sufixo), data=json.dumps({"referenciaExterna": referencia_externa}), content_type="application/json")

        assert response.status_code == 500
        corpo = response.json()
        assert corpo["protocolo"] == "proto-boom"
        assert corpo["referenciaExterna"] == referencia_externa
    finally:
        _limpar(contrato["id"])
