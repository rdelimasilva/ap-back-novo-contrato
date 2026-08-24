import os

import pytest

from shared.cloudsql_client import get_db

pytestmark = pytest.mark.skipif(
    os.getenv("ENVIRONMENT") == "production",
    reason="testes gravam/apagam dados reais — nunca rodar contra produção",
)

TEST_CODIGO = "__TEST_ZZZ__"


def _cleanup():
    db = get_db()
    db.table("dominio_arranjo").delete().eq("codigo", TEST_CODIGO).execute()


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


def test_insert_select_update_delete_round_trip():
    db = get_db()

    inserted = db.table("dominio_arranjo").insert({
        "codigo": TEST_CODIGO,
        "descricao": "Arranjo de teste",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }).execute()
    assert inserted.data[0]["codigo"] == TEST_CODIGO

    found = db.table("dominio_arranjo").select("*").eq("codigo", TEST_CODIGO).execute()
    assert len(found.data) == 1
    assert found.data[0]["ativo"] is True

    updated = db.table("dominio_arranjo").update({"ativo": False}).eq("codigo", TEST_CODIGO).execute()
    assert updated.data[0]["ativo"] is False

    deleted = db.table("dominio_arranjo").delete().eq("codigo", TEST_CODIGO).execute()
    assert len(deleted.data) == 1

    empty = db.table("dominio_arranjo").select("*").eq("codigo", TEST_CODIGO).execute()
    assert empty.data == []


def test_upsert_inserts_then_updates_in_place():
    db = get_db()

    first = db.table("dominio_arranjo").upsert({
        "codigo": TEST_CODIGO,
        "descricao": "Arranjo de teste",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert first.data[0]["descricao"] == "Arranjo de teste"

    second = db.table("dominio_arranjo").upsert({
        "codigo": TEST_CODIGO,
        "descricao": "Arranjo de teste atualizado",
        "ativo": False,
        "atualizado_em": "2026-08-20T00:00:00-03:00",
    }, on_conflict="codigo").execute()
    assert second.data[0]["descricao"] == "Arranjo de teste atualizado"
    assert second.data[0]["ativo"] is False

    rows = db.table("dominio_arranjo").select("*").eq("codigo", TEST_CODIGO).execute()
    assert len(rows.data) == 1


def test_delete_without_filter_raises():
    db = get_db()
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").delete().execute()


def test_update_without_filter_raises():
    db = get_db()
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").update({"ativo": False}).execute()
