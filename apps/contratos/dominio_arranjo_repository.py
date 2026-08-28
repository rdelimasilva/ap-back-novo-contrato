"""Sincronização de `dominio_arranjo` (Plano 16). Sem endpoint CERC de
consulta de domínio documentado (SPEC-01 §2): a lista abaixo é o domínio
v1.5 publicado na documentação da CERC, transcrito estaticamente — quando a
CERC publicar uma nova versão, esta constante precisa ser atualizada à mão.
'99T' (coringa "todos os arranjos") não entra aqui: já é tratado à parte em
apps.contratos.validation.validar_c19_arranjos_no_dominio.
"""

from datetime import datetime, timezone

from shared.cloudsql_client import get_db

CODIGOS_ARRANJO_VIGENTES = [
    "ACC", "BCC", "BCD", "CBC", "ECC", "ECD", "GCC", "HCC", "JCC", "MCC",
    "MCD", "OCD", "SCC", "SCD", "VCC", "VCD", "VDC", "HCD", "SIC", "BRS",
    "MAC", "CUP", "CZC", "FRC", "MXC", "SFC", "TKC", "BNC", "CCD", "BRC",
    "SPC", "CSC", "DAC", "DCC", "AGC", "AUC", "RCC", "AVC", "DBC",
]


def sincronizar_arranjos(financiador_id: str, codigos_vigentes: list) -> dict:
    db = get_db(financiador_id)
    agora = datetime.now(timezone.utc)
    vigentes = set(codigos_vigentes)

    for codigo in vigentes:
        db.table("dominio_arranjo").upsert(
            {"codigo": codigo, "ativo": True, "atualizado_em": agora},
            on_conflict="codigo",
        ).execute()

    ativos = db.table("dominio_arranjo").select("codigo").eq("ativo", True).execute()
    desativados = 0
    for row in ativos.data:
        if row["codigo"] not in vigentes:
            db.table("dominio_arranjo").update({"ativo": False, "atualizado_em": agora}).eq("codigo", row["codigo"]).execute()
            desativados += 1

    return {"ativados": len(vigentes), "desativados": desativados}
