"""Máquina de estados do contrato — SPEC-02 §8, regras derivadas em §5.2.

Funções puras — nenhuma chamada a banco/CERC. Quem persiste uma transição
(webhook receiver, job de reconciliação, views internas) chama estas
funções primeiro e só então grava o resultado; este módulo nunca toca
`contrato`/`garantia_ur`/`cerc_requisicao` diretamente.

Diagrama (§8, com uma correção do Plano 13 — ver abaixo):

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
    v           v              v              ATUALIZANDO -> (webhook) -> REGISTRADO | REJEITADO
REGISTRADO  REJEITADO   PENDENTE_CONCILIACAO
    |
    +-- op I --> INATIVANDO        -> (webhook) -> INATIVADO        | REGISTRADO
    +-- op B --> BAIXANDO          -> (webhook) -> BAIXADO          | REGISTRADO
    +-- op P --> RESILINDO_PARCIAL -> (webhook) -> RESILIDO_PARCIAL | REGISTRADO
    +-- op R --> RESILINDO_TOTAL   -> (webhook) -> RESILIDO_TOTAL   | REGISTRADO

Correção do Plano 13: a SPEC-02 §8 desenha "op I/B/P/R" indo direto de
REGISTRADO pro estado terminal, sem espera — mas isso contradiz a regra
central da §0 ("nenhuma operação é definitiva antes da confirmação
assíncrona"). Decisão confirmada com o usuário (não assumida): operações
pós-registro esperam o webhook igual à atualização (op=A) já espera hoje.
Uma falha na confirmação NÃO rejeita o contrato original (ele já estava
REGISTRADO antes da tentativa) — volta pra REGISTRADO, só a operação
específica não se efetivou.

O SLA em si (30min pra PENDENTE_CONCILIACAO, alerta após 2h) não é
responsabilidade deste módulo — ele só responde "qual o próximo estado
quando um timeout acontece", não "quanto tempo já passou". Isso é do
job de reconciliação (Plano futuro), que é quem sabe a hora real.
"""

ENVIANDO = "ENVIANDO"
AGUARDANDO_WEBHOOK = "AGUARDANDO_WEBHOOK"
REJEITADO_ESTRUTURAL = "REJEITADO_ESTRUTURAL"
REGISTRADO = "REGISTRADO"
REJEITADO = "REJEITADO"
PENDENTE_CONCILIACAO = "PENDENTE_CONCILIACAO"
ATUALIZANDO = "ATUALIZANDO"
INATIVANDO = "INATIVANDO"
BAIXANDO = "BAIXANDO"
RESILINDO_PARCIAL = "RESILINDO_PARCIAL"
RESILINDO_TOTAL = "RESILINDO_TOTAL"
INATIVADO = "INATIVADO"
BAIXADO = "BAIXADO"
RESILIDO_PARCIAL = "RESILIDO_PARCIAL"
RESILIDO_TOTAL = "RESILIDO_TOTAL"

ESTADOS_TERMINAIS = {
    REJEITADO_ESTRUTURAL, REJEITADO, INATIVADO, BAIXADO, RESILIDO_PARCIAL, RESILIDO_TOTAL,
}

