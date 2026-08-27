import json

import httpx
import pytest
import respx
from django.test import Client

from apps.contratos.contrato_repository import buscar_contrato_por_referencia
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = f"/api/v1/contratos/{FINANCIADOR_TESTE}"


def _payload(referencia_externa="CTR-TESTE-VIEW-1"):
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-VIEW",
        "documentoContratante": "22751826000125",
        "repactuacao": "0",
        "cnpjDetentor": FINANCIADOR_TESTE,
        "tipoEfeito": "2",
        "saldoDevedor": 150000.00,
        "limiteOperacaoGarantida": 200000.00,
        "valorMantido": 180000.00,
        "dataAssinatura": "2026-08-15",
        "dataVencimento": "2027-08-15",
        "identificacaoGestaoEntidadeRegistradora": "1",
        "modalidadeOperacao": "2",
        "parcelas": [{"vencimento": "2026-09-15", "valor": 12500.00}],
        "garantias": [
            {
                "referenciaExterna": f"{referencia_externa}-G1",
                "domicilioPagamento": {
                    "numeroDocumentoTitular": FINANCIADOR_TESTE, "tipoConta": "CC", "compe": "341",
                    "ispb": "60701190", "agencia": "0001", "numeroConta": "464561-6",
                },
                "definicaoUnidadeRecebivel": {
                    "listaCnpjCredenciadora": ["99T"], "listaCodigoArranjoPagamento": ["99T"],
                    "documentoUsuarioFinalRecebedor": "22751826000125", "documentoTitular": "22751826000125",
                    "dataInicio": "2026-08-26", "dataFim": "2027-08-15",
                },
                "regrasDivisao": "1", "valorAOnerar": 180000.00, "tipoDistribuicao": "padrao_pro_rata_ap",
            },
        ],
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


def _mock_token():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )


def test_criar_contrato_get_retorna_lista_nao_405():
    response = Client().get(URL)
    assert response.status_code == 200
    assert "dados" in response.json()


