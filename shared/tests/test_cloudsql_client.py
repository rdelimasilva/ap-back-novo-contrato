import os
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ENVIRONMENT") == "production",
    reason="testes gravam/apagam dados reais — nunca rodar contra produção",
)

TEST_CODIGO = "__TEST_ZZZ__"
FINANCIADOR_TESTE = "12345678000199"
FINANCIADOR_TESTE_2 = "99999999000191"
FINANCIADOR_TESTE_3 = "11111111000100"

from shared.cloudsql_client import get_db  # noqa: E402
import shared.cloudsql_client as cloudsql_client_module  # noqa: E402


def _cleanup():
    db = get_db(FINANCIADOR_TESTE)
    db.table("dominio_arranjo").delete().eq("codigo", TEST_CODIGO).execute()


def setup_function(_):
    _cleanup()


def teardown_function(_):
    _cleanup()


def test_insert_select_update_delete_round_trip():
    db = get_db(FINANCIADOR_TESTE)

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
    db = get_db(FINANCIADOR_TESTE)

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
    db = get_db(FINANCIADOR_TESTE)
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").delete().execute()


def test_update_without_filter_raises():
    db = get_db(FINANCIADOR_TESTE)
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").update({"ativo": False}).execute()


def test_get_db_cacheia_por_financiador_id(monkeypatch):
    cloudsql_client_module._clients.clear()
    # Aponta o "segundo tenant" para a MESMA config do tenant de teste — o
    # objetivo aqui é provar que o cache é chaveado por financiador_id (dois
    # tenants diferentes nunca compartilham o mesmo CloudSQLClient), não
    # provisionar um segundo Cloud SQL real só para este teste.
    monkeypatch.setenv(
        f"TENANT_{FINANCIADOR_TESTE_2}_CONFIG_CONTRATOS",
        os.environ[f"TENANT_{FINANCIADOR_TESTE}_CONFIG_CONTRATOS"],
    )

    db1a = get_db(FINANCIADOR_TESTE)
    db1b = get_db(FINANCIADOR_TESTE)
    db2 = get_db(FINANCIADOR_TESTE_2)

    assert db1a is db1b
    assert db1a is not db2

    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_2, None)


def test_get_db_single_flight_on_concurrent_first_access(monkeypatch):
    # Reproduz o bug real que o ap-back-optin encontrou e corrigiu: duas
    # (aqui, dez) threads chamando get_db() pela primeira vez para o MESMO
    # financiador_id ainda não cacheado, ao mesmo tempo. Sem o lock por
    # tenant, cada uma chamaria _create_engine (engine + connector reais) e
    # a perdedora vazaria um pool de conexões nunca fechado. Trocamos
    # _create_engine por um fake lento pra alargar a janela de corrida e
    # contamos quantas vezes ele é de fato chamado.
    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_3, None)
    cloudsql_client_module._locks.pop(FINANCIADOR_TESTE_3, None)
    monkeypatch.setenv(
        f"TENANT_{FINANCIADOR_TESTE_3}_CONFIG_CONTRATOS",
        os.environ[f"TENANT_{FINANCIADOR_TESTE}_CONFIG_CONTRATOS"],
    )

    call_count = 0
    count_lock = threading.Lock()

    def _slow_fake_engine(config):
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.05)  # alarga a janela pra forçar a corrida
        return object()

    monkeypatch.setattr(cloudsql_client_module, "_create_engine", _slow_fake_engine)

    results = []

    def _call():
        results.append(get_db(FINANCIADOR_TESTE_3))

    threads = [threading.Thread(target=_call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 1  # engine construído uma única vez
    assert len({id(r) for r in results}) == 1  # todas as threads recebem o mesmo client

    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_3, None)
    cloudsql_client_module._locks.pop(FINANCIADOR_TESTE_3, None)
