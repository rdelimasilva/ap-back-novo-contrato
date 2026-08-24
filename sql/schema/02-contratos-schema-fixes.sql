-- Correções levantadas pela revisão final do Plano 02, aplicadas enquanto
-- o banco ainda está vazio (mais barato agora do que depois que existirem
-- dados reais):
--
-- 1. A PK original de garantia_ur tornava documento_ufr/documento_titular
--    implicitamente NOT NULL, mas a SPEC-02 §4.4 marca os dois como
--    opcionais — um webhook real sem um desses campos falharia ao gravar.
--    Troca por PK substituta (id) + índice único funcional que trata NULL
--    como equivalente a string vazia pra efeito de deduplicação/upsert.
-- 2. Índices que faltavam nos 3 caminhos de maior volume esperado.
-- 3. Tabela de controle do que já foi aplicado, usada por apply_schema.py
--    a partir de agora pra não reaplicar (nem aplicar 2x por engano contra
--    homolog/produção quando essas instâncias existirem).

ALTER TABLE garantia_ur DROP CONSTRAINT garantia_ur_pkey;
ALTER TABLE garantia_ur ADD COLUMN id BIGSERIAL PRIMARY KEY;
-- Dropar a PK antiga NÃO remove o NOT NULL que a PK deixou nas colunas —
-- em Postgres esse NOT NULL vira independente da PK e precisa ser
-- removido explicitamente.
ALTER TABLE garantia_ur ALTER COLUMN documento_ufr DROP NOT NULL;
ALTER TABLE garantia_ur ALTER COLUMN documento_titular DROP NOT NULL;
CREATE UNIQUE INDEX garantia_ur_natural_key ON garantia_ur (
  garantia_id, cnpj_credenciadora,
  COALESCE(documento_ufr, ''), COALESCE(documento_titular, ''),
  codigo_arranjo, data_liquidacao, origem
);

CREATE INDEX ON contrato_evento (contrato_id, ocorrido_em);
CREATE INDEX ON webhook_inbox (recebido_em) WHERE processado_em IS NULL;
CREATE INDEX ON cerc_requisicao (correlacao_id);

CREATE TABLE schema_aplicado (
  arquivo       TEXT PRIMARY KEY,
  checksum      TEXT NOT NULL,
  aplicado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
