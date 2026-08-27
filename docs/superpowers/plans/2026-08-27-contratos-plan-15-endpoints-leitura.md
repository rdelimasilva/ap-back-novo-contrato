# Endpoints de leitura de contratos (GET) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar `GET /api/v1/contratos/<financiador_id>` (lista) e `GET /api/v1/contratos/<financiador_id>/<id>` (detalhe), os únicos endpoints de leitura que faltam pra fechar o loop criar→acompanhar que o front (`ap-front`) precisa consumir.

**Architecture:** Segue exatamente os padrões já estabelecidos em `apps/contratos/contrato_repository.py` (funções puras de acesso a dado via `get_db(financiador_id).table(...)`) e `apps/contratos/views.py` (view monta um DTO camelCase a partir da linha snake_case do banco). A URL de listagem reaproveita o mesmo path de `criar_contrato` (`/contratos/<financiador_id>`) — um dispatcher novo escolhe entre `criar_contrato` (POST, já existe, não muda) e a nova `listar_contratos` (GET) pelo `request.method`, exatamente como REST trata uma coleção. A URL de detalhe é nova (`/contratos/<financiador_id>/<id>`).

**Tech Stack:** Django (function-based views, sem DRF), SQLAlchemy via `shared/cloudsql_client.py` (API estilo PostgREST), pytest + `django.test.Client` + `respx` (testes de integração contra banco real do tenant de teste).

**Spec:** `C:\DEV\ap\ap-front\docs\superpowers\specs\2026-08-27-contratos-cerc-integracao-frontend-design.md` §2 (repo irmão — a spec cobre os dois lados da integração; este plano implementa só a metade do backend).

## Global Constraints

- `JsonResponse` do Django usa `DjangoJSONEncoder` por padrão — serializa `Decimal`, `date`/`datetime` e `uuid.UUID` sozinho. **Não** escrever conversão manual desses tipos nas views novas.
- Isolamento de tenant é estrutural: `get_db(financiador_id)` conecta num Cloud SQL **por tenant** (não é uma coluna filtrada) — nenhuma query nova precisa (nem deve) filtrar por `cnpj_participante` manualmente.
- Todo DTO de resposta é **camelCase**; toda coluna de banco é **snake_case**. A conversão acontece na view, nunca no repository (repository sempre devolve a linha crua do banco — mesmo padrão de `buscar_contrato_por_referencia`/`inserir_contrato_criado`).
- Nomes de função do repository não podem colidir com nomes de view (mesmo módulo `apps.contratos`, importados um no outro) — repository usa `listar_contratos_do_financiador`/`buscar_contrato_detalhado`; view usa `listar_contratos`/`detalhar_contrato`.
- `FINANCIADOR_TESTE = "12345678000199"` é o tenant de teste já configurado em `.env` (`TENANT_12345678000199_CONFIG_CONTRATOS`) — reusar esse valor em todo teste novo, nunca inventar outro.

---

### Task 1: `GET /api/v1/contratos/<financiador_id>` — lista

**Files:**
- Modify: `apps/contratos/contrato_repository.py` (adicionar `listar_contratos_do_financiador`)
- Modify: `apps/contratos/views.py` (adicionar `listar_contratos`, `contratos` dispatcher, helper `_contrato_para_dto`)
- Modify: `apps/contratos/urls.py:9` (trocar `views.criar_contrato` por `views.contratos` na rota de coleção)
- Modify: `apps/contratos/tests/test_views_criar_contrato.py` (o teste `test_criar_contrato_get_retorna_405` fica obsoleto — GET nesta URL agora é válido)
- Test: `apps/contratos/tests/test_views_listar_contratos.py` (novo arquivo)

**Interfaces:**
- Produces: `listar_contratos_do_financiador(financiador_id: str, status: str | None = None, limit: int | None = None) -> list[dict]` (linhas cruas de `contrato`, snake_case, mais recente primeiro por `enviado_em`)
- Produces: `_contrato_para_dto(c: dict) -> dict` em `views.py` (usada também pela Task 2 — não remover/renomear sem checar a Task 2)
- Consumes: `get_db` de `shared/cloudsql_client.py` (já existe)

