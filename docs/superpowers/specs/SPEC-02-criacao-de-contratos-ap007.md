# SPEC 02 — Serviço de Criação e Gestão de Contratos (CERC-AP007 / AP013)

> **Status:** pronta para implementação
> **Público-alvo:** agente de código / squad de engenharia
> **Papel na cadeia CERC:** Financiador (IF ou Não Financeira)
> **Canal:** API REST síncrona + Webhook assíncrono (`tipoEvento = contrato`)
> **Versão da API CERC:** 1.5 (`v15` / `v150`)
> **Fonte normativa:** docs.cerc.com — Financiador › Gestão de contratos (registro, acompanhamento, AP013)

---

## 0. A regra que define esta spec

> **O retorno síncrono do `PUT /v15/contratos` valida apenas a estrutura do request.
> O resultado real do registro chega depois, por webhook (`tipoEvento = contrato`).**

Toda a arquitetura deriva disso: o serviço é **assíncrono por natureza**, nenhum contrato pode ser considerado registrado com base no HTTP 207, e a máquina de estados (§8) é o coração da implementação. Um agente que implementar isto como request/response síncrono estará **errado**.

---

## 1. Escopo

| Capacidade | Interface CERC |
|---|---|
| Criar contrato + definições de garantia | `PUT /v15/contratos` — `tipoOperacao = C` (AP007A + AP007B) |
| Atualizar contrato | `tipoOperacao = A` |
| Inativar contrato | `tipoOperacao = I` |
| Baixar contrato | `tipoOperacao = B` |
| **Simular** contrato (sem efeito) | `tipoOperacao = S` |
| Resilição parcial / total | `tipoOperacao = P` / `R` |
| Receber resultado do processamento | Webhook `tipoEvento = contrato` |
| Consultar contrato (detalhe + URs) | `POST /contrato/consultar` |
| Consultar contrato (visão sintética por credenciadora) | `POST /v150/contrato/consultar` (**AP013B**) |
| Reconciliar por arquivo | AP013 / AP013A / AP013B / AP013C (`*.csv` via SFTP) — §10 |

**Fora do escopo:** opt-in/opt-out e consulta de agenda (→ **SPEC 01**); jornada da credenciadora (AP002/AP008).

---

## 2. Modelo conceitual

Um **contrato** (AP007A) carrega os dados do instrumento e uma ou mais **garantias** (AP007B). Cada garantia contém uma `definicaoUnidadeRecebivel` — um **filtro** (credenciadoras × arranjos × UFR/titular × janela de liquidação) — e a regra de comprometimento. A CERC resolve o filtro em **URs alcançadas** e aplica o **efeito de contrato**.

```
Contrato (AP007A)
 ├─ dados do instrumento (contratante, detentor, datas, saldo, modalidade)
 ├─ domicílio de pagamento
 └─ garantias[] (AP007B)
     ├─ definicaoUnidadeRecebivel = FILTRO
     ├─ regrasDivisao + valorAOnerar = EFEITO
     └─ (retorno) unidadesRecebiveisAlcancadas[] = URs resolvidas pela CERC
```

### 2.1 Classificação dos atributos (define o que pode ser atualizado)

| Classe | Campos | Regra |
|---|---|---|
| **Chave** | `identificadorContrato`, `cnpjParticipante` | Identificam o contrato. Imutáveis. |
| **Estáticos** | `repactuacao`, `documentoContratante`, `identificacaoContratosAnteriores`, `dataAssinatura`, `dataVencimento`, `modalidadeOperacao`, `parcelas[]` (data e valor) | **Não podem** ser alterados após informados. Tentativa → `107807`. |
| **Dinâmicos** | `cnpjDetentor`, `domicilioPagamento` (`numeroDocumentoTitular`, `tipoConta`, `compe`, `ispb`, `agencia`, `numeroConta`), `identificacaoGestaoEntidadeRegistradora`, `carteira`, `tipoAvaliacao` | Atualizáveis via `tipoOperacao = A`. |
| **Valores** | `saldoDevedor`, `limiteOperacaoGarantida` | Devem ser mantidos atualizados ao longo do ciclo de vida. |

O serviço **deve** implementar essa classificação como validação local (`ERR_CAMPO_ESTATICO`) e rejeitar a atualização antes de chamar a CERC.

### 2.2 Periodicidade obrigatória

Atualizar a cada negociação que envolva **troca de titularidade ou ônus de URs**, dentro da janela de processamento online. As informações do contrato **devem ser mantidas atualizadas** no sistema CERC — obrigação regulatória, não otimização.

---

## 3. Autenticação e ambientes

Idêntico à SPEC 01 §3 (OAuth 2.0 client credentials, `api.int.cerc.com` / `api.prd.cerc.com`, Bearer, renovação a 80 % de `expires_in`). Reutilizar o mesmo `TokenProvider` — **não** duplicar implementação.

---

## 4. `PUT /v15/contratos` — request

Body: **array** de contratos. Resposta `207` (multi-status) com um item por entrada.

### 4.1 Campos do contrato (AP007A)

