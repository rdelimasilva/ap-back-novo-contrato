"""Cliente REST da CERC — criar/atualizar/inativar/baixar/consultar contrato.

Toda chamada grava uma linha em cerc_requisicao ANTES de decidir se levanta
CercApiError (design §4) — a trilha de auditoria existe mesmo quando a
chamada termina em erro. Em 401, invalida o token do tenant (Plano 06) e
repete a mesma chamada uma única vez, com uma segunda linha de log
(tentativa=2).

SPEC-02 §4: PUT /v15/contratos recebe sempre um array (lote), mesmo para
um único item, e responde 207 multi-status (array, um item por entrada
enviada) — o parsing item-a-item do 207 é responsabilidade de quem
consome o retorno desta camada de transporte, não deste módulo. Diferente
de PUT /v15/contratos: POST /contrato/consultar (SPEC-02 §6.1) recebe um
objeto puro (não array) e responde 200 com o detalhe do contrato — logo
consultar_contrato devolve um dict, não uma lista.

Multi-tenancy: toda função pública recebe financiador_id como primeiro
parâmetro — usado para buscar o token do tenant certo
(services/cerc/token_provider.py) e gravar a auditoria em cerc_requisicao
do banco do tenant certo (shared/cloudsql_client.py). Ver
docs/superpowers/specs/2026-08-24-contratos-service-design.md §1.1.
"""

import os
import uuid

import httpx

from services.cerc.token_provider import get_cerc_token, invalidate_token
from shared.cloudsql_client import get_db


class CercApiError(Exception):
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"CERC API respondeu {status_code}: {body}")


def _log_attempt(financiador_id: str, recurso: str, correlacao_id: str, request_body, response, tentativa: int) -> None:
    get_db(financiador_id).table("cerc_requisicao").insert({
        "id": str(uuid.uuid4()),
        "recurso": recurso,
        "correlacao_id": correlacao_id,
        "http_status": response.status_code if response is not None else None,
        "request_body": request_body,
        "response_body": _safe_json(response),
        "tentativa": tentativa,
    }).execute()


def _safe_json(response):
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def _send(method: str, path: str, body, correlacao_id: str, token: str) -> httpx.Response:
    url = os.environ["CERC_API_BASE_URL"] + path
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": correlacao_id,
    }
    return httpx.request(method, url, json=body, headers=headers, timeout=15.0)


def _request(financiador_id: str, method: str, path: str, body, correlacao_id: str):
    token = get_cerc_token(financiador_id)
    try:
        response = _send(method, path, body, correlacao_id, token)
    except httpx.HTTPError:
        _log_attempt(financiador_id, path, correlacao_id, body, None, tentativa=1)
        raise
    _log_attempt(financiador_id, path, correlacao_id, body, response, tentativa=1)

    if response.status_code == 401:
        invalidate_token(financiador_id)
        token = get_cerc_token(financiador_id)
        try:
            response = _send(method, path, body, correlacao_id, token)
        except httpx.HTTPError:
            _log_attempt(financiador_id, path, correlacao_id, body, None, tentativa=2)
            raise
        _log_attempt(financiador_id, path, correlacao_id, body, response, tentativa=2)

    if response.status_code >= 400:
        raise CercApiError(response.status_code, _safe_json(response))

    return response.json()


def criar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "C"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def atualizar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "A"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def inativar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "I"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def baixar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> list:
    item = {**payload, "tipoOperacao": "B"}
    return _request(financiador_id, "PUT", "/v15/contratos", [item], correlacao_id)


def consultar_contrato(financiador_id: str, payload: dict, correlacao_id: str) -> dict:
    return _request(financiador_id, "POST", "/contrato/consultar", payload, correlacao_id)
