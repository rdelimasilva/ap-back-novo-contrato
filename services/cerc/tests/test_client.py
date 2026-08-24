from dotenv import load_dotenv
load_dotenv()

import json
import os

import httpx
import pytest
import respx

os.environ.setdefault("CERC_AUTH_URL", "https://api.int.cerc.com/oauth/token")
os.environ.setdefault("CERC_API_BASE_URL", "https://ap-homolog.cerc.inf.br")

FINANCIADOR_TESTE = "12345678000199"

from services.cerc import client, token_provider  # noqa: E402
from shared.cloudsql_client import get_db  # noqa: E402


def _mock_token():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )


def _multistatus(protocolo="P-1", referencia="CTR-2026-000001", status="0"):
    return [
        {
            "referenciaExterna": referencia,
            "protocolo": protocolo,
            "dataHoraProcessamento": "2026-08-24T12:00:00.000Z",
            "status": status,
            "erros": [],
        }
    ]


@pytest.fixture(autouse=True)
def _reset_state():
    token_provider._caches.clear()
    token_provider._locks.clear()
    import shared.tenant_config as tenant_config_module
    tenant_config_module._cache.clear()

    db = get_db(FINANCIADOR_TESTE)
    db.table("cerc_requisicao").delete().eq("correlacao_id", "corr-1").execute()
    yield
    db.table("cerc_requisicao").delete().eq("correlacao_id", "corr-1").execute()
    tenant_config_module._cache.clear()


@respx.mock
def test_criar_contrato_sends_array_body_with_tipo_operacao_c():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus())
    )

    result = client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert result == _multistatus()
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "tipoOperacao": "C"}]

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["http_status"] == 207
    assert logged.data[0]["recurso"] == "/v15/contratos"
    assert logged.data[0]["tentativa"] == 1


@respx.mock
def test_criar_contrato_retries_once_on_401():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-expired", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-fresh", "expires_in": 3600}),
        ]
    )
    contratos_route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        side_effect=[
            httpx.Response(401, json={"erro": "token expirado"}),
            httpx.Response(207, json=_multistatus()),
        ]
    )

    result = client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert result == _multistatus()
    assert contratos_route.call_count == 2

    logged = (
        get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*")
        .eq("correlacao_id", "corr-1").order("tentativa").execute()
    )
    assert [row["tentativa"] for row in logged.data] == [1, 2]
    assert logged.data[0]["http_status"] == 401
    assert logged.data[1]["http_status"] == 207


@respx.mock
def test_criar_contrato_raises_cerc_api_error_on_4xx():
    _mock_token()
    respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(422, json={"codigo": "107807", "mensagem": "campo estático"})
    )

    with pytest.raises(client.CercApiError) as exc:
        client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert exc.value.status_code == 422
    assert exc.value.body == {"codigo": "107807", "mensagem": "campo estático"}

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["http_status"] == 422


@respx.mock
def test_criar_contrato_logs_before_raising_on_transport_failure():
    _mock_token()
    respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    with pytest.raises(httpx.ConnectError):
        client.criar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["http_status"] is None
    assert logged.data[0]["tentativa"] == 1


@respx.mock
def test_atualizar_contrato_sends_tipo_operacao_a():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus(status="0"))
    )

    result = client.atualizar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001", "cnpjDetentor": "99999999000191"}, correlacao_id="corr-1")

    assert result[0]["status"] == "0"
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "cnpjDetentor": "99999999000191", "tipoOperacao": "A"}]


@respx.mock
def test_inativar_contrato_sends_tipo_operacao_i():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus(status="0"))
    )

    client.inativar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "tipoOperacao": "I"}]


@respx.mock
def test_baixar_contrato_sends_tipo_operacao_b():
    _mock_token()
    route = respx.put("https://ap-homolog.cerc.inf.br/v15/contratos").mock(
        return_value=httpx.Response(207, json=_multistatus(status="0"))
    )

    client.baixar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == [{"referenciaExterna": "CTR-2026-000001", "tipoOperacao": "B"}]


@respx.mock
def test_consultar_contrato_sends_plain_object_body_and_returns_dict():
    _mock_token()
    detalhe = {
        "referenciaExterna": "CTR-2026-000001",
        "identificadorContrato": "OP-88231",
        "quantidadeUnidadesRecebiveisAlcancadas": 3,
    }
    route = respx.post("https://ap-homolog.cerc.inf.br/contrato/consultar").mock(
        return_value=httpx.Response(200, json=detalhe)
    )

    result = client.consultar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-2026-000001"}, correlacao_id="corr-1")

    assert result == detalhe
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {"referenciaExterna": "CTR-2026-000001"}  # objeto puro, não array

    logged = get_db(FINANCIADOR_TESTE).table("cerc_requisicao").select("*").eq("correlacao_id", "corr-1").execute()
    assert len(logged.data) == 1
    assert logged.data[0]["recurso"] == "/contrato/consultar"


@respx.mock
def test_consultar_contrato_raises_cerc_api_error_on_404():
    _mock_token()
    respx.post("https://ap-homolog.cerc.inf.br/contrato/consultar").mock(
        return_value=httpx.Response(404, json={"codigo": "113005", "mensagem": "contrato inexistente"})
    )

    with pytest.raises(client.CercApiError) as exc:
        client.consultar_contrato(FINANCIADOR_TESTE, {"referenciaExterna": "CTR-INEXISTENTE"}, correlacao_id="corr-1")

    assert exc.value.status_code == 404