- [ ] **Step 1: Escrever o teste de lista vazia (falha esperada — endpoint não existe ainda)**

Criar `apps/contratos/tests/test_views_listar_contratos.py`:

```python
from django.test import Client

from apps.contratos.contrato_repository import buscar_contrato_por_referencia, inserir_contrato_criado, remover_contrato_rejeitado
from apps.contratos import state_machine
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL_LISTA = f"/api/v1/contratos/{FINANCIADOR_TESTE}"


def _payload_minimo(referencia_externa):
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-LISTA",
        "documentoContratante": "22751826000125",
        "cnpjDetentor": FINANCIADOR_TESTE,
        "tipoEfeito": "2",
        "saldoDevedor": 150000.00,
        "limiteOperacaoGarantida": 200000.00,
        "valorMantido": 180000.00,
        "dataAssinatura": "2026-08-15",
        "dataVencimento": "2027-08-15",
        "identificacaoGestaoEntidadeRegistradora": "2",
        "modalidadeOperacao": "1",
        "repactuacao": "0",
    }


def _limpar(referencia_externa):
    db = get_db(FINANCIADOR_TESTE)
    existente = db.table("contrato").select("id").eq("referencia_externa", referencia_externa).execute()
    for row in existente.data:
        contrato_id = row["id"]
        for g in db.table("garantia").select("id").eq("contrato_id", contrato_id).execute().data:
            db.table("garantia_ur").delete().eq("garantia_id", g["id"]).execute()
        db.table("garantia").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_domicilio").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_parcela").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_contrato_anterior").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato").delete().eq("id", contrato_id).execute()


def test_listar_contratos_retorna_dados_como_lista():
    response = Client().get(URL_LISTA)
    assert response.status_code == 200
    corpo = response.json()
    assert isinstance(corpo["dados"], list)


def test_listar_contratos_inclui_contrato_recem_criado():
    referencia_externa = "CTR-TESTE-LISTA-1"
    _limpar(referencia_externa)
    try:
        payload_validado = {**_payload_minimo(referencia_externa), "garantias": [], "identificacaoContratosAnteriores": [], "parcelas": []}
        inserir_contrato_criado(
            FINANCIADOR_TESTE, payload_validado, status=state_machine.AGUARDANDO_WEBHOOK,
            protocolo="proto-lista-1", id_contrato_cerc="cerc-lista-1",
        )

        response = Client().get(URL_LISTA)
        assert response.status_code == 200
        dados = response.json()["dados"]
        encontrado = next((c for c in dados if c["referenciaExterna"] == referencia_externa), None)
        assert encontrado is not None
        assert encontrado["status"] == state_machine.AGUARDANDO_WEBHOOK
        assert encontrado["protocolo"] == "proto-lista-1"
        assert encontrado["saldoDevedor"] == 150000.00
    finally:
        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        if contrato:
            remover_contrato_rejeitado(FINANCIADOR_TESTE, contrato["id"])


def test_listar_contratos_filtro_status_exclui_outros_status():
    referencia_externa = "CTR-TESTE-LISTA-FILTRO"
    _limpar(referencia_externa)
    try:
        payload_validado = {**_payload_minimo(referencia_externa), "garantias": [], "identificacaoContratosAnteriores": [], "parcelas": []}
        inserir_contrato_criado(
            FINANCIADOR_TESTE, payload_validado, status=state_machine.REJEITADO_ESTRUTURAL,
            protocolo=None, id_contrato_cerc=None,
        )

        response = Client().get(f"{URL_LISTA}?status=REGISTRADO")
        assert response.status_code == 200
        referencias = [c["referenciaExterna"] for c in response.json()["dados"]]
        assert referencia_externa not in referencias
    finally:
        contrato = buscar_contrato_por_referencia(FINANCIADOR_TESTE, referencia_externa)
        if contrato:
            remover_contrato_rejeitado(FINANCIADOR_TESTE, contrato["id"])


def test_contratos_metodo_nao_suportado_retorna_405():
    response = Client().put(URL_LISTA, data="{}", content_type="application/json")
    assert response.status_code == 405
```

