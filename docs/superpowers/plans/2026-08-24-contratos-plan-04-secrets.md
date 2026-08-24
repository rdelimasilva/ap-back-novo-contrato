# contratos-service — Plan 04: Secrets Reader — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One function to read a secret (like the CERC `client_secret`) — from Google Secret Manager in deployed environments, from an env var in local dev — so no secret ever needs to be committed in plaintext.

**Architecture:** `shared/secrets.py`, branching on whether `GOOGLE_CLOUD_PROJECT` is set. Copied verbatim from `ap-back-optin/optin/shared/secrets.py` — this logic is project/secret-name agnostic, nothing about it is optin-specific.

**Tech Stack:** google-cloud-secret-manager (already in `requirements.txt` from Plan 01), pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-contratos-service-design.md` (§4, §5). Series: plan 4 of ~10.

**Depends on:** `2026-08-24-contratos-plan-01-scaffold.md` (repo layout).

## Global Constraints

- `client_secret` and other secrets are **never** logged or committed in plaintext (SPEC-02 §3, reusing SPEC-01 §3 by reference). `.env` (local, git-ignored, already has real values from Plan 02) holds real values for dev; `.env.example` holds only keys.

---

### Task 1: `shared/secrets.py`

**Files:**
- Create: `contratos/shared/secrets.py`
- Test: `contratos/shared/tests/test_secrets.py`

**Interfaces:**
- Produces: `get_secret(name: str) -> str`. Plan 05 (`token_provider`) reads `CERC_CLIENT_SECRET` through this.

- [ ] **Step 1: Write the failing test**

```python
# contratos/shared/tests/test_secrets.py
from shared.secrets import get_secret
import pytest


def test_get_secret_reads_env_var_when_no_gcp_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("MY_SECRET", "valor-local")
    assert get_secret("MY_SECRET") == "valor-local"


def test_get_secret_raises_when_missing_locally(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("NAO_EXISTE", raising=False)
    with pytest.raises(RuntimeError):
        get_secret("NAO_EXISTE")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest shared/tests/test_secrets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'shared.secrets'`

- [ ] **Step 3: Write `shared/secrets.py`**

```python
"""Leitura de segredos — Secret Manager em produção/homolog, env var em dev local.

Dev local: sem GOOGLE_CLOUD_PROJECT setado, lê a env var com o mesmo nome do
segredo (ex.: CERC_CLIENT_SECRET no .env). Em produção/homolog, lê do Secret
Manager do projeto (versão "latest").
"""

import os


def get_secret(name: str) -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Secret '{name}' não configurado (defina a env var localmente)")
        return value

    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project}/secrets/{name}/versions/latest"
    response = client.access_secret_version(name=path)
    return response.payload.data.decode("utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest shared/tests/test_secrets.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add shared/secrets.py shared/tests/test_secrets.py
git commit -m "feat: secrets reader (Secret Manager / env var fallback)"
```

---

## Self-Review Notes

- **Spec coverage:** design §4/§5 (secret handling) — covered for the local-fallback path. The Secret Manager branch is exercised functionally only against real GCP credentials in homolog/prod, consistent with certification being a later deployment gate, not a unit-test concern.
- **Placeholder scan:** none.
- **Type consistency:** `get_secret(name: str) -> str` is the exact signature Plan 05 (`token_provider`) calls for `CERC_CLIENT_SECRET`.

**Next:** `2026-08-24-contratos-plan-05-token-provider.md` (CERC OAuth2 token provider).
