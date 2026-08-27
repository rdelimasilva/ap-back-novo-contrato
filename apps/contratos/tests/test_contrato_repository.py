from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest

from apps.contratos import state_machine
from apps.contratos.contrato_repository import (
    atualizar_status_pos_registro,
    buscar_contrato_por_referencia,
    inserir_contrato_criado,
    remover_contrato_rejeitado,
)
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"


def _payload_validado(referencia_externa="CTR-TESTE-REPO-1", com_domicilio=True, com_anteriores=False, com_parcelas=True):
    garantia = {
        "referenciaExterna": f"{referencia_externa}-G1",
        "regrasDivisao": "1",
        "valorAOnerar": Decimal("180000.00"),
        "tipoDistribuicao": "padrao_pro_rata_ap",
        "definicaoUnidadeRecebivel": {
            "listaCnpjCredenciadora": ["99T"],
            "listaCodigoArranjoPagamento": ["VCC", "MCC"],
            "documentoUsuarioFinalRecebedor": "22751826000125",
            "documentoTitular": "22751826000125",
            "dataInicio": date(2026, 8, 26),
            "dataFim": date(2027, 8, 15),
        },
    }
    if com_domicilio:
        garantia["domicilioPagamento"] = {
            "numeroDocumentoTitular": "12345678000199",
            "nomeTitular": "Titular Teste",
            "tipoConta": "CC",
            "compe": "341",
            "ispb": "60701190",
            "agencia": "0001",
            "numeroConta": "464561-6",
        }
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-REPO",
        "documentoContratante": "22751826000125",
        "repactuacao": "0",
        "identificacaoContratosAnteriores": ["OP-ANTERIOR-1"] if com_anteriores else [],
        "cnpjDetentor": FINANCIADOR_TESTE,
        "tipoEfeito": "2",
        "saldoDevedor": Decimal("150000.00"),
        "limiteOperacaoGarantida": Decimal("200000.00"),
        "valorMantido": Decimal("180000.00"),
        "dataAssinatura": date(2026, 8, 15),
        "dataVencimento": date(2027, 8, 15),
        "identificacaoGestaoEntidadeRegistradora": "1",
        "modalidadeOperacao": "2",
        "parcelas": [{"vencimento": date(2026, 9, 15), "valor": Decimal("12500.00")}] if com_parcelas else [],
        "carteira": "CARTEIRA-01",
        "tipoAvaliacao": "avaliacao_contrato_basica_ap",
        "garantias": [garantia],
    }


def _limpar(referencia_externa):
    db = get_db(FINANCIADOR_TESTE)
    existente = db.table("contrato").select("id").eq("referencia_externa", referencia_externa).execute()
    for row in existente.data:
        contrato_id = row["id"]
        garantias = db.table("garantia").select("id").eq("contrato_id", contrato_id).execute()
        for g in garantias.data:
            db.table("garantia_ur").delete().eq("garantia_id", g["id"]).execute()
        db.table("garantia").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_domicilio").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_parcela").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_contrato_anterior").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato").delete().eq("id", contrato_id).execute()


def test_buscar_contrato_por_referencia_inexistente_retorna_none():
    assert buscar_contrato_por_referencia(FINANCIADOR_TESTE, "CTR-TESTE-REPO-INEXISTENTE") is None


def test_inserir_contrato_criado_grava_todas_as_subtabelas():
    referencia_externa = "CTR-TESTE-REPO-COMPLETO"
    _limpar(referencia_externa)
    try:
        payload = _payload_validado(referencia_externa, com_anteriores=True, com_parcelas=True)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="AGUARDANDO_WEBHOOK", protocolo="proto-1", id_contrato_cerc="cerc-1",
        )

        assert contrato["referencia_externa"] == referencia_externa
        assert contrato["status"] == "AGUARDANDO_WEBHOOK"
        assert contrato["protocolo_cerc"] == "proto-1"
        assert contrato["cnpj_participante"] == FINANCIADOR_TESTE
        assert contrato["saldo_devedor"] == Decimal("150000.00")

        db = get_db(FINANCIADOR_TESTE)
        anteriores = db.table("contrato_contrato_anterior").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(anteriores.data) == 1
        assert anteriores.data[0]["identificador_anterior"] == "OP-ANTERIOR-1"

        parcelas = db.table("contrato_parcela").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(parcelas.data) == 1
        assert parcelas.data[0]["valor"] == Decimal("12500.00")

        domicilio = db.table("contrato_domicilio").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(domicilio.data) == 1
        assert domicilio.data[0]["ispb"] == "60701190"

        garantias = db.table("garantia").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(garantias.data) == 1
        assert garantias.data[0]["referencia_externa"] == f"{referencia_externa}-G1"
        assert garantias.data[0]["def_lista_arranjos"] == ["VCC", "MCC"]

        encontrado = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert encontrado["id"] == contrato["id"]
    finally:
        _limpar(referencia_externa)