- [ ] **Step 2: Rodar os testes e confirmar que falham (404, endpoint não existe)**

Run: `cd apps/.. && python -m pytest apps/contratos/tests/test_views_listar_contratos.py -v` (a partir de `C:\DEV\ap\ap-back-contratos\contratos`)
Expected: FAIL — `test_listar_contratos_retorna_dados_como_lista` falha com `status_code == 404` (URL não roteada pra GET) ou erro de import.

- [ ] **Step 3: Adicionar `listar_contratos_do_financiador` ao repository**

Em `apps/contratos/contrato_repository.py`, adicionar ao final do arquivo:

```python
def listar_contratos_do_financiador(financiador_id: str, status: str | None = None, limit: int | None = None) -> list[dict]:
    """Lista as linhas de `contrato` do tenant, mais recente primeiro
    (`enviado_em` desc). Filtro opcional por `status` (SPEC-02 §8)."""
    query = get_db(financiador_id).table("contrato").select("*")
    if status:
        query = query.eq("status", status)
    query = query.order("enviado_em", desc=True)
    if limit:
        query = query.limit(limit)
    return query.execute().data
```

- [ ] **Step 4: Adicionar dispatcher + view de listagem + DTO helper em `views.py`**

No topo do import de `contrato_repository` (linha 13-18), adicionar `listar_contratos_do_financiador` à lista de nomes importados:

```python
from apps.contratos.contrato_repository import (
    atualizar_status_pos_registro,
    buscar_contrato_por_referencia,
    inserir_contrato_criado,
    listar_contratos_do_financiador,
    remover_contrato_rejeitado,
)
```

Adicionar `HttpResponseNotAllowed` ao import do `django.http` (linha 7):

```python
from django.http import HttpResponseNotAllowed, JsonResponse
```

Logo antes de `def criar_contrato(request, financiador_id: str):` (linha 331), adicionar o helper de DTO e o dispatcher:

```python
def _contrato_para_dto(c: dict) -> dict:
    """Linha crua de `contrato` (snake_case) -> DTO de resposta (camelCase).
    Compartilhado entre listar_contratos e detalhar_contrato — qualquer
    campo adicionado aqui aparece nos dois endpoints."""
    return {
        "id": c["id"],
        "referenciaExterna": c["referencia_externa"],
        "identificadorContrato": c["identificador_contrato"],
        "protocolo": c.get("protocolo_cerc"),
        "idContratoCerc": c.get("id_contrato_cerc"),
        "status": c["status"],
        "statusGarantia": c.get("status_garantia"),
        "cnpjParticipante": c["cnpj_participante"],
        "documentoContratante": c["documento_contratante"],
        "cnpjDetentor": c["cnpj_detentor"],
        "tipoEfeito": c["tipo_efeito"],
        "modalidadeOperacao": c["modalidade_operacao"],
        "gestaoEntidadeRegistradora": c["gestao_entidade_registradora"],
        "saldoDevedor": c["saldo_devedor"],
        "limiteOperacaoGarantida": c["limite_operacao_garantida"],
        "valorMantido": c["valor_mantido"],
        "dataAssinatura": c["data_assinatura"],
        "dataVencimento": c["data_vencimento"],
        "repactuacao": c["repactuacao"],
        "carteira": c.get("carteira"),
        "tipoAvaliacao": c.get("tipo_avaliacao"),
        "qtdUrsAlcancadas": c.get("qtd_urs_alcancadas"),
        "valorUrsAlcancadas": c.get("valor_urs_alcancadas"),
        "resultadoDistribuicao": c.get("resultado_distribuicao"),
        "indSobrecolateral": c.get("ind_sobrecolateral"),
        "criadoEm": c.get("enviado_em"),
        "confirmadoEm": c.get("confirmado_em"),
    }


def listar_contratos(request, financiador_id: str):
    """GET /api/v1/contratos/<financiador_id> — lista os contratos do
    financiador, mais recente primeiro. Filtros opcionais via querystring:
    ?status=, ?limit=."""
    status = request.GET.get("status") or None
    limit_param = request.GET.get("limit")
    limit = int(limit_param) if limit_param else None
    contratos = listar_contratos_do_financiador(financiador_id, status=status, limit=limit)
    return JsonResponse({"dados": [_contrato_para_dto(c) for c in contratos]})


def contratos(request, financiador_id: str):
    """Dispatcher da URL de coleção `/contratos/<financiador_id>`: POST cria
    (tipoOperacao=C, comportamento existente de `criar_contrato`), GET lista.
    Mesma URL para os dois verbos — convenção REST de coleção."""
    if request.method == "POST":
        return criar_contrato(request, financiador_id)
    if request.method == "GET":
        return listar_contratos(request, financiador_id)
    return HttpResponseNotAllowed(["GET", "POST"])
```

