import base64
import json
import logging
from datetime import datetime

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from apps.contratos.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)


def health(request):
    return JsonResponse({"status": "ok"})


def _violacao_unique(erro: DBAPIError) -> bool:
    """True se o DBAPIError é uma violação de constraint UNIQUE (sqlstate
    23505). Necessário porque o driver pg8000 não popula os subtipos de
    exceção da PEP 249 (IntegrityError etc.) por sqlstate — toda falha do
    protocolo chega como um DatabaseError genérico (visto e confirmado
    contra o Cloud SQL real deste tenant); o único jeito confiável de
    distinguir "duplicado" de "outro erro de banco" é inspecionar o
    dicionário de campos da mensagem de erro do Postgres em `orig.args[0]`."""
    args = getattr(erro.orig, "args", None)
    return bool(args) and isinstance(args[0], dict) and args[0].get("C") == "23505"


def _autenticado(request, financiador_id: str) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return False
    try:
        decodificado = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        # Header vindo de fora (a CERC ou qualquer chamador não confiável) —
        # base64/utf-8 inválido é "não autenticado", não um bug nosso.
        return False
    usuario, _, senha = decodificado.partition(":")

    try:
        config = get_tenant_config(financiador_id)
    except RuntimeError:
        # financiador_id sem segredo configurado — trata como credencial
        # inválida, não como 404/500 (não vazamos se o tenant existe).
        return False
    return usuario == config.get("webhook_basic_user") and senha == config.get("webhook_basic_password")


@require_POST
def webhook_contrato(request, financiador_id: str):
    """Receptor do webhook CERC (tipoEvento=contrato) — SPEC-02 §5.2/§5.3.

    Fino por design: autentica, grava em webhook_inbox, publica no
    Pub/Sub e responde. Nenhuma transição de estado acontece aqui — isso é
    do consumidor (Plano 11), que lê o Pub/Sub e importa state_machine.
    """
    if not _autenticado(request, financiador_id):
        return JsonResponse({"erro": "autenticação inválida"}, status=401)

    try:
        envelope = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)

    tipo_evento = envelope.get("tipoEvento") if isinstance(envelope, dict) else None
    data_hora_evento = envelope.get("dataHoraEvento") if isinstance(envelope, dict) else None
    evento = envelope.get("evento") if isinstance(envelope, dict) else None
    if not tipo_evento or not data_hora_evento or evento is None:
        return JsonResponse(
            {"erro": "envelope inválido: tipoEvento, dataHoraEvento e evento são obrigatórios"}, status=400,
        )

    hash_dedupe = hash_evento(tipo_evento, evento, data_hora_evento)
    webhook_id = str(ULID())

    try:
        get_db(financiador_id).table("webhook_inbox").insert({
            "id": webhook_id,
            "tipo_evento": tipo_evento,
            "data_hora_evento": datetime.fromisoformat(data_hora_evento),
            "payload": envelope,
            "hash_dedupe": hash_dedupe,
        }).execute()
    except DBAPIError as erro:
        if _violacao_unique(erro):
            # UNIQUE(hash_dedupe) — reentrega da CERC do mesmo evento. Já está
            # persistido de uma entrega anterior: inofensivo, responde 2xx.
            logger.info("[Webhook] Evento duplicado ignorado (financiador=%s, hash=%s)", financiador_id, hash_dedupe)
            return JsonResponse({}, status=202)
        logger.exception("[Webhook] Falha ao persistir webhook_inbox (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "falha ao persistir evento"}, status=500)
    except Exception:
        # Não conseguimos persistir por um motivo que NÃO é duplicidade —
        # o evento não está seguro. Responde 5xx de propósito para que a
        # CERC use uma de suas (até 5) tentativas de reentrega.
        logger.exception("[Webhook] Falha ao persistir webhook_inbox (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "falha ao persistir evento"}, status=500)

    try:
        pubsub_client.publish_webhook_contrato(webhook_id, financiador_id)
    except Exception:
        # publish_webhook_contrato já é melhor-esforço e não deveria levantar;
        # se mesmo assim levantar, a linha já está persistida — não perdemos
        # o evento, só o atraso de processamento (o Plano 11 varre por
        # processado_em IS NULL). Loga e responde sucesso normalmente.
        logger.exception("[Webhook] publish_webhook_contrato levantou inesperadamente (financiador=%s)", financiador_id)

    return JsonResponse({}, status=202)