def test_inserir_contrato_criado_sem_anteriores_nem_parcelas_nao_gera_linhas():
    referencia_externa = "CTR-TESTE-REPO-MINIMO"
    _limpar(referencia_externa)
    try:
        payload = _payload_validado(referencia_externa, com_anteriores=False, com_parcelas=False)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="AGUARDANDO_WEBHOOK", protocolo="proto-2", id_contrato_cerc=None,
        )

        db = get_db(FINANCIADOR_TESTE)
        anteriores = db.table("contrato_contrato_anterior").select("*").eq("contrato_id", contrato["id"]).execute()
        assert anteriores.data == []
        parcelas = db.table("contrato_parcela").select("*").eq("contrato_id", contrato["id"]).execute()
        assert parcelas.data == []
    finally:
        _limpar(referencia_externa)


def test_inserir_contrato_criado_garantia_sem_domicilio_nao_gera_linha_de_domicilio():
    referencia_externa = "CTR-TESTE-REPO-SEM-DOMICILIO"
    _limpar(referencia_externa)
    try:
        payload = _payload_validado(referencia_externa, com_domicilio=False)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="AGUARDANDO_WEBHOOK", protocolo="proto-3", id_contrato_cerc=None,
        )

        db = get_db(FINANCIADOR_TESTE)
        domicilio = db.table("contrato_domicilio").select("*").eq("contrato_id", contrato["id"]).execute()
        assert domicilio.data == []

        garantias = db.table("garantia").select("*").eq("contrato_id", contrato["id"]).execute()
        assert len(garantias.data) == 1
        assert garantias.data[0]["referencia_externa"] == f"{referencia_externa}-G1"
    finally:
        _limpar(referencia_externa)


def test_inserir_contrato_criado_grava_enviado_em():
    # Revisão final, achado 6: enviado_em nunca era escrito em lugar nenhum —
    # sem ele, um futuro job de conciliação de SLA não tem como medir há quanto
    # tempo o contrato espera pelo webhook.
    referencia_externa = "CTR-TESTE-REPO-ENVIADO-EM"
    _limpar(referencia_externa)
    try:
        antes = datetime.now(timezone.utc)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, _payload_validado(referencia_externa),
            status="AGUARDANDO_WEBHOOK", protocolo="proto-enviado-em", id_contrato_cerc=None,
        )

        assert contrato["enviado_em"] is not None
        depois = datetime.now(timezone.utc)
        assert antes - timedelta(minutes=5) <= contrato["enviado_em"] <= depois + timedelta(minutes=5)
    finally:
        _limpar(referencia_externa)


@pytest.mark.parametrize("status_pedido", ["AGUARDANDO_WEBHOOK", "REJEITADO_ESTRUTURAL"])
def test_inserir_contrato_criado_nunca_deixa_o_contrato_preso_em_enviando(status_pedido):
    # Revisão final, achado 5: a função agora insere `contrato` com o status
    # PLACEHOLDER ENVIANDO, grava os filhos, e só então faz o UPDATE final para
    # o status real. ENVIANDO só pode ser observável se o processo morrer no
    # meio — uma chamada bem-sucedida tem que terminar exatamente no status
    # pedido, tanto na linha persistida quanto no valor de retorno.
    referencia_externa = "CTR-TESTE-REPO-SEM-ENVIANDO"
    _limpar(referencia_externa)
    try:
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, _payload_validado(referencia_externa),
            status=status_pedido, protocolo="proto-enviando", id_contrato_cerc="cerc-enviando",
        )

        assert contrato["status"] == status_pedido
        assert contrato["status"] != state_machine.ENVIANDO
        assert contrato["protocolo_cerc"] == "proto-enviando"
        assert contrato["id_contrato_cerc"] == "cerc-enviando"

        persistido = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        assert persistido["status"] == status_pedido
    finally:
        _limpar(referencia_externa)


