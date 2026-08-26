"""Escritas em `contrato`/`contrato_contrato_anterior`/`contrato_parcela`/
`contrato_domicilio`/`garantia` para o fluxo de criação (Plano 12).
`payload_validado` é exatamente o que
apps.contratos.contrato_validation_orquestrador.validar_criacao_contrato
retorna — valores monetários já Decimal, datas já date.

domicilio: SPEC-02 §4.2 carrega domicilioPagamento POR GARANTIA, mas o
schema (§11, sql/schema/01-contratos-schema.sql) tem contrato_domicilio
como UMA linha por CONTRATO — mismatch herdado da própria spec, não
inventado aqui. Grava o domicílio da PRIMEIRA garantia; ver Global
Constraints do Plano 12.
"""

import uuid
from datetime import datetime, timezone

from apps.contratos import state_machine
from shared.cloudsql_client import get_db


def buscar_contrato_por_referencia(financiador_id: str, referencia_externa: str) -> dict | None:
    resultado = get_db(financiador_id).table("contrato").select("*").eq("referencia_externa", referencia_externa).execute()
    return resultado.data[0] if resultado.data else None


def remover_contrato_rejeitado(financiador_id: str, contrato_id: str) -> None:
    """Descarta um contrato e TODO o seu grafo de sub-tabelas, em ordem
    segura para as FKs (filhos antes do pai).

    Só existe para o caminho de RESSUBMISSÃO de um contrato
    REJEITADO_ESTRUTURAL (apps/contratos/views.py::criar_contrato): aquele
    contrato nunca foi registrado na CERC, então a linha antiga é lixo — e
    precisa sumir ANTES da nova ser inserida, senão o UNIQUE
    (cnpj_participante, identificador_contrato) da tabela `contrato` estoura.

    NÃO re-checa o status: é responsabilidade de QUEM CHAMA garantir que o
    contrato está REJEITADO_ESTRUTURAL. Chamar isto para um contrato
    AGUARDANDO_WEBHOOK/REGISTRADO apagaria dado financeiro real.
    """
    db = get_db(financiador_id)

    for garantia in db.table("garantia").select("id").eq("contrato_id", contrato_id).execute().data:
        # Um contrato REJEITADO_ESTRUTURAL nunca chega a ter UR (URs só vêm por
        # webhook, e webhook só existe depois de AGUARDANDO_WEBHOOK). Apagado
        # defensivamente mesmo assim — o custo é um DELETE que não casa nada, e
        # o benefício é não depender dessa invariante para não estourar a FK.
        db.table("garantia_ur").delete().eq("garantia_id", garantia["id"]).execute()
    db.table("garantia").delete().eq("contrato_id", contrato_id).execute()

    db.table("contrato_domicilio").delete().eq("contrato_id", contrato_id).execute()
    db.table("contrato_parcela").delete().eq("contrato_id", contrato_id).execute()
    db.table("contrato_contrato_anterior").delete().eq("contrato_id", contrato_id).execute()
    # contrato_evento e indicador_consistencia também referenciam contrato(id).
    # contrato_evento em particular SEMPRE tem linha aqui: a própria rejeição
    # estrutural grava um evento tipo="rejeicao_estrutural" (views.py). Sem este
    # DELETE, o DELETE de `contrato` abaixo violaria a FK.
    db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
    db.table("indicador_consistencia").delete().eq("contrato_id", contrato_id).execute()

    db.table("contrato").delete().eq("id", contrato_id).execute()