def test_criar_contrato_corpo_nao_json_retorna_400():
    response = Client().post(URL, data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


def test_criar_contrato_validacao_local_falha_retorna_422():
    payload = _payload("CTR-TESTE-VIEW-INVALIDO")
    payload["saldoDevedor"] = 0.00
    response = Client().post(URL, data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 422
    corpo = response.json()
    assert corpo["codigo"] == "C04"


@respx.mock
def test_criar_contrato_sucesso_207_status_0_persiste_aguardando_webhook():
    referencia_externa = "CTR-TESTE-VIEW-OK"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-1",
                "idDoContrato": "cerc-view-1", "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
                "status": "0", "erros": [],
            }])
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 202
        corpo = response.json()
        assert corpo["status"] == "AGUARDANDO_WEBHOOK"
        assert corpo["referenciaExterna"] == referencia_externa

        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert contrato["status"] == "AGUARDANDO_WEBHOOK"
        assert contrato["protocolo_cerc"] == "proto-view-1"
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_207_status_1_persiste_rejeitado_estrutural_e_retorna_422():
    referencia_externa = "CTR-TESTE-VIEW-REJEITADO"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-2",
                "dataHoraProcessamento": "2026-08-25T12:00:00.000Z", "status": "1",
                "erros": [{"codigo": "107501", "mensagem": "UFR sem vínculo"}],
            }])
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 422
        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert contrato["status"] == "REJEITADO_ESTRUTURAL"
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_referencia_externa_repetida_e_idempotente_nao_chama_cerc_de_novo():
    referencia_externa = "CTR-TESTE-VIEW-DUP"
    _limpar(referencia_externa)
    try:
        _mock_token()
        rota = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-3",
                "dataHoraProcessamento": "2026-08-25T12:00:00.000Z", "status": "0", "erros": [],
            }])
        )

        cliente = Client()
        r1 = cliente.post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")
        r2 = cliente.post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert r1.status_code == 202
        assert r2.status_code == 202
        assert rota.call_count == 1  # segunda chamada não bate na CERC de novo
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_erro_cerc_retorna_502():
    referencia_externa = "CTR-TESTE-VIEW-502"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(500, json={"erro": "indisponível"})
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 502
        assert buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa) is None
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_resubmissao_de_rejeitado_estrutural_chama_cerc_de_novo_e_substitui_o_grafo():
    # Revisão final, achados 1/2: antes, o replay idempotente curto-circuitava
    # TAMBÉM para REJEITADO_ESTRUTURAL — um contrato recusado estruturalmente
    # (que a CERC NUNCA registrou) ficava num beco sem saída permanente sob o
    # mesmo referenciaExterna. Agora a ressubmissão é aceita: a CERC é chamada
    # de novo, o grafo antigo é descartado e o novo é gravado com um id novo,
    # sem violar UNIQUE (cnpj_participante, identificador_contrato).
    referencia_externa = "CTR-TESTE-VIEW-REJEITADO-RETRY"
    _limpar(referencia_externa)
    try:
        _mock_token()
        rota = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            side_effect=[
                httpx.Response(207, json=[{
                    "referenciaExterna": referencia_externa, "protocolo": "proto-view-4a",
                    "dataHoraProcessamento": "2026-08-25T12:00:00.000Z", "status": "1",
                    "erros": [{"codigo": "107501", "mensagem": "UFR sem vínculo"}],
                }]),
                httpx.Response(207, json=[{
                    "referenciaExterna": referencia_externa, "protocolo": "proto-view-4b",
                    "idDoContrato": "cerc-view-4b",
                    "dataHoraProcessamento": "2026-08-25T12:05:00.000Z", "status": "0", "erros": [],
                }]),
            ]
        )

        cliente = Client()
        r1 = cliente.post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")
        assert r1.status_code == 422
        rejeitado = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert rejeitado["status"] == "REJEITADO_ESTRUTURAL"

        r2 = cliente.post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert r2.status_code == 202
        corpo2 = r2.json()
        assert corpo2["status"] == "AGUARDANDO_WEBHOOK"
        assert corpo2["protocolo"] == "proto-view-4b"
        assert rota.call_count == 2  # ressubmissão bate na CERC de novo (ao contrário do replay)

        db = get_db(FINANCIADOR_TESTE)
        linhas = db.table("contrato").select("*").eq("referencia_externa", referencia_externa).execute()
        assert len(linhas.data) == 1  # o grafo antigo foi substituído, não duplicado
        novo = linhas.data[0]
        assert novo["id"] != rejeitado["id"]
        assert novo["status"] == "AGUARDANDO_WEBHOOK"
        assert novo["identificador_contrato"] == rejeitado["identificador_contrato"]

        # nada do grafo antigo sobreviveu
        for tabela in ("garantia", "contrato_domicilio", "contrato_parcela", "contrato_evento"):
            antigas = db.table(tabela).select("*").eq("contrato_id", rejeitado["id"]).execute()
            assert antigas.data == [], tabela
        # e o novo grafo está completo
        assert len(db.table("garantia").select("*").eq("contrato_id", novo["id"]).execute().data) == 1
        assert len(db.table("contrato_parcela").select("*").eq("contrato_id", novo["id"]).execute().data) == 1
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_rejeicao_estrutural_devolve_erros_da_cerc_e_grava_contrato_evento():
    # Revisão final, achado 8: os erros[] do item 207 eram lidos e descartados —
    # o chamador recebia um 422 sem nenhuma indicação do motivo, e nada ficava
    # na trilha de auditoria.
    referencia_externa = "CTR-TESTE-VIEW-REJEITADO-ERROS"
    _limpar(referencia_externa)
    try:
        _mock_token()
        erros_cerc = [
            {"codigo": "107501", "mensagem": "UFR sem vínculo"},
            {"codigo": "107502", "mensagem": "arranjo não habilitado"},
        ]
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-erros",
                "dataHoraProcessamento": "2026-08-25T12:00:00.000Z", "status": "1",
                "erros": erros_cerc,
            }])
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 422
        corpo = response.json()
        assert corpo["erros"] == erros_cerc
        assert corpo["protocolo"] == "proto-view-erros"

        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        eventos = get_db(FINANCIADOR_TESTE).table("contrato_evento").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(eventos.data) == 1
        assert eventos.data[0]["tipo"] == "rejeicao_estrutural"
        assert eventos.data[0]["payload"]["erros"] == erros_cerc
    finally:
        _limpar(referencia_externa)


