import shared.pubsub_auth as pubsub_auth


class _FakeRequest:
    def __init__(self, auth_header=None):
        self.META = {}
        if auth_header is not None:
            self.META["HTTP_AUTHORIZATION"] = auth_header


def test_verificar_push_oidc_sem_header_retorna_false():
    assert pubsub_auth.verificar_push_oidc(_FakeRequest()) is False


def test_verificar_push_oidc_header_sem_bearer_retorna_false():
    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Basic xxx")) is False


def test_verificar_push_oidc_sem_conta_esperada_configurada_retorna_false(monkeypatch):
    # PUBSUB_PUSH_INVOKER_SA é obrigatório — a claim `audience` de um ID token
    # OIDC do Google não é, por si só, uma fronteira de autenticação (qualquer
    # identidade Google autenticada pode pedir um token para qualquer
    # audiência), então sem a conta esperada configurada a requisição deve
    # ser recusada, não aceita "por padrão" (mesmo princípio de _autenticado
    # em apps/contratos/views.py: config vazia/ausente nunca autentica qualquer um).
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")
    monkeypatch.delenv("PUBSUB_PUSH_INVOKER_SA", raising=False)
    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", lambda token, audiencia: {"email": "pubsub-push@proj.iam.gserviceaccount.com"})

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is False


def test_verificar_push_oidc_token_valido_mas_conta_diferente_da_esperada_retorna_false(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")
    monkeypatch.setenv("PUBSUB_PUSH_INVOKER_SA", "pubsub-push@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", lambda token, audiencia: {"email": "outra-conta@proj.iam.gserviceaccount.com"})

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is False


def test_verificar_push_oidc_token_valido_e_conta_esperada_bate_retorna_true(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")
    monkeypatch.setenv("PUBSUB_PUSH_INVOKER_SA", "pubsub-push@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", lambda token, audiencia: {"email": "pubsub-push@proj.iam.gserviceaccount.com"})

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is True


def test_verificar_push_oidc_token_invalido_retorna_false(monkeypatch):
    monkeypatch.setenv("PUBSUB_PUSH_AUDIENCE", "https://contratos.example.com/api/v1/webhooks/contrato/processar")

    def _falha(token, audiencia):
        raise ValueError("token expirado")

    monkeypatch.setattr(pubsub_auth, "_verificar_id_token", _falha)

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is False


def test_verificar_push_oidc_sem_audiencia_configurada_retorna_false(monkeypatch):
    monkeypatch.delenv("PUBSUB_PUSH_AUDIENCE", raising=False)

    assert pubsub_auth.verificar_push_oidc(_FakeRequest("Bearer tok-1")) is False