| Campo | Tipo | Obrig. | Domínio / regra |
|---|---|---|---|
| `tipoOperacao` | string | ✔ | `C` criar · `A` atualizar · `I` inativar · `B` baixar · `S` simular · `P` resilição parcial · `R` resilição total |
| `referenciaExterna` | string | ✔ | única, **não atualizável** |
| `identificadorContrato` | string | ✔ | id do contrato nos controles do participante. Em **bloqueio judicial**, informar o número do processo |
| `documentoContratante` | string | ✔ | CPF/CNPJ sem formatação, zero-pad. **Não atualizável** |
| `repactuacao` | string | ✔ | `0` não · `1` sim |
| `identificacaoContratosAnteriores` | string[] | condicional | obrigatório quando `repactuacao = 1` |
| `cnpjParticipante` | string | ✔ | **não atualizável** |
| `cnpjDetentor` | string | ✔ | atualizável |
| `tipoEfeito` | string | ✔ | `1` troca de titularidade · `2` ônus cessão fiduciária · `3` ônus outros · `4` bloqueio judicial *(`8` promessa de cessão existe no leiaute de arquivo AP013/AP013B; **não** consta no enum da API v1.5 — ver §12.1)* |
| `saldoDevedor` | number | ✔ | ≥ 0.01. Saldo devedor (garantia) ou valor presente (cessão) |
| `limiteOperacaoGarantida` | number | ✔ | ≥ 0.01 |
| `valorMantido` | number | ✔ | ≥ 0.01. Valor mínimo a manter (garantia) ou valor futuro (cessão) |
| `dataAssinatura` | date | ✔ | `AAAA-MM-DD`. **Não atualizável** |
| `dataVencimento` | date | ✔ | `AAAA-MM-DD`. **Não atualizável** |
| `identificacaoGestaoEntidadeRegistradora` | string | ✔ | `1` gestão pela entidade registradora (GCAP) · `2` gestão do financiador · `3` gestão do financiador com monitoramento e alertas CERC |
| `modalidadeOperacao` | string | ✔ | `1` rotativo · `2` parcelado · `3` cessão. **Não atualizável** |
| `parcelas[]` | array | condicional | `{ vencimento, valor ≥ 0.01 }`. Obrigatória conforme modalidade (`107034`) |
| `carteira` | string | — | default: carteira padrão do participante |
| `tipoAvaliacao` | string | — | `avaliacao_agenda_basica_ap` · `avaliacao_agenda_completa_ap` · `avaliacao_contrato_basica_ap` · `avaliacao_contrato_completa_ap` |
| `taxaJuros` | number | — | taxa anual, 2 casas (`20.53` = 20,53 % a.a.). Preencher em contratos **PEAC** |
| `indexador` | string | — | `1` prefixada · `2` Selic · `3` DI · `4` IPCA · `5` IGPM · `6` dólar · `7` euro · `8` outros. PEAC → `1` |
| `aceiteIncondicional` | string | — | `1` aceitar · `2` recusar |
| `garantias[]` | array | ✔ | ver §4.2 |

### 4.2 Garantias (AP007B)

| Campo | Obrig. | Regra |
|---|---|---|
| `referenciaExterna` | ✔ | única **dentro do contrato** (`107505` = duplicada) |
| `domicilioPagamento` | ✔ | ver §4.3 |
| `definicaoUnidadeRecebivel` | ✔ | filtro; ver §4.4 |
| `regrasDivisao` | ✔ | `1` comprometimento de **valor definido** · `2` comprometimento de **percentual** do valor que vier a ser constituído |
| `valorAOnerar` | ✔ | valor em reais (`regrasDivisao=1`) ou percentual (`regrasDivisao=2`, **≤ 100** — `107825`) |
| `tipoDistribuicao` | condicional | `padrao_empilhamento_ap` · `padrao_pro_rata_ap`. **Só** pode ser enviado quando `identificacaoGestaoEntidadeRegistradora = 1` (`107503`); obrigatório nesse caso (`107224`) |

### 4.3 `domicilioPagamento`

| Campo | Obrig. | Regra |
|---|---|---|
| `numeroDocumentoTitular` | ✔ | CPF/CNPJ sem formatação |
| `nomeTitular` | — | |
| `tipoConta` | ✔ | `CC` corrente · `CD` depósito · `PG` pagamento · `PP` poupança |
| `compe` | — | exatamente 3 dígitos, zero-pad |
| `ispb` | ✔ | exatamente 8 dígitos, zero-pad |
| `agencia` | ✔ | até 8 dígitos, **sem** dígito verificador |
| `numeroConta` | ✔ | contas `CC`/`CD`/`PP`: número **com** DV separado por hífen (`999999-9`); conta `PG`: sem hífen |

> **Regra de recusa:** contratos com domicílio em instituição **não integrante do SLC** são recusados. Validar o ISPB contra a lista de participantes do SLC antes do envio.

### 4.4 `definicaoUnidadeRecebivel` (filtro)

| Campo | Obrig. | Regra |
|---|---|---|
| `listaCnpjCredenciadora` | ✔ | CNPJs sem formatação, ou `["99T"]` para todas |
| `listaCodigoArranjoPagamento` | ✔ | códigos do domínio vigente, ou `["99T"]` para todos |
| `documentoUsuarioFinalRecebedor` | — | CPF/CNPJ completo **ou raiz** |
| `documentoTitular` | — | CPF/CNPJ completo **ou raiz** |
| `dataInicio` | ✔ | início da janela de liquidação. **Não pode ser no passado** (`107813`) |
| `dataFim` | ✔ | `>= dataInicio` (`107217`) |

**Regras de CNPJ raiz:** quando se usa raiz, `documentoTitular` deve ser **igual** a `documentoUsuarioFinalRecebedor` (`107814`) e o CNPJ raiz deve ser o **único** especificado na definição (`107815`).

### 4.5 Exemplo mínimo de request