- [ ] **Step 5: Trocar a rota de coleção pra apontar pro dispatcher**

Em `apps/contratos/urls.py`, trocar a linha:

```python
re_path(r"^contratos/(?P<financiador_id>\d{14})$", views.criar_contrato),
```

por:

```python
re_path(r"^contratos/(?P<financiador_id>\d{14})$", views.contratos),
```

- [ ] **Step 6: Atualizar o teste obsoleto de 405 em `test_views_criar_contrato.py`**

Em `apps/contratos/tests/test_views_criar_contrato.py`, o teste:

```python
def test_criar_contrato_get_retorna_405():
    response = Client().get(URL)
    assert response.status_code == 405
```

fica **errado** (GET agora é válido nessa URL). Substituir por:

```python
def test_criar_contrato_get_retorna_lista_nao_405():
    response = Client().get(URL)
    assert response.status_code == 200
    assert "dados" in response.json()
```

- [ ] **Step 7: Rodar os testes da Task 1 e confirmar que passam**

Run: `cd C:\DEV\ap\ap-back-contratos\contratos && python -m pytest apps/contratos/tests/test_views_listar_contratos.py apps/contratos/tests/test_views_criar_contrato.py -v`
Expected: PASS — todos os testes verdes, incluindo o `test_criar_contrato_get_retorna_lista_nao_405` reescrito.

- [ ] **Step 8: Rodar a suíte inteira do app pra garantir que nada mais quebrou**

Run: `cd C:\DEV\ap\ap-back-contratos\contratos && python -m pytest apps/contratos -v`
Expected: PASS — nenhuma regressão nos testes de `criar_contrato`/`inativar_contrato`/`baixar_contrato`/webhook (o dispatcher não muda o comportamento de POST, só adiciona um branch de GET).

- [ ] **Step 9: Commit**

```bash
git add apps/contratos/contrato_repository.py apps/contratos/views.py apps/contratos/urls.py apps/contratos/tests/test_views_listar_contratos.py apps/contratos/tests/test_views_criar_contrato.py
git commit -m "feat: GET /api/v1/contratos/<financiador_id> — lista contratos do tenant"
```

---

### Task 2: `GET /api/v1/contratos/<financiador_id>/<id>` — detalhe

**Files:**
- Modify: `apps/contratos/contrato_repository.py` (adicionar `buscar_contrato_detalhado`)
- Modify: `apps/contratos/views.py` (adicionar `detalhar_contrato` + helpers `_garantia_para_dto`/`_ur_para_dto`/`_indicador_para_dto`)
- Modify: `apps/contratos/urls.py` (nova rota)
- Test: `apps/contratos/tests/test_views_detalhar_contrato.py` (novo arquivo)

**Interfaces:**
- Consumes: `_contrato_para_dto` da Task 1 (mesmo módulo `views.py`)
- Produces: `buscar_contrato_detalhado(financiador_id: str, contrato_id: str) -> dict | None` (linha de `contrato` + `garantias: list[dict]` (cada uma com `unidades_recebiveis: list[dict]`) + `indicadores_consistencia: list[dict]`, tudo snake_case)