@pytest.mark.parametrize("corpo_json", ["[]", '"apenas uma string"', "42", "null"])
def test_criar_contrato_corpo_json_nao_objeto_retorna_400(corpo_json):
    # Revisão final, achado 7: json.loads aceita qualquer valor JSON de topo;
    # payload.get(...) num array/string/número/None explodia com AttributeError
    # (500) em vez de devolver um 400 limpo.
    response = Client().post(URL, data=corpo_json, content_type="application/json")
    assert response.status_code == 400
    assert "objeto JSON" in response.json()["erro"]


@respx.mock
def test_criar_contrato_falha_ao_persistir_apos_207_retorna_500_e_nao_propaga(monkeypatch):
    # Revisão final, achados 1/2 (Parte B): tudo entre "a CERC aceitou" e a
    # resposta HTTP era completamente desprotegido. Qualquer falha ali significa
    # dado financeiro real submetido à CERC sem registro local — tem que virar
    # um 500 logado com protocolo/referência, nunca uma exceção não tratada.
    referencia_externa = "CTR-TESTE-VIEW-FALHA-PERSISTIR"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[{
                "referenciaExterna": referencia_externa, "protocolo": "proto-view-boom",
                "idDoContrato": "cerc-view-boom", "dataHoraProcessamento": "2026-08-25T12:00:00.000Z",
                "status": "0", "erros": [],
            }])
        )

        def _explode(*args, **kwargs):
            raise RuntimeError("banco caiu no meio da persistência")

        monkeypatch.setattr("apps.contratos.views.inserir_contrato_criado", _explode)

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 500
        corpo = response.json()
        # o protocolo precisa voltar (e ir para o log) para permitir conciliação manual
        assert corpo["protocolo"] == "proto-view-boom"
        assert corpo["referenciaExterna"] == referencia_externa
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_207_com_array_vazio_retorna_500_em_vez_de_index_error():
    referencia_externa = "CTR-TESTE-VIEW-207-VAZIO"
    _limpar(referencia_externa)
    try:
        _mock_token()
        respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
            return_value=httpx.Response(207, json=[])
        )

        response = Client().post(URL, data=json.dumps(_payload(referencia_externa)), content_type="application/json")

        assert response.status_code == 500
        assert buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa) is None
    finally:
        _limpar(referencia_externa)


@respx.mock
def test_criar_contrato_campo_obrigatorio_nivel_contrato_ausente_retorna_422_sem_chamar_cerc():
    # Regressão (task review, rodada 1): identificacaoGestaoEntidadeRegistradora
    # só era checado dentro do loop `for g in garantias`, que não roda quando
    # garantias == [] (legítimo para repactuacao="1", C03) — o payload passava
    # pela validação local inteira, era submetido à CERC de verdade, e só
    # então explodia com KeyError não tratado ao persistir. Deliberadamente
    # NÃO registra nenhuma rota respx para a CERC: se o código tentasse
    # chamá-la mesmo assim, respx levantaria (nenhuma rota corresponde) e a
    # asserção de 422 abaixo falharia — provando que a CERC nunca é chamada.
    referencia_externa = "CTR-TESTE-VIEW-SEM-GESTAO"
    _limpar(referencia_externa)
    try:
        payload = _payload(referencia_externa)
        payload["garantias"] = []
        payload["repactuacao"] = "1"
        del payload["identificacaoGestaoEntidadeRegistradora"]

        response = Client().post(URL, data=json.dumps(payload), content_type="application/json")

        assert response.status_code == 422
        corpo = response.json()
        assert corpo["codigo"] == "CAMPO_OBRIGATORIO"
        assert buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa) is None
    finally:
        _limpar(referencia_externa)
