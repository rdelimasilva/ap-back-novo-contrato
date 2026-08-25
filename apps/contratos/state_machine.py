"""Máquina de estados do contrato — SPEC-02 §8, regras derivadas em §5.2.

Funções puras — nenhuma chamada a banco/CERC. Quem persiste uma transição
(webhook receiver, job de reconciliação, views internas) chama estas
funções primeiro e só então grava o resultado; este módulo nunca toca
`contrato`/`garantia_ur`/`cerc_requisicao` diretamente.

Diagrama (§8):

    PUT /v15/contratos (C)
              |
              v
        ENVIANDO
    +---------+----------+
207 status=0        207 status=1 / 400
    |                     |
    v                     v
AGUARDANDO_WEBHOOK   REJEITADO_ESTRUTURAL
    |
+-----------+----------------+---------------------+
webhook    webhook       timeout SLA          (op = A)
status=0   status=1      (sem webhook)             |
    |           |              |                    v
    v           v              v              ATUALIZANDO --> REGISTRADO
REGISTRADO  REJEITADO   PENDENTE_CONCILIACAO
    |
    +-- op I --> INATIVADO
    +-- op B --> BAIXADO
    +-- op P --> RESILIDO_PARCIAL
    +-- op R --> RESILIDO_TOTAL

O SLA em si (30min pra PENDENTE_CONCILIACAO, alerta após 2h) não é
responsabilidade deste módulo — ele só responde "qual o próximo estado
quando um timeout acontece", não "quanto tempo já passou". Isso é do
job de reconciliação (Plano 11), que é quem sabe a hora real.
"""

ENVIANDO = "ENVIANDO"
AGUARDANDO_WEBHOOK = "AGUARDANDO_WEBHOOK"
REJEITADO_ESTRUTURAL = "REJEITADO_ESTRUTURAL"
REGISTRADO = "REGISTRADO"
REJEITADO = "REJEITADO"
PENDENTE_CONCILIACAO = "PENDENTE_CONCILIACAO"
ATUALIZANDO = "ATUALIZANDO"
INATIVADO = "INATIVADO"
BAIXADO = "BAIXADO"
RESILIDO_PARCIAL = "RESILIDO_PARCIAL"
RESILIDO_TOTAL = "RESILIDO_TOTAL"

ESTADOS_TERMINAIS = {
    REJEITADO_ESTRUTURAL, REJEITADO, INATIVADO, BAIXADO, RESILIDO_PARCIAL, RESILIDO_TOTAL,
}

NAO_APLICAVEL = "NAO_APLICAVEL"
SUFICIENTE = "SUFICIENTE"
INSUFICIENTE = "INSUFICIENTE"
EXCESSO = "EXCESSO"


class EstadoInvalidoError(Exception):
    def __init__(self, estado_atual: str, transicao: str):
        self.estado_atual = estado_atual
        self.transicao = transicao
        super().__init__(f"transição '{transicao}' inválida a partir de '{estado_atual}'")


def estado_apos_207(tipo_operacao: str, status_207: str) -> str:
    """§8: 207 status=0 -> AGUARDANDO_WEBHOOK (ou ATUALIZANDO se tipoOperacao=A,
    já que a atualização espera o webhook num ramo próprio do diagrama);
    status=1 -> REJEITADO_ESTRUTURAL."""
    if status_207 == "1":
        return REJEITADO_ESTRUTURAL
    if tipo_operacao == "A":
        return ATUALIZANDO
    return AGUARDANDO_WEBHOOK


def estado_apos_400() -> str:
    """§8: erro estrutural síncrono (HTTP 400) — mesmo destino que 207 status=1."""
    return REJEITADO_ESTRUTURAL


def estado_apos_webhook(estado_atual: str, status_webhook: str) -> str:
    """§8: webhook status=0 -> REGISTRADO (a partir de AGUARDANDO_WEBHOOK ou
    ATUALIZANDO); status=1 -> REJEITADO."""
    if estado_atual not in (AGUARDANDO_WEBHOOK, ATUALIZANDO):
        raise EstadoInvalidoError(estado_atual, f"webhook (status={status_webhook})")
    return REGISTRADO if status_webhook == "0" else REJEITADO


def estado_apos_timeout_sla(estado_atual: str) -> str:
    """§8: nenhum webhook em 30min (configurável) -> PENDENTE_CONCILIACAO."""
    if estado_atual not in (AGUARDANDO_WEBHOOK, ATUALIZANDO):
        raise EstadoInvalidoError(estado_atual, "timeout SLA")
    return PENDENTE_CONCILIACAO


_OPERACAO_PARA_ESTADO_POS_REGISTRO = {
    "I": INATIVADO,
    "B": BAIXADO,
    "P": RESILIDO_PARCIAL,
    "R": RESILIDO_TOTAL,
}


def estado_apos_operacao_pos_registro(estado_atual: str, tipo_operacao: str) -> str:
    """§8: a partir de REGISTRADO, tipoOperacao I/B/P/R leva a um estado
    terminal específico."""
    if estado_atual != REGISTRADO:
        raise EstadoInvalidoError(estado_atual, f"operação {tipo_operacao}")
    if tipo_operacao not in _OPERACAO_PARA_ESTADO_POS_REGISTRO:
        raise ValueError(f"tipoOperacao '{tipo_operacao}' não leva a um estado pós-registro")
    return _OPERACAO_PARA_ESTADO_POS_REGISTRO[tipo_operacao]


_RESULTADO_PARA_SUBESTADO = {
    "0": NAO_APLICAVEL,
    "1": SUFICIENTE,
    "2": INSUFICIENTE,
    "3": EXCESSO,
}


def sub_estado_garantia(resultado_distribuicao_onus: str) -> str:
    """§8: sub-estado de garantia derivado de resultadoDistribuicaoOnus."""
    try:
        return _RESULTADO_PARA_SUBESTADO[resultado_distribuicao_onus]
    except KeyError:
        raise ValueError(f"resultadoDistribuicaoOnus inválido: {resultado_distribuicao_onus}")


def eh_subgarantido(resultado_distribuicao_onus: str) -> bool:
    """§5.2: resultadoDistribuicaoOnus=2 (insuficiente) -> contrato registrado mas
    subgarantido; o caller deve emitir o evento de domínio ContratoSubgarantido
    e alertar a operação/crédito."""
    return resultado_distribuicao_onus == "2"


def eh_candidato_liberacao_excedente(resultado_distribuicao_onus: str) -> bool:
    """§5.2: resultadoDistribuicaoOnus=3 (em excesso) -> candidato a liberação de
    excedente (AP026, fora do escopo desta fase) — apenas sinaliza."""
    return resultado_distribuicao_onus == "3"


def ur_teve_insucesso(indicador_oneracao: str) -> bool:
    """§5.2: indicadorOneracao=0 numa UR -> insucesso naquela UR; o caller deve
    contabilizar e expor no detalhe."""
    return indicador_oneracao == "0"


def indicador_critico(criticidade: str) -> bool:
    """§5.2: criticidade >= 2 em qualquer indicador de consistência -> destacar
    na resposta interna e notificar o time de crédito."""
    return int(criticidade) >= 2
