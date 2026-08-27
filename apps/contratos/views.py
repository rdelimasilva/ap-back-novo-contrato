import base64
import hmac
import json
import logging
from datetime import date, datetime, timezone

from django.http import HttpResponseNotAllowed, JsonResponse
from django.views.decorators.http import require_GET, require_POST
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from apps.contratos import state_machine
from apps.contratos.contrato_repository import (
    atualizar_status_pos_registro,
    buscar_contrato_detalhado,
    buscar_contrato_por_referencia,
    inserir_contrato_criado,
    listar_contratos_do_financiador,
    remover_contrato_rejeitado,
)
from apps.contratos.contrato_validation_orquestrador import validar_criacao_contrato
from apps.contratos.validation import ValidationError
from apps.contratos.webhook_dedupe import hash_evento
from apps.contratos.webhook_processor import (
    atualizacoes_contrato_do_evento,
    garantia_urs_do_evento,
    indicadores_do_evento,
)
from services.cerc.client import (
    CercApiError,
    baixar_contrato as cerc_baixar_contrato,
    criar_contrato as cerc_criar_contrato,
    inativar_contrato as cerc_inativar_contrato,
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


def _to_float_or_none(val):
    """Converte valor (pode vir como string, Decimal, ou float) para float ou None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


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
        # get_db(financiador_id) dentro deste try de propósito: um financiador_id
        # desconhecido/config de tenant irresolvível é, do ponto de vista deste
        # endpoint, tão "esta mensagem de push não pode ser processada" quanto um
        # envelope malformado — deve virar 400, não um 500 genérico não tratado.
        db = get_db(financiador_id)
    except Exception:
        logger.exception("[Processor] Envelope do Pub/Sub push malformado ou financiador_id não resolvível")
        return JsonResponse({"erro": "envelope inválido"}, status=400)

    linhas_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_inbox_id).execute()
    if not linhas_inbox.data:
        # Condição permanentemente irrecuperável — esta linha nunca vai aparecer
        # depois, então continuar retentando via não-2xx não tem propósito (o
        # Pub/Sub reentregaria por dias até a retenção expirar e então descartar
        # silenciosamente). Confirma a entrega (204) para parar a reentrega.
        logger.error("[Processor] webhook_inbox_id=%s não encontrado (financiador=%s) — condição permanentemente irrecuperável, confirmando entrega", webhook_inbox_id, financiador_id)
        return JsonResponse({}, status=204)
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
        # Este caso genuinamente pode ser transitório (o fluxo de criação do
        # contrato que geraria esta linha pode ainda não ter rodado), então
        # mantemos o 500 (nova entrega do Pub/Sub tentará de novo) e NÃO tocamos
        # processado_em. Mas registramos uma pista de diagnóstico em `erro` para
        # que um operador consultando webhook_inbox WHERE erro IS NOT NULL veja
        # por que a linha está travada, em vez de precisar vasculhar logs.
        logger.error(
            "[Processor] contrato referencia_externa=%s não encontrado (financiador=%s) — deixando para nova entrega do Pub/Sub",
            referencia_externa, financiador_id,
        )
        db.table("webhook_inbox").update({
            "erro": f"contrato referencia_externa={referencia_externa} não encontrado",
        }).eq("id", webhook_inbox_id).execute()
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

    # A partir daqui, qualquer exceção inesperada (payload malformado da CERC
    # levando a um ValueError em state_machine, erro de tipo no banco, etc.)
    # é colocada em quarentena em vez de propagar como 500 — sem isso, o
    # Pub/Sub reentregaria a MESMA mensagem "poison pill" indefinidamente,
    # martelando o endpoint sem nenhuma chance de sucesso. EstadoInvalidoError
    # já foi tratado acima, antes de qualquer escrita, e não passa por aqui.
    try:
        atualizacoes = atualizacoes_contrato_do_evento(evento)
        if atualizacoes.get("confirmado_em"):
            atualizacoes["confirmado_em"] = datetime.fromisoformat(atualizacoes["confirmado_em"])
        atualizacoes["status"] = novo_status

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

        # UPDATE de `contrato` é a ÚLTIMA escrita de domínio de propósito (não a
        # primeira, como era antes): se o processo morrer entre as escritas
        # acima e esta aqui, uma reentrega do Pub/Sub ainda vê contrato.status
        # no valor ANTIGO (ex.: AGUARDANDO_WEBHOOK), então estado_apos_webhook
        # segue funcionando normalmente na nova tentativa, e os upserts de
        # garantia_ur/indicador_consistencia acima são idempotentes (chave
        # derivada do próprio evento, não wall-clock). Com a ordem antiga
        # (contrato primeiro), a mesma falha deixava o contrato marcado
        # REGISTRADO/REJEITADO permanentemente sem nenhuma UR/indicador/evento
        # de subgarantia — perda de dado silenciosa e permanente.
        db.table("contrato").update(atualizacoes).eq("id", contrato["id"]).execute()

        db.table("contrato_evento").insert({
            "contrato_id": contrato["id"], "tipo": "webhook_recebido",
            "payload": payload, "ocorrido_em": datetime.now(timezone.utc),
        }).execute()
    except Exception as exc:
        logger.exception(
            "[Processor] Falha inesperada processando webhook_inbox_id=%s (financiador=%s) — colocando em quarentena",
            webhook_inbox_id, financiador_id,
        )
        db.table("webhook_inbox").update({
            "processado_em": datetime.now(timezone.utc), "erro": repr(exc),
        }).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()

    return JsonResponse({}, status=204)


def _contrato_para_dto(c: dict) -> dict:
    """Linha crua de `contrato` (snake_case) -> DTO de resposta (camelCase).
    Compartilhado entre listar_contratos e detalhar_contrato — qualquer
    campo adicionado aqui aparece nos dois endpoints."""
    return {
        "id": c["id"],
        "referenciaExterna": c["referencia_externa"],
        "identificadorContrato": c["identificador_contrato"],
        "protocolo": c.get("protocolo_cerc"),
        "idContratoCerc": c.get("id_contrato_cerc"),
        "status": c["status"],
        "statusGarantia": c.get("status_garantia"),
        "cnpjParticipante": c["cnpj_participante"],
        "documentoContratante": c["documento_contratante"],
        "cnpjDetentor": c["cnpj_detentor"],
        "tipoEfeito": c["tipo_efeito"],
        "modalidadeOperacao": c["modalidade_operacao"],
        "gestaoEntidadeRegistradora": c["gestao_entidade_registradora"],
        "saldoDevedor": _to_float_or_none(c["saldo_devedor"]),
        "limiteOperacaoGarantida": _to_float_or_none(c["limite_operacao_garantida"]),
        "valorMantido": _to_float_or_none(c["valor_mantido"]),
        "dataAssinatura": c["data_assinatura"],
        "dataVencimento": c["data_vencimento"],
        "repactuacao": c["repactuacao"],
        "carteira": c.get("carteira"),
        "tipoAvaliacao": c.get("tipo_avaliacao"),
        "qtdUrsAlcancadas": c.get("qtd_urs_alcancadas"),
        "valorUrsAlcancadas": _to_float_or_none(c.get("valor_urs_alcancadas")),
        "resultadoDistribuicao": c.get("resultado_distribuicao"),
        "indSobrecolateral": _to_float_or_none(c.get("ind_sobrecolateral")),
        "criadoEm": c.get("enviado_em"),
        "confirmadoEm": c.get("confirmado_em"),
    }


def _ur_para_dto(ur: dict) -> dict:
    return {
        "cnpjCredenciadora": ur.get("cnpj_credenciadora"),
        "documentoUsuarioFinalRecebedor": ur.get("documento_ufr"),
        "documentoTitular": ur.get("documento_titular"),
        "codigoArranjoPagamento": ur.get("codigo_arranjo"),
        "dataLiquidacao": ur.get("data_liquidacao"),
        "constituicao": ur.get("constituicao"),
        "valorConstituidoTotal": _to_float_or_none(ur.get("valor_constituido_total")),
        "valorBloqueado": _to_float_or_none(ur.get("valor_bloqueado")),
        "indicadorOneracao": ur.get("indicador_oneracao"),
        "regrasDivisao": ur.get("regras_divisao"),
        "valorOnerado": _to_float_or_none(ur.get("valor_onerado")),
        "valorConstituidoEfeito": _to_float_or_none(ur.get("valor_constituido_efeito")),
        "origem": ur.get("origem"),
    }


def _garantia_para_dto(g: dict) -> dict:
    return {
        "id": g["id"],
        "referenciaExterna": g["referencia_externa"],
        "regrasDivisao": g["regras_divisao"],
        "valorAOnerar": _to_float_or_none(g["valor_a_onerar"]),
        "tipoDistribuicao": g.get("tipo_distribuicao"),
        "definicaoUnidadeRecebivel": {
            "listaCnpjCredenciadora": g["def_lista_credenciadoras"],
            "listaCodigoArranjoPagamento": g["def_lista_arranjos"],
            "documentoUsuarioFinalRecebedor": g.get("def_documento_ufr"),
            "documentoTitular": g.get("def_documento_titular"),
            "dataInicio": g["def_data_inicio"],
            "dataFim": g["def_data_fim"],
        },
        "unidadesRecebiveisAlcancadas": [_ur_para_dto(ur) for ur in g.get("unidades_recebiveis", [])],
    }


def _indicador_para_dto(i: dict) -> dict:
    return {
        "indicador": i["indicador"],
        "resultado": i.get("resultado"),
        "parametros": i.get("parametros"),
        "criticidade": i.get("criticidade"),
        "observadoEm": i.get("observado_em"),
    }


@require_GET
def detalhar_contrato(request, financiador_id: str, contrato_id: str):
    """GET /api/v1/contratos/<financiador_id>/<id> — detalhe de um
    contrato: dados do contrato + garantias (com URs alcançadas) +
    indicadores de consistência."""
    detalhe = buscar_contrato_detalhado(financiador_id, contrato_id)
    if detalhe is None:
        return JsonResponse({"erro": "contrato não encontrado"}, status=404)

    corpo = _contrato_para_dto(detalhe)
    corpo["garantias"] = [_garantia_para_dto(g) for g in detalhe["garantias"]]
    corpo["indicadoresConsistencia"] = [_indicador_para_dto(i) for i in detalhe["indicadores_consistencia"]]
    return JsonResponse(corpo)


def listar_contratos(request, financiador_id: str):
    """GET /api/v1/contratos/<financiador_id> — lista os contratos do
    financiador, mais recente primeiro. Filtros opcionais via querystring:
    ?status=, ?limit=."""
    status = request.GET.get("status") or None
    limit_param = request.GET.get("limit")
    limit = None
    if limit_param:
        try:
            limit = int(limit_param)
            if limit <= 0:
                return JsonResponse({"erro": "'limit' deve ser um inteiro positivo"}, status=400)
        except ValueError:
            return JsonResponse({"erro": "'limit' deve ser um inteiro positivo"}, status=400)
    contratos = listar_contratos_do_financiador(financiador_id, status=status, limit=limit)
    return JsonResponse({"dados": [_contrato_para_dto(c) for c in contratos]})


def contratos(request, financiador_id: str):
    """Dispatcher da URL de coleção `/contratos/<financiador_id>`: POST cria
    (tipoOperacao=C, comportamento existente de `criar_contrato`), GET lista.
    Mesma URL para os dois verbos — convenção REST de coleção."""
    if request.method == "POST":
        return criar_contrato(request, financiador_id)
    if request.method == "GET":
        return listar_contratos(request, financiador_id)
    return HttpResponseNotAllowed(["GET", "POST"])


def criar_contrato(request, financiador_id: str):
    """POST /api/v1/contratos/<financiador_id> — cria um contrato
    (tipoOperacao=C). Valida localmente (C01-C20 aplicáveis a criação),
    submete à CERC, interpreta o 207 e persiste o estado inicial. O
    resultado REAL do registro chega depois pelo webhook (Planos 10/11)
    — este endpoint responde 202 (aceito, processamento assíncrono), não
    201/200 (SPEC-02 §0)."""
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)

    # json.loads aceita qualquer valor JSON de topo (array, string, número,
    # null) — só um objeto tem .get()/campos. Sem este guard, um corpo `[]`
    # explodia com AttributeError (500) em vez de virar um 400 limpo. Mesmo
    # padrão do receptor de webhook acima.
    if not isinstance(payload, dict):
        return JsonResponse({"erro": "corpo deve ser um objeto JSON"}, status=400)

    referencia_externa = payload.get("referenciaExterna")
    # id do contrato REJEITADO_ESTRUTURAL que esta submissão substitui (se
    # houver) — o grafo antigo é descartado logo antes de persistir o novo.
    id_rejeitado_a_substituir = None
    if referencia_externa:
        existente = buscar_contrato_por_referencia(financiador_id, referencia_externa)
        if existente:
            if existente["status"] == state_machine.REJEITADO_ESTRUTURAL:
                # RESSUBMISSÃO, não replay: um REJEITADO_ESTRUTURAL nunca foi
                # registrado na CERC, então o chamador tem todo o direito de
                # corrigir o payload e tentar de novo com o MESMO
                # referenciaExterna. Curto-circuitar aqui (comportamento antigo)
                # deixava o contrato num beco sem saída permanente: a única saída
                # era um referenciaExterna NOVO — que, com o mesmo
                # identificadorContrato (correto: é a mesma operação), estourava
                # o UNIQUE (cnpj_participante, identificador_contrato) DEPOIS da
                # CERC já ter sido chamada de novo.
                id_rejeitado_a_substituir = existente["id"]
            else:
                # Qualquer outro status significa que a CERC aceitou/está
                # processando/registrou: replay idempotente, não chama a CERC
                # de novo.
                return JsonResponse({
                    "id": existente["id"], "status": existente["status"],
                    "referenciaExterna": referencia_externa,
                }, status=202)

    db = get_db(financiador_id)
    ativos = db.table("dominio_arranjo").select("codigo").eq("ativo", True).execute()
    ativos_arranjos = {row["codigo"] for row in ativos.data}

    try:
        payload_validado = validar_criacao_contrato(payload, hoje=date.today(), ativos_arranjos=ativos_arranjos)
    except ValidationError as erro:
        return JsonResponse({"codigo": erro.codigo, "erro": erro.mensagem}, status=422)
    except (KeyError, TypeError) as erro:
        return JsonResponse({"erro": f"campo obrigatório ausente ou mal formado: {erro}"}, status=400)

    payload_cerc = {**payload, "cnpjParticipante": financiador_id}
    try:
        resultado = cerc_criar_contrato(financiador_id, payload_cerc, correlacao_id=referencia_externa)
    except CercApiError:
        logger.exception("[CriarContrato] CERC respondeu erro (financiador=%s, referencia=%s)", financiador_id, referencia_externa)
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)
    except Exception:
        logger.exception("[CriarContrato] falha inesperada ao chamar a CERC (financiador=%s, referencia=%s)", financiador_id, referencia_externa)
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)

    # ---------------------------------------------------------------------
    # QUARENTENA: daqui pra frente a CERC JÁ RECEBEU e processou a submissão —
    # é dado financeiro real, já commitado do lado de lá. Qualquer exceção não
    # tratada neste trecho (resultado vazio -> IndexError, item sem "status" ->
    # KeyError, DBAPIError em qualquer escrita) significaria "submetido na CERC,
    # nenhum registro local, nenhum log utilizável". Mesmo espírito da
    # quarentena de processar_webhook_contrato acima — aqui não há linha de
    # webhook_inbox para marcar, então a rede de segurança é logar protocolo +
    # referência + financiador (o suficiente para conciliar manualmente) e
    # responder com um código de erro em vez de propagar.
    #
    # O escopo começa DEPOIS do except da chamada à CERC de propósito: a
    # ValidationError da validação local e as falhas de comunicação com a CERC
    # já têm os seus próprios 422/502 acima e NÃO passam por aqui.
    # ---------------------------------------------------------------------
    protocolo = None
    try:
        if not resultado:
            raise ValueError("resposta 207 da CERC não trouxe nenhum item")
        item = resultado[0]
        protocolo = item.get("protocolo") if isinstance(item, dict) else None
        novo_status = state_machine.estado_apos_207(tipo_operacao="C", status_207=item["status"])

        if id_rejeitado_a_substituir:
            # ANTES do insert, obrigatoriamente: a linha antiga ocupa o mesmo
            # (cnpj_participante, identificador_contrato) e o mesmo
            # referencia_externa que a nova vai usar.
            remover_contrato_rejeitado(financiador_id, id_rejeitado_a_substituir)

        contrato = inserir_contrato_criado(
            financiador_id, payload_validado, status=novo_status,
            protocolo=protocolo, id_contrato_cerc=item.get("idDoContrato"),
        )

        corpo = {
            "id": contrato["id"], "status": novo_status,
            "referenciaExterna": referencia_externa, "protocolo": protocolo,
        }
        if novo_status == state_machine.REJEITADO_ESTRUTURAL:
            # Devolve ao chamador POR QUE a CERC recusou (antes os erros eram
            # lidos e descartados) e registra a recusa na trilha de auditoria.
            corpo["erros"] = item.get("erros") or []
            db.table("contrato_evento").insert({
                "contrato_id": contrato["id"], "tipo": "rejeicao_estrutural",
                "payload": item, "ocorrido_em": datetime.now(timezone.utc),
            }).execute()

        status_http = 202 if novo_status != state_machine.REJEITADO_ESTRUTURAL else 422
        return JsonResponse(corpo, status=status_http)
    except Exception:
        logger.exception(
            "[CriarContrato] SUBMISSÃO JÁ ACEITA PELA CERC mas falhou ao interpretar/persistir "
            "localmente — CONCILIAR MANUALMENTE (financiador=%s, referencia=%s, protocolo=%s)",
            financiador_id, referencia_externa, protocolo,
        )
        return JsonResponse({
            "erro": "contrato submetido à CERC mas não persistido localmente; conciliação manual necessária",
            "referenciaExterna": referencia_externa, "protocolo": protocolo,
        }, status=500)


def _operacao_pos_registro(request, financiador_id: str, tipo_operacao: str, cerc_fn):
    """Núcleo comum de `inativar_contrato`/`baixar_contrato` (Plano 14) —
    `I`/`B` são espelhos exatos um do outro (mesma forma de payload, mesma
    máquina de estados), diferindo só em `tipo_operacao` e em qual função do
    cliente CERC chamar. Segue o mesmo padrão de quarentena pós-CERC de
    `criar_contrato` acima: uma vez que a CERC aceitou a submissão, qualquer
    falha ao interpretar/persistir localmente vira 500 logado com protocolo +
    referência (dado real já commitado do lado da CERC), nunca uma exceção
    não tratada."""
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"erro": "corpo deve ser um objeto JSON"}, status=400)

    referencia_externa = payload.get("referenciaExterna")
    if not referencia_externa:
        return JsonResponse({"codigo": "CAMPO_OBRIGATORIO", "erro": "'referenciaExterna' é obrigatório"}, status=422)

    contrato = buscar_contrato_por_referencia(financiador_id, referencia_externa)
    if contrato is None or contrato["cnpj_participante"] != financiador_id:
        # Trata "pertence a outro tenant" igual a "não encontrado" — defensivo:
        # buscar_contrato_por_referencia filtra só por referencia_externa,
        # nunca por cnpj_participante. Hoje o isolamento de tenant acontece no
        # nível de banco (um Cloud SQL por financiador_id via get_db), mas o
        # UNIQUE (cnpj_participante, identificador_contrato) do schema
        # antecipa mais de um cnpj_participante compartilhando um banco — se
        # isso um dia for verdade, esta checagem evita que uma operação
        # DESTRUTIVA (inativar/baixar) atinja o contrato de outro tenant.
        return JsonResponse({"erro": f"contrato referenciaExterna={referencia_externa} não encontrado"}, status=404)

    situacao = state_machine.situacao_operacao_pos_registro(contrato["status"], tipo_operacao)
    if situacao == "CONFLITO":
        return JsonResponse({
            "erro": f"contrato está em '{contrato['status']}', operação '{tipo_operacao}' não permitida a partir deste estado",
        }, status=409)
    if situacao == "REPLAY":
        return JsonResponse({
            "id": contrato["id"], "status": contrato["status"], "referenciaExterna": referencia_externa,
        }, status=202)

    # "PROSSEGUIR": campos-chave lidos do que JÁ ESTÁ PERSISTIDO, nunca do
    # corpo da requisição — identificadorContrato/documentoContratante são
    # imutáveis (SPEC-02 §2.1) e este contrato já foi criado pelo Plano 12;
    # decisão de arquitetura deste plano (ver seção "Architecture Decision").
    payload_cerc = {
        "identificadorContrato": contrato["identificador_contrato"],
        "referenciaExterna": referencia_externa,
        "documentoContratante": contrato["documento_contratante"],
        "cnpjParticipante": financiador_id,
    }
    try:
        # correlacao_id inclui tipo_operacao (Idempotency-Key da CERC) de
        # propósito: criar_contrato já usa referencia_externa sozinho como sua
        # própria correlacao_id, então uma criação (C) seguida de uma
        # inativação (I)/baixa (B) no MESMO contrato, com a mesma
        # referencia_externa, mandaria a MESMA Idempotency-Key com corpos
        # DIFERENTES — um comportamento de idempotência conforme na CERC
        # poderia devolver a resposta CACHEADA da criação em vez de processar
        # a operação nova. design doc §"Idempotency-Key/referenciaExterna
        # únicos nos POST mutantes".
        resultado = cerc_fn(financiador_id, payload_cerc, correlacao_id=f"{referencia_externa}:{tipo_operacao}")
    except CercApiError:
        logger.exception(
            "[OperacaoPosRegistro] CERC respondeu erro (financiador=%s, referencia=%s, operacao=%s)",
            financiador_id, referencia_externa, tipo_operacao,
        )
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)
    except Exception:
        logger.exception(
            "[OperacaoPosRegistro] falha inesperada ao chamar a CERC (financiador=%s, referencia=%s, operacao=%s)",
            financiador_id, referencia_externa, tipo_operacao,
        )
        return JsonResponse({"erro": "falha ao comunicar com a CERC"}, status=502)

    protocolo = None
    try:
        if not resultado:
            raise ValueError("resposta 207 da CERC não trouxe nenhum item")
        item = resultado[0]
        protocolo = item.get("protocolo") if isinstance(item, dict) else None
        novo_status = state_machine.estado_apos_207_pos_registro(tipo_operacao, item["status"])

        atualizado = atualizar_status_pos_registro(financiador_id, contrato["id"], novo_status, protocolo)

        get_db(financiador_id).table("contrato_evento").insert({
            "contrato_id": contrato["id"], "tipo": f"operacao_pos_registro_{tipo_operacao}",
            "payload": item, "ocorrido_em": datetime.now(timezone.utc),
        }).execute()

        corpo = {
            "id": atualizado["id"], "status": novo_status,
            "referenciaExterna": referencia_externa, "protocolo": protocolo,
        }
        if novo_status == state_machine.REGISTRADO:
            # A operação em si foi recusada estruturalmente (207 status=1) —
            # devolve por que, mesmo padrão de criar_contrato para
            # REJEITADO_ESTRUTURAL.
            corpo["erros"] = item.get("erros") or []

        status_http = 202 if novo_status != state_machine.REGISTRADO else 422
        return JsonResponse(corpo, status=status_http)
    except Exception:
        logger.exception(
            "[OperacaoPosRegistro] SUBMISSÃO JÁ ACEITA PELA CERC mas falhou ao interpretar/persistir "
            "localmente — CONCILIAR MANUALMENTE (financiador=%s, referencia=%s, operacao=%s, protocolo=%s)",
            financiador_id, referencia_externa, tipo_operacao, protocolo,
        )
        return JsonResponse({
            "erro": "operação submetida à CERC mas não persistida localmente; conciliação manual necessária",
            "referenciaExterna": referencia_externa, "protocolo": protocolo,
        }, status=500)


@require_POST
def inativar_contrato(request, financiador_id: str):
    """POST /api/v1/contratos/<financiador_id>/inativar — tipoOperacao=I."""
    return _operacao_pos_registro(request, financiador_id, "I", cerc_inativar_contrato)


@require_POST
def baixar_contrato(request, financiador_id: str):
    """POST /api/v1/contratos/<financiador_id>/baixar — tipoOperacao=B."""
    return _operacao_pos_registro(request, financiador_id, "B", cerc_baixar_contrato)