- [ ] **Step 1: Escrever os testes (falha esperada — endpoint não existe ainda)**

Criar `apps/contratos/tests/test_views_detalhar_contrato.py`:

```python
from datetime import datetime, timezone

from django.test import Client

from apps.contratos import state_machine
from apps.contratos.contrato_repository import buscar_contrato_por_referencia, inserir_contrato_criado, remover_contrato_rejeitado
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"


def _payload_validado(referencia_externa):
    return {
        "referenciaExterna": referencia_externa,
        "identificadorContrato": "OP-TESTE-DETALHE",
        "documentoContratante": "22751826000125",
        "cnpjDetentor": FINANCIADOR_TESTE,
        "tipoEfeito": "2",
        "saldoDevedor": 150000.00,
        "limiteOperacaoGarantida": 200000.00,
        "valorMantido": 180000.00,
        "dataAssinatura": "2026-08-15",
        "dataVencimento": "2027-08-15",
        "identificacaoGestaoEntidadeRegistradora": "2",
        "modalidadeOperacao": "1",
        "repactuacao": "0",
        "identificacaoContratosAnteriores": [],
        "parcelas": [],
        "garantias": [{
            "referenciaExterna": f"{referencia_externa}-G1",
            "regrasDivisao": "1",
            "valorAOnerar": 180000.00,
            "tipoDistribuicao": None,
            "definicaoUnidadeRecebivel": {
                "listaCnpjCredenciadora": ["99T"],
                "listaCodigoArranjoPagamento": ["99T"],
                "documentoUsuarioFinalRecebedor": "22751826000125",
                "documentoTitular": "22751826000125",
                "dataInicio": "2026-08-26",
                "dataFim": "2027-08-15",
            },
        }],
    }


def _limpar(referencia_externa):
    db = get_db(FINANCIADOR_TESTE)
    existente = db.table("contrato").select("id").eq("referencia_externa", referencia_externa).execute()
    for row in existente.data:
        contrato_id = row["id"]
        for g in db.table("garantia").select("id").eq("contrato_id", contrato_id).execute().data:
            db.table("garantia_ur").delete().eq("garantia_id", g["id"]).execute()
        db.table("garantia").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_domicilio").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_parcela").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_contrato_anterior").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato_evento").delete().eq("contrato_id", contrato_id).execute()
        db.table("contrato").delete().eq("id", contrato_id).execute()


def test_detalhar_contrato_inexistente_retorna_404():
    response = Client().get(f"/api/v1/contratos/{FINANCIADOR_TESTE}/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


def test_detalhar_contrato_traz_garantias_e_urs():
    referencia_externa = "CTR-TESTE-DETALHE-1"
    _limpar(referencia_externa)
    try:
        payload_validado = _payload_validado(referencia_externa)
        contrato = inserir_contrato_criado(
            FINANCIADOR_TESTE, payload_validado, status=state_machine.REGISTRADO,
            protocolo="proto-detalhe-1", id_contrato_cerc="cerc-detalhe-1",
        )

        garantia_id = get_db(FINANCIADOR_TESTE).table("garantia").select("id").eq("contrato_id", contrato["id"]).execute().data[0]["id"]
        get_db(FINANCIADOR_TESTE).table("garantia_ur").insert({
            "garantia_id": garantia_id, "cnpj_credenciadora": "11111111000111",
            "documento_ufr": "22751826000125", "documento_titular": "22751826000125",
            "codigo_arranjo": "VCC", "data_liquidacao": "2026-09-15", "constituicao": "1",
            "valor_constituido_total": 5000.00, "valor_bloqueado": 0.00,
            "indicador_oneracao": "1", "regras_divisao": "1",
            "valor_onerado": 5000.00, "valor_constituido_efeito": 5000.00,
            "origem": "WEBHOOK", "snapshot_em": datetime.now(timezone.utc),
        }).execute()

        response = Client().get(f"/api/v1/contratos/{FINANCIADOR_TESTE}/{contrato['id']}")
        assert response.status_code == 200
        corpo = response.json()
        assert corpo["referenciaExterna"] == referencia_externa
        assert corpo["status"] == state_machine.REGISTRADO
        assert len(corpo["garantias"]) == 1
        assert corpo["garantias"][0]["referenciaExterna"] == f"{referencia_externa}-G1"
        assert len(corpo["garantias"][0]["unidadesRecebiveisAlcancadas"]) == 1
        assert corpo["garantias"][0]["unidadesRecebiveisAlcancadas"][0]["cnpjCredenciadora"] == "11111111000111"
        assert corpo["indicadoresConsistencia"] == []
    finally:
        _limpar(referencia_externa)
```

