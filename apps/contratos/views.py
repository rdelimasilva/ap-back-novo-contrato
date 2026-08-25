import base64
import hmac
import json
import logging
from datetime import datetime, timezone

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from apps.contratos import state_machine
from apps.contratos.webhook_dedupe import hash_evento
from apps.contratos.webhook_processor import (
    atualizacoes_contrato_do_evento,
    garantia_urs_do_evento,
    indicadores_do_evento,
)
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.pubsub_auth import verificar_push_oidc
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
    except Exception:
        # Qualquer falha ao resolver a config do tenant — env var ausente em
        # dev (RuntimeError), NotFound/PermissionDenied/InvalidArgument do
        # Secret Manager em produção, ou qualquer outra causa — significa
        # "essas credenciais não autenticam aqui". Trata como credencial
        # inválida, não como 404/500 (não vazamos se o tenant existe nem
        # detalhes da falha subjacente ao chamador).
        return False

    usuario_esperado = config.get("webhook_basic_user")
    senha_esperada = config.get("webhook_basic_password")
    if not usuario_esperado or not senha_esperada:
        # Config do tenant com credenciais vazias (ex.: placeholder do
        # .env.example nunca substituído) — nunca trata como "autentica
        # qualquer um". Loga para tornar o tenant mal configurado visível
        # em produção, mas não vaza isso na resposta ao chamador externo.
        logger.error("[Webhook] Credenciais Basic não configuradas para o tenant %s", financiador_id)
        return False

    # bytes, não str: hmac.compare_digest recusa comparar str com
    # caracteres não-ASCII (levanta TypeError) — usuario/senha vêm de
    # base64.b64decode(...).decode("utf-8") sobre entrada controlada pelo
    # chamador externo, então qualquer credencial UTF-8 válida porém não-ASCII
    # precisa continuar caindo em 401, não num 500 não tratado.
    ok_usuario = hmac.compare_digest(usuario.encode("utf-8"), usuario_esperado.encode("utf-8"))
    ok_senha = hmac.compare_digest(senha.encode("utf-8"), senha_esperada.encode("utf-8"))
    return ok_usuario and ok_senha


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


@require_POST
def processar_webhook_contrato(request):
    """Consumidor da push subscription do Pub/Sub — aplica a máquina de
    estados (§8) sobre o evento já persistido em webhook_inbox pelo
    receptor (Plano 10). Verificado por OIDC (design §6). Idempotente sob
    reentrega do Pub/Sub (at-least-once): o guard de
    webhook_inbox.processado_em evita refazer qualquer escrita."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    try:
        envelope = json.loads(request.body)
        dados = json.loads(base64.b64decode(envelope["message"]["data"]))
        webhook_inbox_id = dados["webhook_inbox_id"]
        financiador_id = dados["financiador_id"]
    except Exception:
        logger.exception("[Processor] Envelope do Pub/Sub push malformado")
        return JsonResponse({"erro": "envelope inválido"}, status=400)

    db = get_db(financiador_id)
    linhas_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_inbox_id).execute()
    if not linhas_inbox.data:
        logger.error("[Processor] webhook_inbox_id=%s não encontrado (financiador=%s)", webhook_inbox_id, financiador_id)
        return JsonResponse({"erro": "webhook_inbox não encontrado"}, status=404)
    inbox = linhas_inbox.data[0]

    if inbox["processado_em"] is not None:
        return JsonResponse({}, status=204)

    payload = inbox["payload"]
    tipo_evento = payload.get("tipoEvento")
    evento = payload.get("evento")

    if tipo_evento != "contrato":
        logger.warning("[Processor] tipoEvento=%s fora do escopo deste consumidor, ignorando", tipo_evento)
        db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    referencia_externa = evento.get("referenciaExterna")
    contratos = db.table("contrato").select("*").eq("referencia_externa", referencia_externa).execute()
    if not contratos.data:
        logger.error(
            "[Processor] contrato referencia_externa=%s não encontrado (financiador=%s) — deixando para nova entrega do Pub/Sub",
            referencia_externa, financiador_id,
        )
        return JsonResponse({"erro": "contrato não encontrado"}, status=500)
    contrato = contratos.data[0]

    try:
        novo_status = state_machine.estado_apos_webhook(contrato["status"], evento["status"])
    except state_machine.EstadoInvalidoError:
        logger.warning(
            "[Processor] webhook para contrato %s chegou em estado inesperado (%s) — tratando como já processado",
            contrato["id"], contrato["status"],
        )
        db.table("webhook_inbox").update({
            "processado_em": datetime.now(timezone.utc), "erro": "estado inválido para webhook",
        }).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    atualizacoes = atualizacoes_contrato_do_evento(evento)
    if atualizacoes.get("confirmado_em"):
        atualizacoes["confirmado_em"] = datetime.fromisoformat(atualizacoes["confirmado_em"])
    atualizacoes["status"] = novo_status
    db.table("contrato").update(atualizacoes).eq("id", contrato["id"]).execute()

    if evento.get("status") == "0":
        data_processamento = evento.get("dataHoraProcessamento")
        snapshot_em = datetime.fromisoformat(data_processamento) if data_processamento else datetime.now(timezone.utc)

        for ur in garantia_urs_do_evento(evento, snapshot_em):
            referencia_garantia = ur.pop("referencia_externa_garantia")
            garantias = (
                db.table("garantia").select("id")
                .eq("contrato_id", contrato["id"]).eq("referencia_externa", referencia_garantia)
                .execute()
            )
            if not garantias.data:
                logger.warning(
                    "[Processor] garantia referencia_externa=%s não encontrada no contrato %s — UR ignorada",
                    referencia_garantia, contrato["id"],
                )
                continue
            ur["garantia_id"] = garantias.data[0]["id"]
            db.table("garantia_ur").upsert(
                ur,
                # NOTA (desvio do brief): a PK original de garantia_ur foi substituída
                # em sql/schema/02-contratos-schema-fixes.sql por um id BIGSERIAL + um
                # índice único FUNCIONAL (garantia_ur_natural_key) que envolve
                # documento_ufr/documento_titular em COALESCE(..., '') — necessário
                # porque a SPEC-02 §4.4 marca os dois como opcionais e a PK antiga os
                # tornava implicitamente NOT NULL. O Postgres só infere um índice de
                # arbitragem para ON CONFLICT quando a lista de colunas bate
                # exatamente com as expressões do índice (erro 42P10 confirmado
                # contra o Cloud SQL real deste tenant ao usar os nomes de coluna
                # nus, como o brief especificava) — por isso replicamos aqui as
                # mesmas expressões COALESCE do índice.
                on_conflict=(
                    "garantia_id, cnpj_credenciadora, COALESCE(documento_ufr, ''), "
                    "COALESCE(documento_titular, ''), codigo_arranjo, data_liquidacao, origem"
                ),
            ).execute()

        for indicador in indicadores_do_evento(evento, snapshot_em):
            indicador["contrato_id"] = contrato["id"]
            db.table("indicador_consistencia").upsert(
                indicador, on_conflict="contrato_id, indicador, observado_em",
            ).execute()

        if state_machine.eh_subgarantido(evento.get("resultadoDistribuicaoOnus")):
            db.table("contrato_evento").insert({
                "contrato_id": contrato["id"], "tipo": "ContratoSubgarantido",
                "payload": evento, "ocorrido_em": snapshot_em,
            }).execute()

    db.table("contrato_evento").insert({
        "contrato_id": contrato["id"], "tipo": "webhook_recebido",
        "payload": payload, "ocorrido_em": datetime.now(timezone.utc),
    }).execute()

    db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()

    return JsonResponse({}, status=204)
