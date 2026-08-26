"""Orquestra as validações C01-C20 (apps/contratos/validation.py, Plano 08)
contra o payload de CRIAÇÃO de contrato (SPEC-02 §4, tipoOperacao=C — sem
o campo tipoOperacao em si, quem chama decide isso). Fail-fast: levanta
ValidationError na primeira regra violada, mesmo padrão de cada
validar_cXX individual.

Fora do escopo (Plano 12, ver Global Constraints do plano):
- C14 (lista de participantes do SLC) — nenhuma fonte de dado existe
  neste código para essa lista; não inventada aqui.
- C17 (campos estáticos imutáveis) — só se aplica a tipoOperacao=A
  (atualização), fora do escopo deste plano (create-only).
- C20 (limite de 45 efeitos por UR) — para uma garantia NOVA (criação),
  qtd_efeitos_existente é sempre 0 (a garantia não existe até este
  contrato ser criado), então a regra é trivialmente satisfeita; não
  chamada aqui.

Converte todo valor monetário de int/float (o que json.loads produz)
para decimal.Decimal — nunca grava float numa coluna NUMERIC (design
§13.3) — e toda data em string AAAA-MM-DD para datetime.date. Retorna
um NOVO dict; o payload de entrada nunca é mutado.
"""

from datetime import date
from decimal import Decimal

from apps.contratos.validation import (
    ValidationError,
    tipo_documento,
    validar_c01_documento,
    validar_c02_repactuacao,
    validar_c03_repactuacao_sem_garantias,
    validar_c04_valor_monetario,
    validar_c05_modalidade_parcelado,
    validar_c06_tipo_distribuicao,
    validar_c07_regra_divisao_percentual,
    validar_c08_data_inicio_futura,
    validar_c09_ordem_datas,
    validar_c10_raiz_titular_igual_ufr,
    validar_c11_raiz_documento_unico,
    validar_c12_referencia_garantia_unica,
    validar_c13_sem_sobreposicao_garantias,
    validar_c15_numero_conta,
    validar_c16_domicilio_formatos,
    validar_c18_bloqueio_judicial,
    validar_c19_arranjos_no_dominio,
)


def _dec(valor) -> Decimal:
    return Decimal(str(valor))


def _eh_raiz(documento) -> bool:
    return bool(documento) and tipo_documento(documento) == "CNPJ_RAIZ"


# Campos de nível-contrato que apps.contratos.contrato_repository.inserir_contrato_criado
# lê via acesso direto (payload_validado["..."]), mas que nenhum validador C0x cobre
# incondicionalmente (alguns só são tocados dentro do loop `for g in garantias`, que não
# roda quando garantias == [] — legítimo para repactuacao="1", C03). Sem este guard, um
# payload faltando um destes passa pela validação local inteira, é submetido à CERC (uma
# chamada externa real), e só então explode com KeyError não tratado ao persistir — depois
# de já ter gasto a chamada à CERC. Checado aqui, primeiro, para que a falta de um destes
# vire um 422 comum (mesmo caminho de qualquer outra violação de validação local), nunca
# um 500 nem uma chamada à CERC desperdiçada.
CAMPOS_OBRIGATORIOS_NIVEL_CONTRATO = (
    "referenciaExterna", "identificadorContrato", "cnpjDetentor",
    "tipoEfeito", "modalidadeOperacao", "identificacaoGestaoEntidadeRegistradora",
)


def _validar_campos_obrigatorios(payload: dict) -> None:
    # `campo not in payload` (ausência da CHAVE), não um teste de "falsy" — o
    # bug real é o KeyError de contrato_repository.inserir_contrato_criado
    # (payload_validado["campo"]) quando a CHAVE nunca chegou no payload, não
    # o valor estar vazio/None quando presente. Um teste "falsy" aqui
    # quebraria test_validar_criacao_contrato_c18_bloqueio_judicial_sem_identificador
    # (Tarefa 1, já mesclada), que passa identificadorContrato="" de propósito
    # para exercitar C18 — esse "" é um valor de payload legítimo (a chave
    # existe), não um payload malformado.
    for campo in CAMPOS_OBRIGATORIOS_NIVEL_CONTRATO:
        if campo not in payload:
            raise ValidationError("CAMPO_OBRIGATORIO", f"'{campo}' é obrigatório")


