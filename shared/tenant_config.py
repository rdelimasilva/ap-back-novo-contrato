"""Configuração por tenant (financiador) para o serviço de contratos.

Um segredo por tenant (TENANT_{financiador_id}_CONFIG_CONTRATOS, JSON) via
shared.secrets.get_secret — dev local lê a env var de mesmo nome (sem
GOOGLE_CLOUD_PROJECT); produção/homolog lê do Secret Manager, um segredo
por tenant. Cacheado em memória por processo, sem TTL (mesma filosofia do
cache de token de services/cerc/token_provider.py).

Nome de segredo com sufixo _CONTRATOS: deliberadamente diferente do
TENANT_{financiador_id}_CONFIG do ap-back-optin — cada serviço tem seu
próprio segredo (mesmo raciocínio de "cada serviço com suas próprias
credenciais CERC" já adotado), evitando colisão de nome no Secret Manager.

Chaves esperadas no JSON: cloudsql_connection_name, cloudsql_db_user,
cloudsql_db_password, cloudsql_db_name, cloudsql_ip_type (opcional,
default "PUBLIC"), cerc_client_id, cerc_client_secret.

Ver docs/superpowers/specs/2026-08-24-contratos-service-design.md §1.1.
"""
import json

from shared.secrets import get_secret

_cache: dict = {}


def get_tenant_config(financiador_id: str) -> dict:
    if financiador_id in _cache:
        return _cache[financiador_id]

    raw = get_secret(f"TENANT_{financiador_id}_CONFIG_CONTRATOS")
    config = json.loads(raw)
    _cache[financiador_id] = config
    return config