```json
[
  {
    "tipoOperacao": "C",
    "referenciaExterna": "CTR-2026-000001",
    "identificadorContrato": "OP-88231",
    "documentoContratante": "22751826000125",
    "repactuacao": "0",
    "cnpjParticipante": "12345678000199",
    "cnpjDetentor": "12345678000199",
    "tipoEfeito": "2",
    "saldoDevedor": 150000.00,
    "limiteOperacaoGarantida": 200000.00,
    "valorMantido": 180000.00,
    "dataAssinatura": "2026-08-15",
    "dataVencimento": "2027-08-15",
    "identificacaoGestaoEntidadeRegistradora": "1",
    "modalidadeOperacao": "2",
    "parcelas": [
      { "vencimento": "2026-09-15", "valor": 12500.00 },
      { "vencimento": "2026-10-15", "valor": 12500.00 }
    ],
    "carteira": "CARTEIRA-01",
    "tipoAvaliacao": "avaliacao_contrato_basica_ap",
    "garantias": [
      {
        "referenciaExterna": "CTR-2026-000001-G1",
        "domicilioPagamento": {
          "numeroDocumentoTitular": "12345678000199",
          "tipoConta": "CC",
          "compe": "341",
          "ispb": "60701190",
          "agencia": "0001",
          "numeroConta": "464561-6"
        },
        "definicaoUnidadeRecebivel": {
          "listaCnpjCredenciadora": ["99T"],
          "listaCodigoArranjoPagamento": ["VCC", "MCC"],
          "documentoUsuarioFinalRecebedor": "22751826000125",
          "documentoTitular": "22751826000125",
          "dataInicio": "2026-08-18",
          "dataFim": "2027-08-15"
        },
        "regrasDivisao": "1",
        "valorAOnerar": 180000.00,
        "tipoDistribuicao": "padrao_pro_rata_ap"
      }
    ]
  }
]
```

---

## 5. Respostas

### 5.1 Síncrona — `207`

```jsonc
[
  {
    "referenciaExterna": "CTR-2026-000001",
    "protocolo": "a0439fea-ac6e-4f03-a72e-1167999dcec5",
    "idDoContrato": "…",              // id do contrato na CERC
    "dataHoraProcessamento": "2026-08-17T12:00:00Z",
    "status": "0",                    // 0 = recebido, 1 = erro estrutural
    "erros": []
  }
]
```

`207` significa **"recebido, será processado"** — não "registrado". Persistir `protocolo` e aguardar o webhook.

### 5.2 Assíncrona — webhook `tipoEvento = contrato`

Envelope: `{ tipoEvento, dataHoraEvento (RFC3339), evento }`. Disparado a cada criação, atualização, baixa ou inativação de contrato.

Campos de `evento`:

| Campo | Obrig. | Observação |
|---|---|---|
| `referenciaExterna` | ✔ | correlação com o request |
| `protocolo` | ✔ | GUID da CERC |
| `status` | ✔ | `0` sucesso · `1` falha |
| `dataHoraProcessamento` | ✔ | RFC3339 |
| `quantidadeUnidadesRecebiveisAlcancadas` | quando `status=0` | |
| `valorUnidadesRecebiveisAlcancadas` | quando `status=0` | |
| `resultadoDistribuicaoOnus` | quando `status=0` | `0` não se aplica · `1` **suficiente** · `2` **insuficiente** · `3` **em excesso** |
| `garantiasAlcancadas[]` | quando `status=0` | inclui `unidadesRecebiveisAlcancadas[]` |
| `indicadoresConsistencia[]` | opcional | presente quando `tipoAvaliacao` foi informado |
| `erros[]` | quando `status=1` | catálogo §7 |

**`unidadesRecebiveisAlcancadas[]`** (por UR): `cnpjCredenciadora`, `tipoDocumentoUsuarioFinalRecebedor` (`1` CPF, `2` CNPJ), `documentoUsuarioFinalRecebedor`, `documentoTitular`, `codigoArranjoPagamento`, `dataLiquidacao`, `constituicao` (`1` constituída, `2` a constituir), `valorConstituidoTotal`, `valorBloqueado`, `indicadorOneracao` (`0` insucesso; `1..N` prioridade — **menor = maior prioridade**), `regrasDivisao`, `valorOnerado`, `valorConstituidoEfeito`.

**`indicadoresConsistencia[]`**: `indicador` (ex.: `estabilidade_agenda`), `resultado` (texto), `parametros[] {chave, valor}`, `criticidade` (`0` consistente · `1` neutro · `2` alerta · `3` crítico).

**Regras de negócio derivadas (implementar):**

- `resultadoDistribuicaoOnus = 2` (insuficiente) → o contrato **está registrado**, mas subgarantido. Emitir evento de domínio `ContratoSubgarantido` e alertar a operação/crédito.
- `resultadoDistribuicaoOnus = 3` (em excesso) → candidato a liberação de excedente (AP026, fora do escopo).
- `indicadorOneracao = 0` em uma UR → insucesso naquela UR: contabilizar e expor no detalhe.
- `criticidade >= 2` em qualquer indicador → destacar na resposta interna e notificar o time de crédito.

### 5.3 Requisitos do receptor de webhook

Iguais à SPEC 01 §4.4: autenticação OAuth2 ou Basic, resposta **2xx**, **até 5 tentativas** e nada além disso, **500 req/s**, gravação em `webhook_inbox` **antes** de processar, resposta em < 200 ms, deduplicação por hash.

---

## 6. Consultas

### 6.1 `POST /contrato/consultar` — visão analítica

```jsonc
// request
{ "referenciaExterna": "CTR-2026-000001", "identificadorContrato": "OP-88231",
  "tipoAvaliacao": "avaliacao_contrato_basica_ap" }   // tipoAvaliacao opcional
```

`200` devolve o contrato completo (dados do instrumento, `domicilioPagamento`, `parcelas[]`, `garantiasAlcancadas[]` com `unidadesRecebiveisAlcancadas[]`, `quantidadeUnidadesRecebiveisAlcancadas`, `valorUnidadesRecebiveisAlcancadas`, `resultadoDistribuicaoOnus`, `indicadoresConsistencia[]`).
`400` dados inválidos · `404` contrato não encontrado.

> Para operações do tipo **`S` (simulação)**, apenas `referenciaExterna` é garantido na resposta.

