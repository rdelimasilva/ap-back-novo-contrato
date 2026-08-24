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
CREATE INDEX ON contrato (cnpj_participante, status);
CREATE INDEX ON contrato (status);

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

CREATE TABLE cerc_requisicao (
  id                 TEXT PRIMARY KEY,
  recurso            TEXT NOT NULL,
  correlacao_id      TEXT NOT NULL,
  http_status        INT,
  request_body       JSONB NOT NULL,
  response_body      JSONB,
  tentativa          INT NOT NULL DEFAULT 1,
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_inbox (
  id               TEXT PRIMARY KEY,
  tipo_evento      TEXT NOT NULL,
  data_hora_evento TIMESTAMPTZ NOT NULL,
  payload          JSONB NOT NULL,
  hash_dedupe      TEXT NOT NULL UNIQUE,
  processado_em    TIMESTAMPTZ,
  erro             TEXT,
  recebido_em      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dominio_arranjo (
  codigo        TEXT PRIMARY KEY,
  descricao     TEXT,
  ativo         BOOLEAN NOT NULL DEFAULT true,
  atualizado_em TIMESTAMPTZ NOT NULL
);
