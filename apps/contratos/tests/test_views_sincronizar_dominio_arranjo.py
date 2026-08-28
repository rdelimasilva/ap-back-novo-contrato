import pytest
from django.test import Client

from apps.contratos import views
from apps.contratos.dominio_arranjo_repository import CODIGOS_ARRANJO_VIGENTES
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/jobs/sincronizar-dominio-arranjo"


def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    for codigo in CODIGOS_ARRANJO_VIGENTES:
        db.table("dominio_arranjo").delete().eq("codigo", codigo).execute()


@pytest.fixture(autouse=True)
def _oidc_ok(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)


def test_sincronizar_dominio_arranjo_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL)
    assert response.status_code == 401


def test_sincronizar_dominio_arranjo_ativa_todos_os_codigos_vigentes_por_tenant():
    _limpar()
    try:
        response = Client().post(URL)
        assert response.status_code == 200

        body = response.json()
        assert body["tenants"][FINANCIADOR_TESTE]["ativados"] == len(CODIGOS_ARRANJO_VIGENTES)

        db = get_db(FINANCIADOR_TESTE)
        ativos = db.table("dominio_arranjo").select("codigo").eq("ativo", True).execute()
        codigos_ativos = {row["codigo"] for row in ativos.data}
        assert set(CODIGOS_ARRANJO_VIGENTES).issubset(codigos_ativos)
    finally:
        _limpar()


def test_sincronizar_dominio_arranjo_isola_erro_por_tenant(monkeypatch):
    def _explode(financiador_id, codigos_vigentes):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(views, "sincronizar_arranjos", _explode)

    response = Client().post(URL)

    assert response.status_code == 200
    body = response.json()
    assert body["tenants"][FINANCIADOR_TESTE] == {"erro": "falha ao sincronizar"}