### 6.2 `POST /v150/contrato/consultar` — visão sintética (**AP013B**)

```jsonc
// request
{ "referenciaExterna": "CTR-2026-000001", "identificadorContrato": "OP-88231" }
```

Resposta agrega **por credenciadora** em `informacoesAlcancadas[]`:

`cnpjEntidadeRegistradora`, `cnpjCredenciadora`, `qtdeURsConstituidas`, `qtdeURsNaoConstituidas`, `quantidadeEfeitosContrato`, `valorEfeitosSolicitados`, `valorEfeitosCalculadosCERC`, `qtdeURsPriorUm`, `qtdeURsPriorDifUm`.

Cabeçalho traz ainda `tipoServico` (`1` GCAP · `2` registro simples · `3` monitoramento), `dataCriacaoContrato`, `dataAtualizacao`, `dataAssinaturaContrato`, `dataVencimento` e **`indSobrecolateral`** (= valor dos efeitos calculados pela CERC ÷ saldo devedor).

Erros: `113001` `referenciaExterna` obrigatória · `113002` `identificadorContrato` obrigatório · `113005` contrato inexistente · `113999` erro inesperado.

**Uso recomendado:** `/contrato/consultar` para a tela de detalhe de um contrato; `/v150/contrato/consultar` para dashboards de carteira e monitoramento de sobrecolateralização (evita trafegar milhares de URs).

---

## 7. Catálogo de erros (prefixo 107)

### 7.1 Estruturais do contrato — `107001`–`107041`

Campos obrigatórios/ inválidos: tipo de operação (`107001-002`), referência externa (`107003-005`), identificador do contrato (`107006`), contratante (`107007-008`), repactuação (`107009-010`), contrato anterior (`107011-013`), participante (`107014-015`), detentor (`107016-017`), tipo de efeito (`107018-019`), saldo devedor (`107020-021`), limite da operação garantida (`107022-023`), valor a ser mantido (`107024-025`), data de assinatura (`107026-027`), data de vencimento (`107028-029`), gestão pela entidade registradora (`107030-031`), modalidade da operação (`107032-033`), lista de parcelas (`107034`), data da parcela (`107035-036`), valor da parcela (`107037-038`), carteira (`107039-040`), tipo de avaliação (`107041`).

**Tratamento:** `422` ao chamador; nenhum é retentável.

### 7.2 Estruturais da definição de garantia — `107201`–`107236`

Tipo de operação na definição (`107201-202`), referência externa na definição (`107203-204`), identificador do contrato na definição (`107205-206`), credenciadora (`107207-208`), UFR (`107209-210`), arranjo (`107211-212`), data de liquidação início (`107213-214`), fim (`107215-216`), ordem das datas (`107217`), titular (`107218-219`), regra de divisão (`107220-221`), valor a onerar (`107222-223`), tipo de distribuição (`107224-225`), documento do titular do domicílio (`107226-227`), tipo de conta (`107228-229`), COMPE (`107230`), ISPB (`107231-232`), agência (`107233-234`), número da conta (`107235-236`).

### 7.3 Negócio — `107501`–`107505` e `107801`–`107827`

| Código | Descrição | Tratamento |
|---|---|---|
| 107501 | UFR não possui vínculo com a instituição credenciadora | `422` — filtro inválido |
| 107502 | Falta de conexão operacional ativa da credenciadora com registradoras | **Retentável** (transitório) + alerta |
| 107503 | `tipoDistribuicao` só quando gestão pela entidade registradora | `422` — barrar localmente |
| 107504 | Não aceito devido à recusa do contrato | Garantia órfã: contrato pai foi recusado |
| 107505 | Referência externa duplicada na definição de garantia | `422` — barrar localmente |
| 107801 | Operação não permitida — acesso negado | **Alerta crítico**; não retentar |
| 107802 | Operação inválida para o registro atual | Reconciliar estado |
| 107803 | Contrato já informado | Idempotência: reconciliar e tratar como sucesso |
| 107804 | Definição de garantia não informada para o contrato | `422` — `garantias[]` vazio |
| 107805 | Modalidade da operação incompatível com o tipo de efeito | `422` — ver matriz §12.2 |
| 107806 | Tipo de distribuição incompatível com a regra de divisão | `422` |
| 107807 | Atualização de campos não permitidos | `422` — barrar localmente pela classificação §2.1 |
| 107808 / 107809 / 107810 | Acesso negado / operação inválida / campos não permitidos **na definição** | idem, escopo garantia |
| 107811 | Credenciadora não pode afetar recebíveis de estabelecimentos que não são seus | `422` |
| 107812 | UFR não associado à credenciadora | `422` |
| 107813 | Data de liquidação início não pode ser do passado | `422` — barrar localmente |
| 107814 | Titular deve ser igual ao UFR para CNPJ raiz | `422` — barrar localmente |
| 107815 | CNPJ raiz deve ser o único especificado na definição | `422` — barrar localmente |
| 107816 | Contrato renegociado não pode ter definições de garantia especificadas | `422` |
| 107817 | Número de contratos renegociados fora do limite | `422` |
| 107818 / 107819 / 107820 | Participante sem carteira padrão / carteira informada inativa / carteira padrão inativa | Configuração → alerta |
| 107821 | Definição de garantia atinge URs baixadas | `422` |
| 107822 | Número de ativos alcançados fora do limite permitido | `422` — reduzir a janela do filtro |
| 107823 | Sobreposição de definições de garantia não permitida | `422` — barrar localmente (§9.3) |
| 107824 | Contrato não encontrado | `404` |
| 107825 | Regra de divisão percentual não pode exceder 100 | `422` — barrar localmente |
| 107826 | Falha na comunicação com entidade registradora responsável | **Retentável** com backoff |
| 107827 | Erro genérico de validação | `422` + registrar payload |
| **107842** | **Rejeição por limite de efeitos excedido: máximo de 45 efeitos por UR** | `422` — expor claramente ao usuário |
| 107901–107904 | Layout / grade horária / nome de arquivo | Canal arquivo |
| 107999 | Erro inesperado | **Retentável** |