def test_remover_contrato_rejeitado_apaga_o_grafo_inteiro():
    # Revisão final, achados 1/2: o caminho de ressubmissão de um contrato
    # REJEITADO_ESTRUTURAL depende disto para apagar a linha antiga ANTES de
    # inserir a nova — senão o UNIQUE (cnpj_participante, identificador_contrato)
    # estoura. Inclui uma linha de contrato_evento de propósito: ela também
    # referencia contrato(id) e bloquearia o DELETE se não fosse removida.
    referencia_externa = "CTR-TESTE-REPO-REMOVER"
    _limpar(referencia_externa)
    try:
        payload = _payload_validado(referencia_externa, com_anteriores=True, com_parcelas=True)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="REJEITADO_ESTRUTURAL",
            protocolo="proto-remover", id_contrato_cerc=None,
        )
        contrato_id = contrato["id"]

        db = get_db(FINANCIADOR_TESTE)
        db.table("contrato_evento").insert({
            "contrato_id": contrato_id, "tipo": "rejeicao_estrutural",
            "payload": {"status": "1", "erros": [{"codigo": "107501"}]},
            "ocorrido_em": datetime.now(timezone.utc),
        }).execute()

        remover_contrato_rejeitado(FINANCIADOR_TESTE, contrato_id)

        assert buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa) is None
        for tabela in (
            "garantia", "contrato_domicilio", "contrato_parcela",
            "contrato_contrato_anterior", "contrato_evento",
        ):
            assert db.table(tabela).select("*").eq("contrato_id", contrato_id).execute().data == [], tabela
    finally:
        _limpar(referencia_externa)


def test_remover_contrato_rejeitado_libera_o_unique_de_identificador_contrato():
    # A prova direta do achado 1: depois de remover o rejeitado, um contrato
    # NOVO com o MESMO identificadorContrato (é a mesma operação sendo
    # retentada) entra sem violar UNIQUE (cnpj_participante, identificador_contrato).
    referencia_externa = "CTR-TESTE-REPO-REUSO-IDENT"
    _limpar(referencia_externa)
    try:
        payload = _payload_validado(referencia_externa)
        payload["identificadorContrato"] = "OP-TESTE-REPO-REUSO"
        rejeitado = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="REJEITADO_ESTRUTURAL",
            protocolo="proto-reuso-1", id_contrato_cerc=None,
        )

        remover_contrato_rejeitado(FINANCIADOR_TESTE, rejeitado["id"])

        novo = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload, status="AGUARDANDO_WEBHOOK",
            protocolo="proto-reuso-2", id_contrato_cerc="cerc-reuso-2",
        )

        assert novo["id"] != rejeitado["id"]
        assert novo["identificador_contrato"] == "OP-TESTE-REPO-REUSO"
        assert novo["referencia_externa"] == referencia_externa
        assert novo["status"] == "AGUARDANDO_WEBHOOK"
    finally:
        _limpar(referencia_externa)


def _inserir_contrato_registrado_minimo(referencia_externa: str, identificador_contrato: str = "OP-TESTE-POS-REGISTRO") -> dict:
    """Insere uma linha `contrato` mínima, já REGISTRADA, direto na tabela —
    sem passar por `inserir_contrato_criado` (que exige um payload_validado
    completo, com garantias/parcelas, irrelevante para testar uma função que
    só faz UPDATE de duas colunas)."""
    db = get_db(FINANCIADOR_TESTE)
    contrato_id = str(uuid.uuid4())
    db.table("contrato").insert({
        "id": contrato_id,
        "referencia_externa": referencia_externa,
        "identificador_contrato": identificador_contrato,
        "protocolo_cerc": "proto-original",
        "status": "REGISTRADO",
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


def test_atualizar_status_pos_registro_grava_novo_status_e_protocolo():
    referencia_externa = "CTR-TESTE-REPO-POS-REGISTRO-1"
    contrato = _inserir_contrato_registrado_minimo(referencia_externa)
    try:
        atualizado = atualizar_status_pos_registro(
            FINANCIADOR_TESTE, contrato["id"], novo_status="INATIVANDO", protocolo="proto-novo",
        )
        assert atualizado["status"] == "INATIVANDO"
        assert atualizado["protocolo_cerc"] == "proto-novo"
        # nenhuma outra coluna foi tocada
        assert atualizado["identificador_contrato"] == contrato["identificador_contrato"]
        assert atualizado["documento_contratante"] == contrato["documento_contratante"]
    finally:
        get_db(FINANCIADOR_TESTE).table("contrato").delete().eq("id", contrato["id"]).execute()
