"""Mapeia o `evento` do webhook CERC (tipoEvento=contrato, SPEC-02 §5.2)
para as escritas que o processador (Plano 11, apps/contratos/views.py)
precisa fazer em `contrato`, `garantia_ur` e `indicador_consistencia`.

Funções puras — nenhuma chamada a banco. O caller resolve `garantia_id`
(a partir da `referencia_externa_garantia` sintética que garantia_urs_do_evento
inclui em cada linha) e `contrato_id`, e executa as escritas.

Assunção documentada (não afirmada explicitamente na tabela compactada da
SPEC-02 §5.2, mas consistente com como a CERC correlaciona tudo nesse
payload — ver §4.2): cada item de `garantiasAlcancadas[]` carrega sua
própria `referenciaExterna`, igual à `garantias[].referenciaExterna`
enviada no request original (AP007B).
"""

from apps.contratos import state_machine


def atualizacoes_contrato_do_evento(evento: dict) -> dict:
    """§5.2: campos do evento que atualizam a linha de `contrato` quando
    status=0. Quando status=1 (falha), o evento não traz nenhum desses
    campos — o caller só atualiza `status` (via state_machine), nada mais."""
    if evento.get("status") != "0":
        return {}
    resultado = evento["resultadoDistribuicaoOnus"]
    return {
        "qtd_urs_alcancadas": evento.get("quantidadeUnidadesRecebiveisAlcancadas"),
        "valor_urs_alcancadas": evento.get("valorUnidadesRecebiveisAlcancadas"),
        "confirmado_em": evento.get("dataHoraProcessamento"),
        "resultado_distribuicao": resultado,
        "status_garantia": state_machine.sub_estado_garantia(resultado),
    }


def garantia_urs_do_evento(evento: dict, snapshot_em) -> list:
    """§5.2 `garantiasAlcancadas[].unidadesRecebiveisAlcancadas[]` -> linhas
    de `garantia_ur` (design §11, schema em sql/schema/01-contratos-schema.sql).
    `snapshot_em` deve ser determinístico (derivado do evento, não
    wall-clock) para que uma reentrega da CERC produza a mesma chave
    primária e o upsert do caller seja idempotente."""
    linhas = []
    for garantia in evento.get("garantiasAlcancadas", []) or []:
        referencia_externa_garantia = garantia.get("referenciaExterna")
        for ur in garantia.get("unidadesRecebiveisAlcancadas", []) or []:
            linhas.append({
                "referencia_externa_garantia": referencia_externa_garantia,
                "cnpj_credenciadora": ur.get("cnpjCredenciadora"),
                "documento_ufr": ur.get("documentoUsuarioFinalRecebedor"),
                "documento_titular": ur.get("documentoTitular"),
                "codigo_arranjo": ur.get("codigoArranjoPagamento"),
                "data_liquidacao": ur.get("dataLiquidacao"),
                "constituicao": ur.get("constituicao"),
                "valor_constituido_total": ur.get("valorConstituidoTotal"),
                "valor_bloqueado": ur.get("valorBloqueado"),
                "indicador_oneracao": ur.get("indicadorOneracao"),
                "regras_divisao": ur.get("regrasDivisao"),
                "valor_onerado": ur.get("valorOnerado"),
                "valor_constituido_efeito": ur.get("valorConstituidoEfeito"),
                "origem": "WEBHOOK",
                "snapshot_em": snapshot_em,
            })
    return linhas


def indicadores_do_evento(evento: dict, observado_em) -> list:
    """§5.2 `indicadoresConsistencia[]` -> linhas de `indicador_consistencia`
    (design §11). `observado_em` determinístico pelo mesmo motivo de
    `garantia_urs_do_evento`."""
    linhas = []
    for item in evento.get("indicadoresConsistencia", []) or []:
        linhas.append({
            "indicador": item.get("indicador"),
            "resultado": item.get("resultado"),
            "parametros": item.get("parametros", []),
            "criticidade": item.get("criticidade"),
            "observado_em": observado_em,
        })
    return linhas
