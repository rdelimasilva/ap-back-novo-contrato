from apps.contratos.webhook_dedupe import hash_evento


def test_hash_evento_e_deterministico():
    evento = {"referenciaExterna": "CTR-1", "protocolo": "P-1", "status": "0"}
    assert hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z") == \
        hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z")


def test_hash_evento_tem_64_caracteres_hex():
    h = hash_evento("contrato", {"a": 1}, "2026-08-17T12:00:00.000Z")
    assert len(h) == 64
    int(h, 16)  # não levanta — é hex válido


def test_hash_evento_ignora_ordem_das_chaves():
    e1 = {"a": 1, "b": 2}
    e2 = {"b": 2, "a": 1}
    assert hash_evento("contrato", e1, "2026-08-17T12:00:00.000Z") == \
        hash_evento("contrato", e2, "2026-08-17T12:00:00.000Z")


def test_hash_evento_muda_com_tipo_evento_diferente():
    evento = {"a": 1}
    assert hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z") != \
        hash_evento("efeitoContrato", evento, "2026-08-17T12:00:00.000Z")


def test_hash_evento_muda_com_data_hora_diferente():
    evento = {"a": 1}
    assert hash_evento("contrato", evento, "2026-08-17T12:00:00.000Z") != \
        hash_evento("contrato", evento, "2026-08-17T12:00:01.000Z")


def test_hash_evento_muda_com_conteudo_diferente():
    assert hash_evento("contrato", {"a": 1}, "2026-08-17T12:00:00.000Z") != \
        hash_evento("contrato", {"a": 2}, "2026-08-17T12:00:00.000Z")