### 7.4 Regras de recusa declaradas pela CERC (validar localmente)

1. Erro de formatação de qualquer tipo → recusa.
2. Valores negativos → recusa (`saldoDevedor`, `limiteOperacaoGarantida`, `valorMantido`, `valorAOnerar`, `parcelas[].valor` ≥ 0.01).
3. Domicílio em instituição **não integrante do SLC** → recusa.
4. `identificadorContrato` inexistente na definição de garantia → recusa.
5. Troca de titularidade sobre UR **já cedida ao mesmo detentor** → recusa.
6. UFR ou titular **fora da base de controle** → recusa.
7. URs **inativas ou baixadas** → recusa.
8. Valor a onerar negativo → recusa.
9. Mais de **45 efeitos** sobre a mesma UR → `107842`.

---

## 8. Máquina de estados do contrato

```
                    PUT /v15/contratos (C)
                              │
                              ▼
                        ENVIANDO
                    ┌─────────┴──────────┐
           207 status=0            207 status=1 / 400
                    │                     │
                    ▼                     ▼
             AGUARDANDO_WEBHOOK      REJEITADO_ESTRUTURAL
                    │
        ┌───────────┼────────────────┬─────────────────────┐
   webhook          webhook       timeout SLA          (op = A)
   status=0         status=1      (sem webhook)             │
        │               │              │                    ▼
        ▼               ▼              ▼              ATUALIZANDO ──► REGISTRADO
   REGISTRADO      REJEITADO      PENDENTE_CONCILIACAO
        │
        ├── op I ──► INATIVADO
        ├── op B ──► BAIXADO
        ├── op P ──► RESILIDO_PARCIAL
        └── op R ──► RESILIDO_TOTAL
```

**Sub-estado de garantia** (derivado de `resultadoDistribuicaoOnus`): `NAO_APLICAVEL` (0) · `SUFICIENTE` (1) · `INSUFICIENTE` (2) · `EXCESSO` (3).

**SLA de webhook:** se nenhum webhook chegar em **30 min** (configurável) após o `207`, mover para `PENDENTE_CONCILIACAO` e disparar `POST /contrato/consultar` para descobrir o desfecho. Alertar se persistir por > 2 h.

**Simulação (`S`):** não cria contrato. Persistir em tabela separada (`simulacao_contrato`) com o resultado do webhook; TTL de 30 dias. Nunca misturar simulações com a base de contratos.

---

## 9. Validações locais obrigatórias (pré-envio)

Toda validação abaixo evita uma chamada rejeitada — implementar como `ContratoValidator` com um teste unitário por regra.

| ID | Regra | Erro CERC evitado |
|---|---|---|
| C01 | Documentos com 11/14 dígitos (ou 8 para raiz), DV válido, zero-pad | 107008, 107015, 107017 |
| C02 | `repactuacao = 1` ⇒ `identificacaoContratosAnteriores` não vazio | 107011 |
| C03 | `repactuacao = 1` ⇒ `garantias[]` **vazio** | 107816 |
| C04 | Valores monetários ≥ 0.01 | 107021, 107023, 107025 |
| C05 | `modalidadeOperacao = 2` (parcelado) ⇒ `parcelas[]` não vazio | 107034 |
| C06 | `tipoDistribuicao` presente **se e somente se** `identificacaoGestaoEntidadeRegistradora = 1` | 107224, 107503 |
| C07 | `regrasDivisao = 2` ⇒ `valorAOnerar` ≤ 100 | 107825 |
| C08 | `definicao.dataInicio >= hoje` | 107813 |
| C09 | `definicao.dataFim >= definicao.dataInicio` | 107217 |
| C10 | CNPJ raiz ⇒ `documentoTitular == documentoUsuarioFinalRecebedor` | 107814 |
| C11 | CNPJ raiz ⇒ único documento na definição | 107815 |
| C12 | `referenciaExterna` das garantias única dentro do contrato | 107505 |
| C13 | Sem sobreposição entre definições de garantia do mesmo contrato (mesma credenciadora × arranjo × UFR/titular × interseção de datas, tratando `99T` como universo) | 107823 |
| C14 | ISPB do domicílio pertence à lista de participantes do SLC | recusa por SLC |
| C15 | `numeroConta` com DV e hífen para `CC`/`CD`/`PP`; sem hífen para `PG` | 107236 |
| C16 | `ispb` = 8 dígitos; `compe` = 3 dígitos; `agencia` ≤ 8 dígitos sem DV | 107230–107234 |
| C17 | Em `tipoOperacao = A`, nenhum campo estático alterado (§2.1) | 107807 |
| C18 | `tipoEfeito = 4` (bloqueio judicial) ⇒ `identificadorContrato` é o número do processo judicial | — |
| C19 | Arranjos pertencem ao domínio vigente sincronizado | 107212 |
| C20 | Estimativa de efeitos por UR ≤ 45 quando a informação local existir | 107842 |

---

## 10. Reconciliação por arquivo (AP013 / A / B / C)

Mesmo com integração por API, o **AP013 é a fonte de verdade regulatória** para conferência. Implementar o ingestor de CSV como job independente (não bloqueante para a API).