def inserir_contrato_criado(
    financiador_id: str, payload_validado: dict, status: str, protocolo, id_contrato_cerc,
) -> dict:
    """Persiste o grafo completo de um contrato recém-submetido à CERC e
    devolve a linha final de `contrato` (já com `status` = o status pedido).

    Ordem deliberada (mesmo padrão de views.py::processar_webhook_contrato,
    que grava o UPDATE de `contrato` por ÚLTIMO): a linha de `contrato` entra
    primeiro — tudo o mais tem FK para contrato.id — mas com o status
    PLACEHOLDER state_machine.ENVIANDO ("submissão em voo, desfecho ainda não
    registrado localmente"); os filhos são gravados em seguida; e só então um
    UPDATE final grava o status REAL. Não há transação abrangendo a função
    inteira (cada .execute() abre a sua), então uma falha no meio deixaria,
    com a ordem antiga, um contrato com status definitivo e filhos faltando —
    que o guard de idempotência de criar_contrato trataria como "já existe",
    perdendo os filhos para sempre. Com esta ordem, a mesma falha deixa a
    linha visivelmente presa em ENVIANDO, diagnosticável por
    `SELECT * FROM contrato WHERE status = 'ENVIANDO'`.
    """
    db = get_db(financiador_id)
    contrato_id = str(uuid.uuid4())

    db.table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": payload_validado["referenciaExterna"],
        "identificador_contrato": payload_validado["identificadorContrato"],
        # protocolo/id da CERC já entram aqui (e são reescritos no UPDATE final)
        # de propósito: se o processo morrer no meio, a linha presa em ENVIANDO
        # ainda carrega o protocolo necessário para conciliar contra a CERC.
        "protocolo_cerc": protocolo,
        "id_contrato_cerc": id_contrato_cerc,
        "status": state_machine.ENVIANDO,
        "enviado_em": datetime.now(timezone.utc),
        "cnpj_participante": financiador_id,
        "documento_contratante": payload_validado["documentoContratante"],
        "cnpj_detentor": payload_validado["cnpjDetentor"],
        "tipo_efeito": payload_validado["tipoEfeito"],
        "modalidade_operacao": payload_validado["modalidadeOperacao"],
        "gestao_entidade_registradora": payload_validado["identificacaoGestaoEntidadeRegistradora"],
        "saldo_devedor": payload_validado["saldoDevedor"],
        "limite_operacao_garantida": payload_validado["limiteOperacaoGarantida"],
        "valor_mantido": payload_validado["valorMantido"],
        "data_assinatura": payload_validado["dataAssinatura"],
        "data_vencimento": payload_validado["dataVencimento"],
        "repactuacao": payload_validado.get("repactuacao") == "1",
        "carteira": payload_validado.get("carteira"),
        "tipo_avaliacao": payload_validado.get("tipoAvaliacao"),
        "taxa_juros": payload_validado.get("taxaJuros"),
        "indexador": payload_validado.get("indexador"),
    }).execute()

    # Um .insert(lista) grava a lista inteira numa única transação
    # (shared/cloudsql_client.py::_exec_insert), então cada tabela-filha vira um
    # commit só em vez de um por linha.
    anteriores = [
        {"contrato_id": contrato_id, "identificador_anterior": identificador_anterior}
        for identificador_anterior in payload_validado.get("identificacaoContratosAnteriores", []) or []
    ]
    if anteriores:
        db.table("contrato_contrato_anterior").insert(anteriores).execute()

    parcelas = [
        {"contrato_id": contrato_id, "vencimento": parcela["vencimento"], "valor": parcela["valor"]}
        for parcela in payload_validado.get("parcelas", []) or []
    ]
    if parcelas:
        db.table("contrato_parcela").insert(parcelas).execute()

    garantias = payload_validado.get("garantias", []) or []
    if garantias and garantias[0].get("domicilioPagamento"):
        domicilio = garantias[0]["domicilioPagamento"]
        db.table("contrato_domicilio").insert({
            "contrato_id": contrato_id,
            "numero_documento_titular": domicilio["numeroDocumentoTitular"],
            "nome_titular": domicilio.get("nomeTitular"),
            "tipo_conta": domicilio["tipoConta"],
            "compe": domicilio.get("compe"),
            "ispb": domicilio["ispb"],
            "agencia": domicilio.get("agencia"),
            "numero_conta": domicilio["numeroConta"],
        }).execute()

    linhas_garantia = []
    for g in garantias:
        definicao = g["definicaoUnidadeRecebivel"]
        linhas_garantia.append({
            "id": str(uuid.uuid4()),
            "contrato_id": contrato_id,
            "referencia_externa": g["referenciaExterna"],
            "regras_divisao": g["regrasDivisao"],
            "valor_a_onerar": g["valorAOnerar"],
            "tipo_distribuicao": g.get("tipoDistribuicao"),
            "def_lista_credenciadoras": definicao["listaCnpjCredenciadora"],
            "def_lista_arranjos": definicao["listaCodigoArranjoPagamento"],
            "def_documento_ufr": definicao.get("documentoUsuarioFinalRecebedor"),
            "def_documento_titular": definicao.get("documentoTitular"),
            "def_data_inicio": definicao["dataInicio"],
            "def_data_fim": definicao["dataFim"],
        })
    if linhas_garantia:
        db.table("garantia").insert(linhas_garantia).execute()

    # ÚLTIMA escrita: é ela que tira o contrato de ENVIANDO e o marca como
    # concluído. Devolve a linha JÁ com o status real, para que o retorno
    # público desta função continue idêntico ao de antes desta reordenação.
    atualizado = db.table("contrato").update({
        "status": status,
        "protocolo_cerc": protocolo,
        "id_contrato_cerc": id_contrato_cerc,
    }).eq("id", contrato_id).execute()

    return atualizado.data[0]
