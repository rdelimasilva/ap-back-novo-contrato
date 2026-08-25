import json

import pytest

import shared.pubsub_client as pubsub_client


class _FakeFuture:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def add_done_callback(self, callback):
        callback(self)

    def result(self):
        if self._error:
            raise self._error
        return self._result


class _FakePublisher:
    def __init__(self, future):
        self._future = future
        self.calls = []

    def publish(self, topic, data):
        self.calls.append((topic, data))
        return self._future


@pytest.fixture(autouse=True)
def _reset_publisher_singleton():
    pubsub_client._publisher = None
    yield
    pubsub_client._publisher = None


def test_publish_webhook_contrato_sends_ids_as_json_to_default_topic(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "registradora-506000")
    monkeypatch.delenv("PUBSUB_TOPIC_CONTRATOS_WEBHOOK", raising=False)
    fake = _FakePublisher(_FakeFuture(result="msg-1"))
    monkeypatch.setattr(pubsub_client, "_get_publisher", lambda: fake)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")

    assert len(fake.calls) == 1
    topic, data = fake.calls[0]
    assert topic == "projects/registradora-506000/topics/contratos-webhook-inbox"
    assert json.loads(data) == {"webhook_inbox_id": "01ABC", "financiador_id": "12345678000199"}


def test_publish_webhook_contrato_respects_custom_topic_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "registradora-506000")
    monkeypatch.setenv("PUBSUB_TOPIC_CONTRATOS_WEBHOOK", "topico-customizado")
    fake = _FakePublisher(_FakeFuture(result="msg-1"))
    monkeypatch.setattr(pubsub_client, "_get_publisher", lambda: fake)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")

    assert fake.calls[0][0] == "projects/registradora-506000/topics/topico-customizado"


def test_publish_webhook_contrato_nao_levanta_quando_publish_falha_de_forma_assincrona(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "registradora-506000")
    fake = _FakePublisher(_FakeFuture(error=RuntimeError("indisponível")))
    monkeypatch.setattr(pubsub_client, "_get_publisher", lambda: fake)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")  # não deve levantar


def test_publish_webhook_contrato_nao_levanta_quando_google_cloud_project_ausente(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

    pubsub_client.publish_webhook_contrato("01ABC", "12345678000199")  # não deve levantar
