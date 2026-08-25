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

from shared.cloudsql_client import get_db


def buscar_contrato_por_referencia(financiador_id: str, referencia_externa: str) -> dict | None:
    resultado = get_db(financiador_id).table("contrato").select("*").eq("referencia_externa", referencia_externa).execute()
    return resultado.data[0] if resultado.data else None


def inserir_contrato_criado(
    financiador_id: str, payload_validado: dict, status: str, protocolo, id_contrato_cerc,
) -> dict:
    db = get_db(financiador_id)
    contrato_id = str(uuid.uuid4())

    inserido = db.table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": payload_validado["referenciaExterna"],
        "identificador_contrato": payload_validado["identificadorContrato"],
        "protocolo_cerc": protocolo,
        "id_contrato_cerc": id_contrato_cerc,
        "status": status,
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
    contrato = inserido.data[0]

    for identificador_anterior in payload_validado.get("identificacaoContratosAnteriores", []) or []:
        db.table("contrato_contrato_anterior").insert({
            "contrato_id": contrato_id, "identificador_anterior": identificador_anterior,
        }).execute()

    for parcela in payload_validado.get("parcelas", []) or []:
        db.table("contrato_parcela").insert({
            "contrato_id": contrato_id, "vencimento": parcela["vencimento"], "valor": parcela["valor"],
        }).execute()

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

    for g in garantias:
        definicao = g["definicaoUnidadeRecebivel"]
        db.table("garantia").insert({
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
        }).execute()

    return contrato
