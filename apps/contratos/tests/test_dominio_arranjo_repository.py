from datetime import datetime, timezone

from apps.contratos.dominio_arranjo_repository import sincronizar_arranjos
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"


def _limpar(*codigos):
    db = get_db(FINANCIADOR_TESTE)
    for codigo in codigos:
        db.table("dominio_arranjo").delete().eq("codigo", codigo).execute()


def test_sincronizar_arranjos_ativa_codigos_vigentes():
    _limpar("ZZ1", "ZZ2")
    try:
        resultado = sincronizar_arranjos(FINANCIADOR_TESTE, ["ZZ1", "ZZ2"])

        assert resultado["ativados"] == 2
        db = get_db(FINANCIADOR_TESTE)
        linha = db.table("dominio_arranjo").select("*").eq("codigo", "ZZ1").execute().data[0]
        assert linha["ativo"] is True
    finally:
        _limpar("ZZ1", "ZZ2")


def test_sincronizar_arranjos_desativa_codigo_que_saiu_da_lista():
    _limpar("ZZ3", "OUTRO")
    try:
        db = get_db(FINANCIADOR_TESTE)
        db.table("dominio_arranjo").insert({
            "codigo": "ZZ3", "ativo": True, "atualizado_em": datetime.now(timezone.utc),
        }).execute()

        resultado = sincronizar_arranjos(FINANCIADOR_TESTE, ["OUTRO"])

        assert resultado["desativados"] == 1
        linha = db.table("dominio_arranjo").select("*").eq("codigo", "ZZ3").execute().data[0]
        assert linha["ativo"] is False
    finally:
        _limpar("ZZ3", "OUTRO")


def test_sincronizar_arranjos_reativa_codigo_que_estava_inativo():
    _limpar("ZZ4")
    try:
        db = get_db(FINANCIADOR_TESTE)
        db.table("dominio_arranjo").insert({
            "codigo": "ZZ4", "ativo": False, "atualizado_em": datetime.now(timezone.utc),
        }).execute()

        sincronizar_arranjos(FINANCIADOR_TESTE, ["ZZ4"])

        linha = db.table("dominio_arranjo").select("*").eq("codigo", "ZZ4").execute().data[0]
        assert linha["ativo"] is True
    finally:
        _limpar("ZZ4")


def test_sincronizar_arranjos_preserva_descricao_existente():
    _limpar("ZZ5")
    try:
        db = get_db(FINANCIADOR_TESTE)
        db.table("dominio_arranjo").insert({
            "codigo": "ZZ5", "descricao": "Descrição Manual", "ativo": True,
            "atualizado_em": datetime.now(timezone.utc),
        }).execute()

        sincronizar_arranjos(FINANCIADOR_TESTE, ["ZZ5"])

        linha = db.table("dominio_arranjo").select("*").eq("codigo", "ZZ5").execute().data[0]
        assert linha["descricao"] == "Descrição Manual"
    finally:
        _limpar("ZZ5")


def test_sincronizar_arranjos_nao_afeta_codigo_fora_da_execucao():
    # Um código que nunca esteve na lista vigente (nem antes, nem agora) não
    # deve ser tocado — só o que estava ATIVO e saiu da lista é desativado.
    _limpar("ZZ6")
    try:
        sincronizar_arranjos(FINANCIADOR_TESTE, ["ZZ_NAO_RELACIONADO_QUALQUER"])
        db = get_db(FINANCIADOR_TESTE)
        assert db.table("dominio_arranjo").select("*").eq("codigo", "ZZ6").execute().data == []
    finally:
        _limpar("ZZ6", "ZZ_NAO_RELACIONADO_QUALQUER")