def validar_criacao_contrato(payload: dict, *, hoje: date, ativos_arranjos: set) -> dict:
    _validar_campos_obrigatorios(payload)
    validar_c01_documento(payload["documentoContratante"])
    validar_c02_repactuacao(payload.get("repactuacao"), payload.get("identificacaoContratosAnteriores"))
    validar_c03_repactuacao_sem_garantias(payload.get("repactuacao"), payload.get("garantias"))
    validar_c04_valor_monetario(_dec(payload["saldoDevedor"]), "saldoDevedor")
    validar_c04_valor_monetario(_dec(payload["limiteOperacaoGarantida"]), "limiteOperacaoGarantida")
    validar_c04_valor_monetario(_dec(payload["valorMantido"]), "valorMantido")
    validar_c05_modalidade_parcelado(payload.get("modalidadeOperacao"), payload.get("parcelas"))
    validar_c18_bloqueio_judicial(payload.get("tipoEfeito"), payload.get("identificadorContrato"))

    for parcela in payload.get("parcelas", []):
        validar_c04_valor_monetario(_dec(parcela["valor"]), "parcelas[].valor")

    garantias = payload.get("garantias", [])
    validar_c12_referencia_garantia_unica(garantias)
    validar_c13_sem_sobreposicao_garantias([
        {
            "credenciadoras": set(g["definicaoUnidadeRecebivel"]["listaCnpjCredenciadora"]),
            "arranjos": set(g["definicaoUnidadeRecebivel"]["listaCodigoArranjoPagamento"]),
            "ufr_titular": (
                g["definicaoUnidadeRecebivel"].get("documentoUsuarioFinalRecebedor"),
                g["definicaoUnidadeRecebivel"].get("documentoTitular"),
            ),
            "data_inicio": date.fromisoformat(g["definicaoUnidadeRecebivel"]["dataInicio"]),
            "data_fim": date.fromisoformat(g["definicaoUnidadeRecebivel"]["dataFim"]),
        }
        for g in garantias
    ])

    for g in garantias:
        definicao = g["definicaoUnidadeRecebivel"]
        domicilio = g["domicilioPagamento"]
        documento_ufr = definicao.get("documentoUsuarioFinalRecebedor")
        documento_titular = definicao.get("documentoTitular")

        validar_c06_tipo_distribuicao(g.get("tipoDistribuicao"), payload["identificacaoGestaoEntidadeRegistradora"])
        validar_c07_regra_divisao_percentual(g["regrasDivisao"], _dec(g["valorAOnerar"]))
        validar_c04_valor_monetario(_dec(g["valorAOnerar"]), "garantias[].valorAOnerar")
        validar_c08_data_inicio_futura(date.fromisoformat(definicao["dataInicio"]), hoje)
        validar_c09_ordem_datas(date.fromisoformat(definicao["dataInicio"]), date.fromisoformat(definicao["dataFim"]))

        eh_raiz = _eh_raiz(documento_ufr)
        validar_c10_raiz_titular_igual_ufr(documento_titular, documento_ufr, eh_raiz)
        validar_c11_raiz_documento_unico([d for d in (documento_ufr, documento_titular) if d], eh_raiz)

        validar_c15_numero_conta(domicilio["tipoConta"], domicilio["numeroConta"])
        validar_c16_domicilio_formatos(domicilio["ispb"], domicilio.get("compe"), domicilio["agencia"])
        validar_c19_arranjos_no_dominio(definicao["listaCodigoArranjoPagamento"], ativos_arranjos)

    return _converter_para_persistencia(payload)


def _converter_para_persistencia(payload: dict) -> dict:
    convertido = dict(payload)
    convertido["saldoDevedor"] = _dec(payload["saldoDevedor"])
    convertido["limiteOperacaoGarantida"] = _dec(payload["limiteOperacaoGarantida"])
    convertido["valorMantido"] = _dec(payload["valorMantido"])
    convertido["dataAssinatura"] = date.fromisoformat(payload["dataAssinatura"])
    convertido["dataVencimento"] = date.fromisoformat(payload["dataVencimento"])
    if payload.get("taxaJuros") is not None:
        convertido["taxaJuros"] = _dec(payload["taxaJuros"])

    convertido["parcelas"] = [
        {**p, "valor": _dec(p["valor"]), "vencimento": date.fromisoformat(p["vencimento"])}
        for p in payload.get("parcelas", [])
    ]

    convertido["garantias"] = []
    for g in payload.get("garantias", []):
        g_convertida = dict(g)
        g_convertida["valorAOnerar"] = _dec(g["valorAOnerar"])
        definicao = dict(g["definicaoUnidadeRecebivel"])
        definicao["dataInicio"] = date.fromisoformat(definicao["dataInicio"])
        definicao["dataFim"] = date.fromisoformat(definicao["dataFim"])
        g_convertida["definicaoUnidadeRecebivel"] = definicao
        convertido["garantias"].append(g_convertida)

    return convertido
