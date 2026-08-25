"""Validações locais pré-CERC — SPEC-02 §9 (C01-C20).

Funções puras — nenhuma chamada a rede/CERC nem ao banco. Onde uma regra
depende de dado externo (domínio de arranjos sincronizado, contrato já
persistido, lista de participantes do SLC, contagem de efeitos já
aplicados numa UR), essa informação é parâmetro explícito da função — a
camada que já acessa o banco (uma view/handler de um plano futuro) busca
o dado e chama a função daqui, em vez desta função acessar o banco por
conta própria (mesmo padrão do ap-back-optin: validar_arranjos/VAL005).
"""

import re
from decimal import Decimal


class ValidationError(Exception):
    def __init__(self, codigo: str, mensagem: str):
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(f"{codigo}: {mensagem}")


# --- Documento (normalizador CPF/CNPJ/raiz — convenção compartilhada com ap-back-optin) ---

def normalizar_documento(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        raise ValidationError("C01", "documento vazio")
    if len(digits) <= 8:
        return digits.zfill(8)
    if len(digits) <= 11:
        return digits.zfill(11)
    return digits.zfill(14)


def tipo_documento(documento: str) -> str:
    tamanho = len(documento)
    if tamanho == 8:
        return "CNPJ_RAIZ"
    if tamanho == 11:
        return "CPF"
    if tamanho == 14:
        return "CNPJ"
    raise ValidationError("C01", f"documento com tamanho inválido: {tamanho}")


def _digito_verificador(base: str, pesos: list) -> str:
    soma = sum(int(d) * p for d, p in zip(base, pesos))
    resto = soma % 11
    return "0" if resto < 2 else str(11 - resto)


def _validar_cpf(cpf: str) -> bool:
    if cpf == cpf[0] * 11:
        return False
    dv1 = _digito_verificador(cpf[:9], list(range(10, 1, -1)))
    dv2 = _digito_verificador(cpf[:9] + dv1, list(range(11, 1, -1)))
    return cpf[-2:] == dv1 + dv2


def _validar_cnpj(cnpj: str) -> bool:
    if cnpj == cnpj[0] * 14:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    dv1 = _digito_verificador(cnpj[:12], pesos1)
    dv2 = _digito_verificador(cnpj[:12] + dv1, pesos2)
    return cnpj[-2:] == dv1 + dv2


def validar_c01_documento(raw: str) -> tuple:
    """C01 — 11/14 dígitos (ou 8 pra raiz), DV válido, zero-pad. Evita 107008/107015/107017."""
    documento = normalizar_documento(raw)
    tipo = tipo_documento(documento)
    if tipo == "CPF" and not _validar_cpf(documento):
        raise ValidationError("C01", "dígito verificador de CPF inválido")
    if tipo == "CNPJ" and not _validar_cnpj(documento):
        raise ValidationError("C01", "dígito verificador de CNPJ inválido")
    return documento, tipo


def validar_c02_repactuacao(repactuacao: str, identificacao_contratos_anteriores) -> None:
    """C02 — repactuacao=1 exige identificacaoContratosAnteriores não vazio. Evita 107011."""
    if repactuacao == "1" and not identificacao_contratos_anteriores:
        raise ValidationError("C02", "repactuacao=1 exige identificacaoContratosAnteriores")


def validar_c03_repactuacao_sem_garantias(repactuacao: str, garantias: list) -> None:
    """C03 — repactuacao=1 exige garantias[] vazio. Evita 107816."""
    if repactuacao == "1" and garantias:
        raise ValidationError("C03", "repactuacao=1 não pode ter garantias[] especificadas")


def validar_c04_valor_monetario(valor: Decimal, campo: str) -> None:
    """C04 — valores monetários >= 0.01. Evita 107021/107023/107025."""
    if valor is None or valor < Decimal("0.01"):
        raise ValidationError("C04", f"{campo} deve ser >= 0.01")


def validar_c05_modalidade_parcelado(modalidade_operacao: str, parcelas: list) -> None:
    """C05 — modalidadeOperacao=2 (parcelado) exige parcelas[] não vazio. Evita 107034."""
    if modalidade_operacao == "2" and not parcelas:
        raise ValidationError("C05", "modalidadeOperacao=2 exige parcelas[] não vazio")


def validar_c06_tipo_distribuicao(tipo_distribuicao, identificacao_gestao_entidade_registradora: str) -> None:
    """C06 — tipoDistribuicao presente se e somente se gestão=1. Evita 107224/107503."""
    gestao_registradora = identificacao_gestao_entidade_registradora == "1"
    if tipo_distribuicao and not gestao_registradora:
        raise ValidationError("C06", "tipoDistribuicao só pode ser informado quando gestão=1")
    if gestao_registradora and not tipo_distribuicao:
        raise ValidationError("C06", "tipoDistribuicao obrigatório quando gestão=1")


def validar_c07_regra_divisao_percentual(regras_divisao: str, valor_a_onerar: Decimal) -> None:
    """C07 — regrasDivisao=2 (percentual) não pode exceder 100. Evita 107825."""
    if regras_divisao == "2" and valor_a_onerar > Decimal("100"):
        raise ValidationError("C07", "regrasDivisao=2 (percentual) não pode exceder 100")


def validar_c08_data_inicio_futura(data_inicio, hoje) -> None:
    """C08 — definicao.dataInicio >= hoje. Evita 107813."""
    if data_inicio < hoje:
        raise ValidationError("C08", "dataInicio não pode ser no passado")


def validar_c09_ordem_datas(data_inicio, data_fim) -> None:
    """C09 — definicao.dataFim >= definicao.dataInicio. Evita 107217."""
    if data_fim < data_inicio:
        raise ValidationError("C09", "dataFim menor que dataInicio")


def validar_c10_raiz_titular_igual_ufr(documento_titular, documento_usuario_final_recebedor, eh_raiz: bool) -> None:
    """C10 — CNPJ raiz exige documentoTitular == documentoUsuarioFinalRecebedor. Evita 107814."""
    if eh_raiz and documento_titular != documento_usuario_final_recebedor:
        raise ValidationError("C10", "CNPJ raiz exige documentoTitular == documentoUsuarioFinalRecebedor")


def validar_c11_raiz_documento_unico(documentos: list, eh_raiz: bool) -> None:
    """C11 — CNPJ raiz exige único documento na definição. Evita 107815."""
    if eh_raiz and len(set(documentos)) > 1:
        raise ValidationError("C11", "CNPJ raiz deve ser o único documento especificado na definição")


def validar_c12_referencia_garantia_unica(garantias: list) -> None:
    """C12 — referenciaExterna das garantias única dentro do contrato. Evita 107505."""
    referencias = [g["referenciaExterna"] for g in garantias]
    if len(referencias) != len(set(referencias)):
        raise ValidationError("C12", "referenciaExterna de garantia duplicada no mesmo contrato")


def conjuntos_se_sobrepoem(a: set, b: set) -> bool:
    """Compara duas listas (credenciadoras ou arranjos) tratando '99T' como curinga
    universal (SPEC-02 §4.4/§9 C13): se qualquer lado contiver '99T', há sobreposição total."""
    if "99T" in a or "99T" in b:
        return True
    return bool(a & b)


def vigencias_se_sobrepoem(inicio_a, fim_a, inicio_b, fim_b) -> bool:
    """Interseção de intervalos fechados [inicio, fim]."""
    return inicio_a <= fim_b and inicio_b <= fim_a


def validar_c13_sem_sobreposicao_garantias(garantias: list) -> None:
    """C13 — sem sobreposição entre definições de garantia do mesmo contrato (mesma
    credenciadora x arranjo x UFR/titular x interseção de datas, tratando 99T como
    universo). Evita 107823.

    `garantias` é uma lista de dicts com chaves: credenciadoras (set), arranjos
    (set), ufr_titular (valor hashable identificando o UFR/titular do filtro),
    data_inicio, data_fim (date).
    """
    for i, a in enumerate(garantias):
        for b in garantias[i + 1:]:
            if not conjuntos_se_sobrepoem(a["credenciadoras"], b["credenciadoras"]):
                continue
            if not conjuntos_se_sobrepoem(a["arranjos"], b["arranjos"]):
                continue
            if a["ufr_titular"] != b["ufr_titular"]:
                continue
            if not vigencias_se_sobrepoem(a["data_inicio"], a["data_fim"], b["data_inicio"], b["data_fim"]):
                continue
            raise ValidationError("C13", "sobreposição entre definições de garantia do mesmo contrato")


def validar_c14_ispb_no_slc(ispb: str, participantes_slc: set) -> None:
    """C14 — ISPB do domicílio pertence à lista de participantes do SLC. Recusa por
    regra do SLC (sem código 107xxx específico). `participantes_slc` vem de uma
    lista sincronizada — buscada pelo caller, não por este módulo."""
    if ispb not in participantes_slc:
        raise ValidationError("C14", f"ISPB {ispb} não pertence à lista de participantes do SLC")


def validar_c15_numero_conta(tipo_conta: str, numero_conta: str) -> None:
    """C15 — numeroConta com DV e hífen p/ CC/CD/PP; sem hífen p/ PG. Evita 107236."""
    tem_hifen = "-" in numero_conta
    if tipo_conta in ("CC", "CD", "PP") and not tem_hifen:
        raise ValidationError("C15", f"numeroConta de conta {tipo_conta} exige dígito verificador separado por hífen")
    if tipo_conta == "PG" and tem_hifen:
        raise ValidationError("C15", "numeroConta de conta PG não deve ter hífen")


def validar_c16_domicilio_formatos(ispb: str, compe, agencia: str) -> None:
    """C16 — ispb=8 dígitos; compe=3 dígitos; agencia <= 8 dígitos sem DV. Evita 107230-107234."""
    if not (ispb and len(ispb) == 8 and ispb.isdigit()):
        raise ValidationError("C16", "ispb deve ter exatamente 8 dígitos")
    if compe and not (len(compe) == 3 and compe.isdigit()):
        raise ValidationError("C16", "compe deve ter exatamente 3 dígitos")
    if not (agencia and agencia.isdigit() and len(agencia) <= 8):
        raise ValidationError("C16", "agencia deve ter até 8 dígitos, sem dígito verificador")


CAMPOS_ESTATICOS = {
    "repactuacao",
    "documentoContratante",
    "identificacaoContratosAnteriores",
    "dataAssinatura",
    "dataVencimento",
    "modalidadeOperacao",
    "parcelas",
}


def validar_c17_campos_estaticos_imutaveis(payload: dict, contrato_atual: dict) -> None:
    """C17 — em tipoOperacao=A, nenhum campo estático (SPEC-02 §2.1) pode ser
    alterado. Evita 107807. `contrato_atual` é o registro já persistido
    localmente — buscado pelo caller antes de montar a atualização, não por
    este módulo."""
    for campo in CAMPOS_ESTATICOS:
        if campo in payload and campo in contrato_atual and payload[campo] != contrato_atual[campo]:
            raise ValidationError("C17", f"campo estático '{campo}' não pode ser alterado")


def validar_c18_bloqueio_judicial(tipo_efeito: str, identificador_contrato: str) -> None:
    """C18 — tipoEfeito=4 (bloqueio judicial) exige identificadorContrato com o
    número do processo judicial. Sem código de erro CERC associado (SPEC-02 §9
    lista '—') — checagem apenas de presença, não de formato de processo."""
    if tipo_efeito == "4" and not identificador_contrato:
        raise ValidationError("C18", "bloqueio judicial exige identificadorContrato com o número do processo")


def validar_c19_arranjos_no_dominio(lista_arranjos: list, ativos: set) -> None:
    """C19 — arranjos pertencem ao domínio vigente sincronizado. Evita 107212. '99T'
    sempre aceito sem checar domínio. `ativos` vem de dominio_arranjo — buscado
    pelo caller, não por este módulo."""
    for codigo in lista_arranjos:
        if codigo != "99T" and codigo not in ativos:
            raise ValidationError("C19", f"arranjo fora do domínio vigente: {codigo}")


def validar_c20_limite_efeitos_por_ur(qtd_efeitos_existente: int, novos_efeitos: int = 1) -> None:
    """C20 — estimativa de efeitos por UR <= 45, quando a informação local existir.
    Evita 107842. `qtd_efeitos_existente` vem de garantia_ur — buscado pelo
    caller, não por este módulo."""
    if qtd_efeitos_existente + novos_efeitos > 45:
        raise ValidationError("C20", "limite de 45 efeitos por UR seria excedido")
