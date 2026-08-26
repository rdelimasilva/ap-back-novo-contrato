import pytest

from apps.contratos import state_machine as sm


# ENVIANDO -> (207) -> AGUARDANDO_WEBHOOK | ATUALIZANDO | REJEITADO_ESTRUTURAL

def test_estado_apos_207_sucesso_criacao_vai_para_aguardando_webhook():
    assert sm.estado_apos_207(tipo_operacao="C", status_207="0") == sm.AGUARDANDO_WEBHOOK


def test_estado_apos_207_sucesso_atualizacao_vai_para_atualizando():
    assert sm.estado_apos_207(tipo_operacao="A", status_207="0") == sm.ATUALIZANDO


def test_estado_apos_207_erro_estrutural_vai_para_rejeitado_estrutural():
    assert sm.estado_apos_207(tipo_operacao="C", status_207="1") == sm.REJEITADO_ESTRUTURAL


def test_estado_apos_400_vai_para_rejeitado_estrutural():
    assert sm.estado_apos_400() == sm.REJEITADO_ESTRUTURAL


# AGUARDANDO_WEBHOOK / ATUALIZANDO -> (webhook) -> REGISTRADO | REJEITADO

def test_estado_apos_webhook_sucesso_de_aguardando_webhook_vai_para_registrado():
    assert sm.estado_apos_webhook(sm.AGUARDANDO_WEBHOOK, status_webhook="0") == sm.REGISTRADO


def test_estado_apos_webhook_sucesso_de_atualizando_vai_para_registrado():
    assert sm.estado_apos_webhook(sm.ATUALIZANDO, status_webhook="0") == sm.REGISTRADO


def test_estado_apos_webhook_falha_vai_para_rejeitado():
    assert sm.estado_apos_webhook(sm.AGUARDANDO_WEBHOOK, status_webhook="1") == sm.REJEITADO


def test_estado_apos_webhook_falha_de_atualizando_vai_para_rejeitado():
    assert sm.estado_apos_webhook(sm.ATUALIZANDO, status_webhook="1") == sm.REJEITADO


def test_estado_apos_webhook_de_estado_nao_esperado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_webhook(sm.REGISTRADO, status_webhook="0")


# INATIVANDO / BAIXANDO / RESILINDO_PARCIAL / RESILINDO_TOTAL -> (webhook) ->
# terminal específico (sucesso) | REGISTRADO (falha — a operação pós-registro
# não vingou, mas o contrato original continua registrado)

@pytest.mark.parametrize("estado_espera,terminal_sucesso", [
    (sm.INATIVANDO, sm.INATIVADO),
    (sm.BAIXANDO, sm.BAIXADO),
    (sm.RESILINDO_PARCIAL, sm.RESILIDO_PARCIAL),
    (sm.RESILINDO_TOTAL, sm.RESILIDO_TOTAL),
])
def test_estado_apos_webhook_sucesso_de_operacao_pos_registro_vai_para_terminal(estado_espera, terminal_sucesso):
    assert sm.estado_apos_webhook(estado_espera, status_webhook="0") == terminal_sucesso


@pytest.mark.parametrize("estado_espera", [
    sm.INATIVANDO, sm.BAIXANDO, sm.RESILINDO_PARCIAL, sm.RESILINDO_TOTAL,
])
def test_estado_apos_webhook_falha_de_operacao_pos_registro_volta_para_registrado(estado_espera):
    assert sm.estado_apos_webhook(estado_espera, status_webhook="1") == sm.REGISTRADO


# timeout SLA -> PENDENTE_CONCILIACAO (de qualquer estado de espera)

def test_estado_apos_timeout_sla_de_aguardando_webhook():
    assert sm.estado_apos_timeout_sla(sm.AGUARDANDO_WEBHOOK) == sm.PENDENTE_CONCILIACAO


def test_estado_apos_timeout_sla_de_atualizando():
    assert sm.estado_apos_timeout_sla(sm.ATUALIZANDO) == sm.PENDENTE_CONCILIACAO


@pytest.mark.parametrize("estado_espera", [
    sm.INATIVANDO, sm.BAIXANDO, sm.RESILINDO_PARCIAL, sm.RESILINDO_TOTAL,
])
def test_estado_apos_timeout_sla_de_operacao_pos_registro(estado_espera):
    assert sm.estado_apos_timeout_sla(estado_espera) == sm.PENDENTE_CONCILIACAO


def test_estado_apos_timeout_sla_de_estado_nao_esperado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_timeout_sla(sm.REGISTRADO)


# REGISTRADO -> (op I/B/P/R) -> estado de ESPERA (não mais terminal direto —
# mudança deste plano: a operação pós-registro também espera o webhook,
# igual à atualização)

@pytest.mark.parametrize("tipo_operacao,esperado", [
    ("I", "INATIVANDO"),
    ("B", "BAIXANDO"),
    ("P", "RESILINDO_PARCIAL"),
    ("R", "RESILINDO_TOTAL"),
])
def test_estado_apos_operacao_pos_registro(tipo_operacao, esperado):
    assert sm.estado_apos_operacao_pos_registro(sm.REGISTRADO, tipo_operacao) == esperado


def test_estado_apos_operacao_pos_registro_de_estado_nao_registrado_levanta_erro():
    with pytest.raises(sm.EstadoInvalidoError):
        sm.estado_apos_operacao_pos_registro(sm.AGUARDANDO_WEBHOOK, "I")


def test_estado_apos_operacao_pos_registro_com_operacao_invalida_levanta_erro():
    with pytest.raises(ValueError):
        sm.estado_apos_operacao_pos_registro(sm.REGISTRADO, "C")


# Sub-estado de garantia (resultadoDistribuicaoOnus)

@pytest.mark.parametrize("resultado,esperado", [
    ("0", "NAO_APLICAVEL"),
    ("1", "SUFICIENTE"),
    ("2", "INSUFICIENTE"),
    ("3", "EXCESSO"),
])
def test_sub_estado_garantia(resultado, esperado):
    assert sm.sub_estado_garantia(resultado) == esperado


def test_sub_estado_garantia_valor_invalido_levanta_erro():
    with pytest.raises(ValueError):
        sm.sub_estado_garantia("9")


# Regras de negócio derivadas (§5.2)

def test_eh_subgarantido_quando_insuficiente():
    assert sm.eh_subgarantido("2") is True


def test_eh_subgarantido_quando_suficiente_e_falso():
    assert sm.eh_subgarantido("1") is False


def test_eh_candidato_liberacao_excedente_quando_excesso():
    assert sm.eh_candidato_liberacao_excedente("3") is True


def test_eh_candidato_liberacao_excedente_quando_nao_excesso_e_falso():
    assert sm.eh_candidato_liberacao_excedente("1") is False


def test_ur_teve_insucesso_quando_indicador_zero():
    assert sm.ur_teve_insucesso("0") is True


def test_ur_teve_insucesso_quando_indicador_positivo_e_falso():
    assert sm.ur_teve_insucesso("1") is False


def test_indicador_critico_quando_criticidade_alta():
    assert sm.indicador_critico("2") is True
    assert sm.indicador_critico("3") is True


def test_indicador_critico_quando_criticidade_baixa_e_falso():
    assert sm.indicador_critico("0") is False
    assert sm.indicador_critico("1") is False