**Nomenclatura:** `{Tipo_Leiaute}_{Ident_IF}_{DataReq}_{Seq}_ret.csv`
onde `Tipo_Leiaute ∈ {CERC-AP013, CERC-AP013A, CERC-AP013B, CERC-AP013C}`, `Ident_IF` = **raiz do CNPJ** (8 dígitos), `DataReq` = `YYYYMMDD`, `Seq` = 7 dígitos a partir de `0000001`.
**Diretório:** `/situacao_contrato/saida/` · **Emissor:** CERC → IF · **Periodicidade:** sob demanda · **Sem arquivo de retorno.**

| Leiaute | Visão | Uso |
|---|---|---|
| **AP013 (legado)** | Analítica: oneração **por UR alcançada** | Conferência UR a UR; auditoria |
| **AP013A** | Sintética **por detentor** | Exposição consolidada por detentor |
| **AP013B** | Analítica agregada **por credenciadora** (inclui `Tipo de serviço` e o efeito `8 = Promessa de Cessão`) | Espelho do `POST /v150/contrato/consultar` |
| **AP013C** | Pós-**redistribuição do Gestão de Colateral** (disponível a partir das **13h**) | Conferência de suficiência antes × depois |

### 10.1 AP013 legado — colunas (resumo operacional)

1 `Referência externa` (enviada no AP007A) · 2 `Identificador do contrato` · 3 `Contratante` · 4 `Repactuação` (0/1) · 5 `Identificador do contrato anterior` (obrigatório se repactuação=1) · 6 `Participante` · 7 `Detentor` · 8 `Tipo de efeito` (1,2,3,4,**8**) · 9 `Saldo devedor` · 10 `Limite da operação garantida` (obrigatório se efeito ≠ 1) · 11 `Valor a ser mantido` · 12 `Data de vencimento` · **13 Lista de URs alcançadas**: 13.1 entidade registradora · 13.2 credenciadora/sub · 13.3 UFR · 13.4 arranjo · 13.5 data de liquidação · 13.6 titular · 13.7 constituição (1/2) · 13.8 valor constituído total · 13.9 valor bloqueado · 13.10 indicador de oneração (0 = insucesso; 1..N prioridade) · 13.11 regra de divisão (1/2) · 13.12 valor onerado · 13.13 referência externa (do AP007B) · 13.14 **valor constituído do efeito** · **14 Indicadores de consistência** (14.1 indicador · 14.2 resultado · 14.3 parâmetros no formato `attr1:res1|attr2:res2` · 14.4 criticidade 0–3) · 15 `Quantidade de URs alcançadas` · 16 `Valor em URs alcançadas` · 17 `Resultado da distribuição dos ônus` (0–3).

### 10.2 Comportamento de URs liquidadas (regra crítica de conciliação)

Quando uma UR vinculada ao contrato é **liquidada**:

- **13.8 valor constituído total** → continua preenchido com o valor **histórico**;
- **13.14 valor constituído do efeito** → vem **zerado (`0.00`)**, indicando que não há mais efeito financeiro ativo;
- **Elegibilidade temporal:** o arquivo exibe **apenas URs com `Data de Liquidação >= data de geração do arquivo`**. A partir do dia seguinte à data de liquidação, a UR **deixa de aparecer** — por regra de data, **não** por ter sido liquidada;
- Liquidação antecipada com data de liquidação futura → a UR **continua aparecendo** (constituído preenchido, efeito zerado).

> **Implicação para o parser:** o desaparecimento de uma UR entre dois arquivos **não** é evidência de erro nem de baixa de garantia. Nunca gerar alerta de divergência apenas por ausência quando `dataLiquidacao < dataGeracaoArquivo`.

### 10.3 AP013A — por detentor

1 detentor · 2 quantidade de contratos ativos · 3 quantidade de contratantes distintos · 4 valor saldo devedor total · 5 nº total de URs **constituídas** alcançadas · 6 nº total de URs **não constituídas** alcançadas · 7 quantidade total de efeitos · 8 valor total dos efeitos **solicitados** · 9 valor total dos efeitos **calculados CERC** (o que o financiador deve esperar onerar) · 10 valor total dos efeitos **calculados Credenciadoras** (o que a credenciadora de fato aplicou).

> **Regra de monitoramento:** `campo 10 ≠ campo 9` é divergência entre expectativa CERC e aplicação da credenciadora. A expectativa é que sejam idênticos — abrir alerta operacional quando divergirem.

### 10.4 AP013C — pós Gestão de Colateral

Estrutura em blocos: data da redistribuição (1); dados gerais (2–6: referência externa, contratante, participante, carteira, valor mínimo a manter); **antes** da redistribuição (7 suficiência, 8 URs constituídas, 9 URs a constituir, 10 valor constituído dos efeitos); **entradas** da redistribuição (11 valor livre de agenda, 12–13 URs solicitadas); **depois** (14 suficiência, 15–16 quantidades, 17 valor constituído dos efeitos); 18 valor de agenda anômala (opcional); 19 observações/erros da redistribuição (opcional).

`Valor em suficiência = valor mínimo (campo 6) − valor constituído dos efeitos`. Negativo = déficit; positivo = excesso.

---

## 11. Modelo de dados