- [ ] **Step 2: Rodar e confirmar que falham**

Run: `cd C:\DEV\ap\ap-back-contratos\contratos && python -m pytest apps/contratos/tests/test_views_detalhar_contrato.py -v`
Expected: FAIL — 404 pra URL não roteada (nenhuma rota `/contratos/<financiador_id>/<id>` existe ainda).

- [ ] **Step 3: Adicionar `buscar_contrato_detalhado` ao repository**

Em `apps/contratos/contrato_repository.py`, adicionar ao final:

```python
def buscar_contrato_detalhado(financiador_id: str, contrato_id: str) -> dict | None:
    """Linha de `contrato` + garantias (cada uma com as URs alcançadas
    persistidas em `garantia_ur`) + indicadores de consistência. Tudo
    snake_case — a conversão pra DTO camelCase é responsabilidade da view
    (mesmo padrão do resto deste módulo)."""
    db = get_db(financiador_id)
    resultado = db.table("contrato").select("*").eq("id", contrato_id).execute()
    if not resultado.data:
        return None
    contrato = resultado.data[0]

    garantias = []
    for g in db.table("garantia").select("*").eq("contrato_id", contrato_id).execute().data:
        urs = db.table("garantia_ur").select("*").eq("garantia_id", g["id"]).execute().data
        garantias.append({**g, "unidades_recebiveis": urs})

    indicadores = db.table("indicador_consistencia").select("*").eq("contrato_id", contrato_id).execute().data

    return {**contrato, "garantias": garantias, "indicadores_consistencia": indicadores}
```

- [ ] **Step 4: Adicionar view + DTO helpers em `views.py`**

Adicionar `buscar_contrato_detalhado` ao import de `contrato_repository` (junto com `listar_contratos_do_financiador` da Task 1):

```python
from apps.contratos.contrato_repository import (
    atualizar_status_pos_registro,
    buscar_contrato_detalhado,
    buscar_contrato_por_referencia,
    inserir_contrato_criado,
    listar_contratos_do_financiador,
    remover_contrato_rejeitado,
)
```

Adicionar `require_GET` ao import de `django.views.decorators.http` (junto com `require_POST`):

```python
from django.views.decorators.http import require_GET, require_POST
```

Logo depois de `_contrato_para_dto` (Task 1), adicionar:

```python
def _ur_para_dto(ur: dict) -> dict:
    return {
        "cnpjCredenciadora": ur.get("cnpj_credenciadora"),
        "documentoUsuarioFinalRecebedor": ur.get("documento_ufr"),
        "documentoTitular": ur.get("documento_titular"),
        "codigoArranjoPagamento": ur.get("codigo_arranjo"),
        "dataLiquidacao": ur.get("data_liquidacao"),
        "constituicao": ur.get("constituicao"),
        "valorConstituidoTotal": ur.get("valor_constituido_total"),
        "valorBloqueado": ur.get("valor_bloqueado"),
        "indicadorOneracao": ur.get("indicador_oneracao"),
        "regrasDivisao": ur.get("regras_divisao"),
        "valorOnerado": ur.get("valor_onerado"),
        "valorConstituidoEfeito": ur.get("valor_constituido_efeito"),
        "origem": ur.get("origem"),
    }


def _garantia_para_dto(g: dict) -> dict:
    return {
        "id": g["id"],
        "referenciaExterna": g["referencia_externa"],
        "regrasDivisao": g["regras_divisao"],
        "valorAOnerar": g["valor_a_onerar"],
        "tipoDistribuicao": g.get("tipo_distribuicao"),
        "definicaoUnidadeRecebivel": {
            "listaCnpjCredenciadora": g["def_lista_credenciadoras"],
            "listaCodigoArranjoPagamento": g["def_lista_arranjos"],
            "documentoUsuarioFinalRecebedor": g.get("def_documento_ufr"),
            "documentoTitular": g.get("def_documento_titular"),
            "dataInicio": g["def_data_inicio"],
            "dataFim": g["def_data_fim"],
        },
        "unidadesRecebiveisAlcancadas": [_ur_para_dto(ur) for ur in g.get("unidades_recebiveis", [])],
    }


def _indicador_para_dto(i: dict) -> dict:
    return {
        "indicador": i["indicador"],
        "resultado": i.get("resultado"),
        "parametros": i.get("parametros"),
        "criticidade": i.get("criticidade"),
        "observadoEm": i.get("observado_em"),
    }


@require_GET
def detalhar_contrato(request, financiador_id: str, contrato_id: str):
    """GET /api/v1/contratos/<financiador_id>/<id> — detalhe de um
    contrato: dados do contrato + garantias (com URs alcançadas) +
    indicadores de consistência."""
    detalhe = buscar_contrato_detalhado(financiador_id, contrato_id)
    if detalhe is None:
        return JsonResponse({"erro": "contrato não encontrado"}, status=404)

    corpo = _contrato_para_dto(detalhe)
    corpo["garantias"] = [_garantia_para_dto(g) for g in detalhe["garantias"]]
    corpo["indicadoresConsistencia"] = [_indicador_para_dto(i) for i in detalhe["indicadores_consistencia"]]
    return JsonResponse(corpo)
```

- [ ] **Step 5: Adicionar a rota nova em `urls.py`**

Em `apps/contratos/urls.py`, adicionar logo depois da rota de coleção:

```python
re_path(r"^contratos/(?P<financiador_id>\d{14})/(?P<contrato_id>[0-9a-f-]{36})$", views.detalhar_contrato),
```

(a ordem importa: esta rota tem que vir **antes** de `.../inativar` e `.../baixar` no `urlpatterns` seria um problema se o regex de `contrato_id` pudesse casar com a palavra `inativar`/`baixar` — não casa, porque `[0-9a-f-]{36}` exige exatamente 36 caracteres hexadecimais/hífen e `inativar`/`baixar` têm letras fora de `a-f`. Ordem não importa aqui, mas manter a rota nova junto da rota de coleção por legibilidade.)

- [ ] **Step 6: Rodar os testes da Task 2 e confirmar que passam**

Run: `cd C:\DEV\ap\ap-back-contratos\contratos && python -m pytest apps/contratos/tests/test_views_detalhar_contrato.py -v`
Expected: PASS

- [ ] **Step 7: Rodar a suíte inteira**

Run: `cd C:\DEV\ap\ap-back-contratos\contratos && python -m pytest apps/contratos -v`
Expected: PASS — nenhuma regressão.

- [ ] **Step 8: Commit**

```bash
git add apps/contratos/contrato_repository.py apps/contratos/views.py apps/contratos/urls.py apps/contratos/tests/test_views_detalhar_contrato.py
git commit -m "feat: GET /api/v1/contratos/<financiador_id>/<id> — detalhe do contrato (garantias + URs + indicadores)"
```

---

## Depois deste plano

O front (`ap-front`, plano `docs/superpowers/plans/2026-08-27-contratos-cerc-frontend.md`) consome estes dois endpoints. Rodar o backend localmente antes de testar o front de ponta a ponta:

```bash
cd C:\DEV\ap\ap-back-contratos\contratos
python manage.py runserver 8000
```