ESTADOS_AGUARDANDO_WEBHOOK = {
    AGUARDANDO_WEBHOOK, ATUALIZANDO, INATIVANDO, BAIXANDO, RESILINDO_PARCIAL, RESILINDO_TOTAL,
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


_TRANSICOES_WEBHOOK = {
    AGUARDANDO_WEBHOOK: (REGISTRADO, REJEITADO),
    ATUALIZANDO: (REGISTRADO, REJEITADO),
    INATIVANDO: (INATIVADO, REGISTRADO),
    BAIXANDO: (BAIXADO, REGISTRADO),
    RESILINDO_PARCIAL: (RESILIDO_PARCIAL, REGISTRADO),
    RESILINDO_TOTAL: (RESILIDO_TOTAL, REGISTRADO),
}


def estado_apos_webhook(estado_atual: str, status_webhook: str) -> str:
    """§8: webhook status=0 -> sucesso; status=1 -> falha. O par
    (sucesso, falha) depende de QUAL espera está sendo resolvida — carregado
    no próprio `estado_atual`, então o caller nunca precisa saber qual
    tipoOperacao originou a espera (Plano 11's webhook processor não precisa
    de nenhuma mudança por causa disso — ver design do Plano 13).

    Para criação/atualização (AGUARDANDO_WEBHOOK/ATUALIZANDO), uma falha
    derruba pra REJEITADO — o registro nunca existiu de verdade. Para
    operações pós-registro (INATIVANDO/BAIXANDO/RESILINDO_*), uma falha NÃO
    rejeita o contrato — ele já estava REGISTRADO antes da tentativa, então
    volta pra REGISTRADO; só a operação específica não se efetivou."""
    try:
        sucesso, falha = _TRANSICOES_WEBHOOK[estado_atual]
    except KeyError:
        raise EstadoInvalidoError(estado_atual, f"webhook (status={status_webhook})")
    return sucesso if status_webhook == "0" else falha


def estado_apos_timeout_sla(estado_atual: str) -> str:
    """§8: nenhum webhook em 30min (configurável) -> PENDENTE_CONCILIACAO,
    de qualquer um dos estados de espera (criação, atualização ou operação
    pós-registro — o timeout não distingue qual)."""
    if estado_atual not in ESTADOS_AGUARDANDO_WEBHOOK:
        raise EstadoInvalidoError(estado_atual, "timeout SLA")
    return PENDENTE_CONCILIACAO


_OPERACAO_PARA_ESTADO_ESPERA = {
    "I": INATIVANDO,
    "B": BAIXANDO,
    "P": RESILINDO_PARCIAL,
    "R": RESILINDO_TOTAL,
}


def estado_apos_operacao_pos_registro(estado_atual: str, tipo_operacao: str) -> str:
    """§8: a partir de REGISTRADO, tipoOperacao I/B/P/R entra num estado de
    ESPERA pelo webhook que confirma a operação — mesmo raciocínio de
    ATUALIZANDO: nenhuma operação pós-registro é definitiva antes da
    confirmação assíncrona (SPEC-02 §0, decisão confirmada no Plano 13).
    Não retorna mais o estado terminal diretamente."""
    if estado_atual != REGISTRADO:
        raise EstadoInvalidoError(estado_atual, f"operação {tipo_operacao}")
    if tipo_operacao not in _OPERACAO_PARA_ESTADO_ESPERA:
        raise ValueError(f"tipoOperacao '{tipo_operacao}' não leva a um estado pós-registro")
    return _OPERACAO_PARA_ESTADO_ESPERA[tipo_operacao]


def estado_apos_207_pos_registro(tipo_operacao: str, status_207: str) -> str:
    """§8 (Plano 14): resultado SÍNCRONO (207) de uma submissão I/B/P/R —
    distinto de `estado_apos_operacao_pos_registro`, que só decide o estado
    de ESPERA a entrar quando a CERC aceita (status=0); esta função também
    cobre o caminho de rejeição síncrona (status=1), que Plano 13 nunca
    precisou tratar (não existia endpoint algum ainda).

    status=0 -> delega para estado_apos_operacao_pos_registro (mesmo destino
    de espera). status=1 -> rejeição ESTRUTURAL da OPERAÇÃO, não do
    contrato — REGISTRADO (nunca REJEITADO_ESTRUTURAL, que implicaria que o
    contrato em si nunca foi registrado). Mesma razão do Plano 13 para a
    falha via webhook (INATIVANDO/BAIXANDO/... -> REGISTRADO em status=1),
    aplicada aqui ao caminho síncrono."""
    if status_207 == "1":
        return REGISTRADO
    return estado_apos_operacao_pos_registro(REGISTRADO, tipo_operacao)


_OPERACAO_PARA_ESTADO_TERMINAL = {
    "I": INATIVADO,
    "B": BAIXADO,
    "P": RESILIDO_PARCIAL,
    "R": RESILIDO_TOTAL,
}


def situacao_operacao_pos_registro(estado_atual: str, tipo_operacao: str) -> str:
    """§8 (Plano 14): o que fazer com uma NOVA requisição I/B/P/R, ANTES de
    chamar a CERC — decide se o endpoint deve prosseguir, responder com um
    replay idempotente, ou recusar por conflito.

    "PROSSEGUIR": estado_atual == REGISTRADO — pode submeter à CERC.
    "REPLAY": estado_atual já é o estado de ESPERA ou o TERMINAL desta MESMA
    tipo_operacao (ex.: tipo_operacao="I" e estado_atual em
    {INATIVANDO, INATIVADO}) — requisição repetida, não chama a CERC de
    novo, quem chama devolve o estado atual como está.
    "CONFLITO": qualquer outro estado_atual — inclui tanto "o contrato ainda
    não chegou a REGISTRADO" quanto "a OUTRA operação pós-registro está em
    curso ou já concluída" (ex.: tentar inativar um contrato BAIXANDO)."""
    if estado_atual == REGISTRADO:
        return "PROSSEGUIR"
    if estado_atual == _OPERACAO_PARA_ESTADO_ESPERA.get(tipo_operacao) or estado_atual == _OPERACAO_PARA_ESTADO_TERMINAL.get(tipo_operacao):
        return "REPLAY"
    return "CONFLITO"


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