```sql
CREATE TABLE contrato (
  id                        TEXT PRIMARY KEY,
  referencia_externa        TEXT UNIQUE NOT NULL,
  identificador_contrato    TEXT NOT NULL,
  protocolo_cerc            TEXT,
  id_contrato_cerc          TEXT,
  status                    TEXT NOT NULL,      -- §8
  status_garantia           TEXT,               -- NAO_APLICAVEL|SUFICIENTE|INSUFICIENTE|EXCESSO
  cnpj_participante         TEXT NOT NULL,
  documento_contratante     TEXT NOT NULL,
  cnpj_detentor             TEXT NOT NULL,
  tipo_efeito               TEXT NOT NULL,
  modalidade_operacao       TEXT NOT NULL,
  gestao_entidade_registradora TEXT NOT NULL,
  tipo_servico              TEXT,               -- de /v150: 1 GCAP, 2 simples, 3 monitoramento
  saldo_devedor             NUMERIC(18,2) NOT NULL,
  limite_operacao_garantida NUMERIC(18,2) NOT NULL,
  valor_mantido             NUMERIC(18,2) NOT NULL,
  data_assinatura           DATE NOT NULL,
  data_vencimento           DATE NOT NULL,
  repactuacao               BOOLEAN NOT NULL,
  carteira                  TEXT,
  tipo_avaliacao            TEXT,
  taxa_juros                NUMERIC(8,2),
  indexador                 TEXT,
  qtd_urs_alcancadas        INT,
  valor_urs_alcancadas      NUMERIC(18,2),
  resultado_distribuicao    TEXT,
  ind_sobrecolateral        NUMERIC(12,4),
  enviado_em                TIMESTAMPTZ,
  confirmado_em             TIMESTAMPTZ,
  UNIQUE (cnpj_participante, identificador_contrato)
);

CREATE TABLE contrato_contrato_anterior (
  contrato_id TEXT REFERENCES contrato(id), identificador_anterior TEXT,
  PRIMARY KEY (contrato_id, identificador_anterior));

CREATE TABLE contrato_parcela (
  contrato_id TEXT REFERENCES contrato(id), vencimento DATE,
  valor NUMERIC(18,2) NOT NULL CHECK (valor >= 0.01),
  PRIMARY KEY (contrato_id, vencimento));

CREATE TABLE contrato_domicilio (
  contrato_id TEXT PRIMARY KEY REFERENCES contrato(id),
  numero_documento_titular TEXT NOT NULL, nome_titular TEXT,
  tipo_conta TEXT NOT NULL, compe TEXT, ispb TEXT NOT NULL,
  agencia TEXT, numero_conta TEXT NOT NULL);

CREATE TABLE garantia (
  id                 TEXT PRIMARY KEY,
  contrato_id        TEXT NOT NULL REFERENCES contrato(id),
  referencia_externa TEXT NOT NULL,
  regras_divisao     TEXT NOT NULL,             -- 1 valor | 2 percentual
  valor_a_onerar     NUMERIC(18,2) NOT NULL,
  tipo_distribuicao  TEXT,                      -- padrao_empilhamento_ap | padrao_pro_rata_ap
  def_lista_credenciadoras TEXT[] NOT NULL,
  def_lista_arranjos       TEXT[] NOT NULL,
  def_documento_ufr        TEXT,
  def_documento_titular    TEXT,
  def_data_inicio          DATE NOT NULL,
  def_data_fim             DATE NOT NULL,
  UNIQUE (contrato_id, referencia_externa));

CREATE TABLE garantia_ur (                      -- snapshot do webhook/consulta
  garantia_id TEXT REFERENCES garantia(id),
  cnpj_credenciadora TEXT, documento_ufr TEXT, documento_titular TEXT,
  codigo_arranjo TEXT, data_liquidacao DATE, constituicao TEXT,
  valor_constituido_total NUMERIC(18,2), valor_bloqueado NUMERIC(18,2),
  indicador_oneracao TEXT, regras_divisao TEXT,
  valor_onerado NUMERIC(18,2), valor_constituido_efeito NUMERIC(18,2),
  origem TEXT NOT NULL,                         -- WEBHOOK | CONSULTA | AP013
  snapshot_em TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (garantia_id, cnpj_credenciadora, documento_ufr,
               documento_titular, codigo_arranjo, data_liquidacao, origem));

CREATE TABLE indicador_consistencia (
  contrato_id TEXT REFERENCES contrato(id), indicador TEXT, resultado TEXT,
  parametros JSONB, criticidade TEXT, observado_em TIMESTAMPTZ,
  PRIMARY KEY (contrato_id, indicador, observado_em));

CREATE TABLE contrato_evento (                  -- histórico completo (event sourcing leve)
  id BIGSERIAL PRIMARY KEY, contrato_id TEXT REFERENCES contrato(id),
  tipo TEXT NOT NULL, payload JSONB NOT NULL, ocorrido_em TIMESTAMPTZ NOT NULL);

CREATE TABLE simulacao_contrato (
  id TEXT PRIMARY KEY, referencia_externa TEXT UNIQUE NOT NULL,
  request JSONB NOT NULL, resultado JSONB, criado_em TIMESTAMPTZ NOT NULL,
  expira_em TIMESTAMPTZ NOT NULL);

CREATE TABLE divergencia_ap013 (
  id BIGSERIAL PRIMARY KEY, arquivo TEXT NOT NULL, leiaute TEXT NOT NULL,
  contrato_id TEXT, campo TEXT NOT NULL,
  valor_local TEXT, valor_cerc TEXT, detectada_em TIMESTAMPTZ NOT NULL,
  resolvida_em TIMESTAMPTZ);
```

Reutilizar `cerc_requisicao` e `webhook_inbox` da SPEC 01 (mesmo schema, mesma infra).

---

## 12. Pontos de atenção e decisões pendentes

### 12.1 `tipoEfeito = 8` (Promessa de Cessão)

O leiaute de arquivo **AP013** e **AP013B** aceitam `8 = Promessa de Cessão`, mas o enum do `PUT /v15/contratos` publicado é `["1","2","3","4"]`. **Ação:** confirmar com a CERC se a promessa de cessão é registrável via API v1.5 e sob qual campo. Enquanto não confirmado: o **parser de AP013 deve aceitar `8`**; o **validador de request deve rejeitar `8`** com mensagem explícita apontando este item.

### 12.2 Matriz modalidade × tipo de efeito

