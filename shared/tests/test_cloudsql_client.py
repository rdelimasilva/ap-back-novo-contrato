from shared.cloudsql_client import get_db


def _cleanup():
    db = get_db()
    db.table("dominio_arranjo").delete().eq("codigo", "VCC").execute()


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


def test_insert_select_update_delete_round_trip():
    db = get_db()

    inserted = db.table("dominio_arranjo").insert({
        "codigo": "VCC",
        "descricao": "Visa Crédito",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }).execute()
    assert inserted.data[0]["codigo"] == "VCC"

    found = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert len(found.data) == 1
    assert found.data[0]["ativo"] is True

    updated = db.table("dominio_arranjo").update({"ativo": False}).eq("codigo", "VCC").execute()
    assert updated.data[0]["ativo"] is False

    deleted = db.table("dominio_arranjo").delete().eq("codigo", "VCC").execute()
    assert len(deleted.data) == 1

    empty = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert empty.data == []


def test_upsert_inserts_then_updates_in_place():
    db = get_db()

    first = db.table("dominio_arranjo").upsert({
        "codigo": "VCC",
        "descricao": "Visa Crédito",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert first.data[0]["descricao"] == "Visa Crédito"

    second = db.table("dominio_arranjo").upsert({
        "codigo": "VCC",
        "descricao": "Visa Crédito Atualizado",
        "ativo": False,
        "atualizado_em": "2026-08-20T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert second.data[0]["descricao"] == "Visa Crédito Atualizado"
    assert second.data[0]["ativo"] is False

    rows = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert len(rows.data) == 1
