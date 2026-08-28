# Próximos passos — pendências de formulário/contrato

Registrado em 2026-08-28. Atualizado em 2026-08-28 após brainstorm + implementação nesta sessão
(repo `ap-back-contratos`).

## Concluído nesta sessão (ap-back-contratos)

**`sincronizar_dominio_arranjo`** — a tabela local `dominio_arranjo` estava vazia (job nunca
implementado), o que fazia a validação C19 rejeitar qualquer `codigoArranjoPagamento` fora de
`"99T"` na criação de contrato. Implementado:
- `apps/contratos/dominio_arranjo_repository.py` — `sincronizar_arranjos()` + constante
  `CODIGOS_ARRANJO_VIGENTES` (lista estática v1.5 da CERC, documentada na SPEC-01 §2 — **não
  existe endpoint CERC live pra isso**; quando a CERC publicar nova versão, atualizar a
  constante à mão).
- Endpoint `POST /api/v1/jobs/sincronizar-dominio-arranjo` em `views.py`/`urls.py`, protegido
  por OIDC (mesmo padrão dos outros jobs), iterando `_TENANTS_JOBS_PERIODICOS` (hoje só o
  tenant de dev, mesmo hack do `ap-back-consulta-agenda`).
- De quebra, corrigido bug pré-existente não relacionado em
  `apps/contratos/tests/test_views_criar_contrato.py`: `dataInicio`/`dataFim` da garantia
  estavam hardcoded (`"2026-08-26"`), o que passou a violar C08 (`dataInicio` no passado)
  assim que "hoje" ultrapassou essa data. Trocado por datas relativas a `date.today()`.
- Suíte completa: 270 passed.

## Re-scoping dos 3 itens originais (decisão tomada nesta sessão)

Os 3 itens abaixo foram levantados achando que seriam trabalho de backend em
`ap-back-contratos`. Depois de mapear os repos irmãos (`ap-front`, `ap-back-optin`,
`ap-back-consulta-agenda`), ficou claro que **nenhum dos três gera trabalho neste repo** — e
foi decidido que o front consome os outros serviços de backend diretamente (sem
`ap-back-contratos` fazer proxy/BFF). Seguem como estavam, mas o "onde" mudou:

### 1. Contratante vs. Detentor
Só existe em `ap-front` (labels/textos de ajuda). Zero trabalho em `ap-back-contratos` — os
campos aqui são só `documento_contratante`/`cnpj_detentor` crus.

### 2. Filtro de URs com credenciadora/bandeira selecionáveis
- `dominio_arranjo` (bandeira) é dono do **`ap-back-optin`** (chamado de "SPEC 01"); é quem
  deveria expor a lista pro front, se ainda não expõe.
- **Não existe domínio de credenciadora em lugar nenhum** — pelos specs da CERC, credenciadora
  é só CNPJ (ou `"99T"` = todas), sem lista fechada. Uma lista de credenciadoras "conhecidas"
  teria que vir de valores distintos já vistos em URs (`ap-back-consulta-agenda`), não de um
  domínio CERC.

### 3. Seleção de URs pro contrato (agenda)
- O serviço de agenda é o **`ap-back-consulta-agenda`**, e ele **já tem** o endpoint
  `GET /agendas/urs` (paginado por cursor, filtrável por `credenciadora`, `arranjo`, `ufr`,
  `titular`, `constituicao`, `origem`) — não é algo hipotético, já está implementado.
- Decisão: **o front chama esse serviço diretamente**, não via proxy deste repo. Depois de
  montado o filtro (credenciadora × arranjo × UFR/titular × janela de datas), o front chama o
  `criar_contrato` deste repo normalmente — que já aceita esses campos.

## Sem trabalho pendente identificado em ap-back-contratos para os itens 1-3

Os próximos passos reais desses itens vivem em `ap-front` (item 1, e a tela de seleção de URs
dos itens 2/3) e possivelmente em `ap-back-optin` (expor `dominio_arranjo` num endpoint de
leitura, se ainda não existir). Recomenda-se abrir sessões dedicadas nesses repos.