`107805 MODALIDADE DA OPERACAO INCOMPATIVEL COM O TIPO DE EFEITO` existe, mas a matriz não está publicada. **Ação:** obter a matriz oficial (canal de suporte/certificação) e implementá-la em `ModalidadeEfeitoPolicy`. Até lá, deixar a validação a cargo da CERC e mapear `107805` para uma mensagem clara ao usuário. Hipótese a validar: `modalidadeOperacao = 3` (cessão) ↔ `tipoEfeito = 1` (troca de titularidade).

### 12.3 Divergência `tipoServico` × `identificacaoGestaoEntidadeRegistradora`

O request usa `identificacaoGestaoEntidadeRegistradora` (`1` registradora, `2` financiador, `3` financiador com monitoramento); a resposta `/v150` usa `tipoServico` (`1` GCAP, `2` registro simples, `3` monitoramento); o arquivo AP013B usa "Tipo de serviço" (`1` gestão de colateral, `2` registro simples, `3` monitoramento). Tratar como o **mesmo conceito** com três nomes e mapear explicitamente em um único enum de domínio (`TipoGestao`), documentando a equivalência no código.

### 12.4 Outros

- **Rate limits e tamanho máximo de lote** do `PUT /v15/contratos`: não publicados. Começar com lotes de 100 e ajustar após a certificação.
- **Grade horária** — "janela de processamento online": confirmar horários para o canal API.
- **AP013C** só é gerado após a redistribuição do Gestão de Colateral, **a partir das 13h**; agendar o ingestor para depois disso.
- `107822` (número de ativos alcançados fora do limite) não tem limite numérico publicado — instrumentar e descobrir empiricamente em homologação.

---

## 13. Critérios de aceite e testes

### 13.1 Unitários

- Cada regra C01–C20 (§9) com pelo menos um caso positivo e um negativo.
- Serializador: valores monetários com 2 casas, sem notação científica; datas em `AAAA-MM-DD`; documentos zero-padded.
- Parser do 207 multi-status com itens mistos.
- Detector de sobreposição de garantias (C13), incluindo o caso `99T`.
- Classificador de campos estáticos × dinâmicos (C17) exercitando todos os campos de §2.1.
- Parser AP013: linha com UR liquidada (13.8 preenchido, 13.14 = `0.00`) **não** gera divergência.
- Parser AP013A: `campo 10 ≠ campo 9` gera exatamente uma divergência.

### 13.2 Integração

| # | Cenário | Esperado |
|---|---|---|
| IT-01 | Criar contrato válido, webhook `status=0` | `REGISTRADO`, URs persistidas, `resultadoDistribuicaoOnus` gravado |
| IT-02 | Webhook `status=1` com `107501` | `REJEITADO`, erro exposto ao chamador |
| IT-03 | 207 recebido, webhook nunca chega | após SLA → `PENDENTE_CONCILIACAO` + consulta automática |
| IT-04 | Webhook duplicado | processado uma única vez |
| IT-05 | Atualização alterando `dataVencimento` | bloqueado localmente (C17), **sem** chamada à CERC |
| IT-06 | `repactuacao=1` com `garantias[]` preenchido | bloqueado localmente (C03) |
| IT-07 | `tipoDistribuicao` com gestão `2` | bloqueado localmente (C06) |
| IT-08 | `regrasDivisao=2` e `valorAOnerar=120` | bloqueado localmente (C07) |
| IT-09 | `dataInicio` no passado | bloqueado localmente (C08) |
| IT-10 | CNPJ raiz com titular ≠ UFR | bloqueado localmente (C10) |
| IT-11 | Simulação (`S`) | grava em `simulacao_contrato`, não cria contrato |
| IT-12 | Baixa (`B`) de contrato registrado | `BAIXADO`, evento no histórico |
| IT-13 | `resultadoDistribuicaoOnus=2` | evento `ContratoSubgarantido` emitido |
| IT-14 | `POST /v150/contrato/consultar` | `indSobrecolateral` e agregados por credenciadora persistidos |
| IT-15 | `113005` na consulta | `404` ao chamador |
| IT-16 | Ingestão de AP013 com UR ausente por regra temporal | **nenhuma** divergência gerada |
| IT-17 | Ingestão de AP013C | suficiência antes/depois calculada e exposta |
| IT-18 | 46 efeitos sobre a mesma UR | `107842` mapeado com mensagem clara |

### 13.3 Definição de pronto

- [ ] Testes §13.1 e §13.2 verdes
- [ ] Todos os códigos `107xxx` de §7 mapeados em enum, com teste de cobertura do catálogo
- [ ] Nenhum `float`/`double` em campo monetário
- [ ] Receptor de webhook sustenta 500 req/s com 100 % de respostas 2xx
- [ ] Job de reconciliação AP013 rodando e populando `divergencia_ap013`
- [ ] Itens §12.1 e §12.2 respondidos pela CERC **ou** registrados como risco aceito com o dono do produto
- [ ] Certificação CERC concluída em homologação

---

## 14. Dependências entre as duas specs

| Componente | Dono | Reuso |
|---|---|---|
| `TokenProvider` (OAuth2) | SPEC 01 | SPEC 02 consome |
| `webhook_inbox` + receptor HTTP | SPEC 01 | SPEC 02 registra o handler de `tipoEvento = contrato` |
| `cerc_requisicao` (auditoria) | SPEC 01 | compartilhado |
| `dominio_arranjo` | SPEC 01 | compartilhado |
| Normalizador de documentos (CPF/CNPJ/raiz) | SPEC 01 | compartilhado |
| Opt-in por força de contrato | SPEC 02 cria | SPEC 01 apenas lê (`origem = CONTRATO`) |

**Ordem de implementação recomendada:** SPEC 01 §3 e §4.4 (token + webhook) → SPEC 01 completa → SPEC 02.
