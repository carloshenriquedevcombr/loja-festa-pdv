-- =============================================================================
-- Migração 0001 — schema consolidado. Aplicada numa única transação por scripts/migrate.sh
-- PostgreSQL 16+
--
-- Convenções
--   * Dinheiro sempre em BIGINT (centavos). Nunca float.
--   * Toda tabela de dados tem loja_id e é protegida por Row-Level Security.
--   * O app define, no início de cada transação:
--       SET LOCAL app.loja_id    = '<uuid da loja>';
--       SET LOCAL app.usuario_id = '<uuid do usuário logado>';
--   * Nada é apagado: peças são ARQUIVADAS, dinheiro é ESTORNADO,
--     papéis expiram por valido_ate.
--   * Nenhuma senha neste arquivo: a senha do papel app_user é definida
--     fora do código (variável de ambiente / Docker secret).
-- =============================================================================


CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- unaccent() não é IMMUTABLE; este wrapper permite usá-lo em índice.
CREATE FUNCTION f_unaccent(texto TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, texto) $$;

-- Utilitários de contexto da sessão
CREATE FUNCTION ctx_loja() RETURNS UUID LANGUAGE sql STABLE
AS $$ SELECT NULLIF(current_setting('app.loja_id', true), '')::uuid $$;

CREATE FUNCTION ctx_usuario() RETURNS UUID LANGUAGE sql STABLE
AS $$ SELECT NULLIF(current_setting('app.usuario_id', true), '')::uuid $$;

-- Trigger genérico: tabela só aceita INSERT
CREATE FUNCTION bloquear_alteracao() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Registro imutável em %: use um lançamento de correção.', TG_TABLE_NAME
    USING ERRCODE = 'integrity_constraint_violation';
END $$;

-- =============================================================================
-- 1. LOJA E IDENTIDADE
-- =============================================================================

CREATE TABLE lojas (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  nome              TEXT NOT NULL,
  razao_social      TEXT,
  cnpj              TEXT CHECK (cnpj IS NULL OR cnpj ~ '^\d{14}$'),
  uf                CHAR(2),
  municipio_ibge    CHAR(7),
  regime_tributario TEXT NOT NULL DEFAULT 'NAO_INFORMADO'
                    CHECK (regime_tributario IN ('MEI','SIMPLES_NACIONAL','LUCRO_PRESUMIDO',
                                                 'LUCRO_REAL','NAO_INFORMADO')),
  criado_em         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE usuarios (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id                UUID NOT NULL REFERENCES lojas(id),
  nome                   TEXT NOT NULL,
  login                  TEXT NOT NULL,
  senha_hash             TEXT NOT NULL CHECK (senha_hash LIKE '$argon2id$%'),
  pin_hash               TEXT CHECK (pin_hash IS NULL OR pin_hash LIKE '$argon2id$%'),
  vinculo                TEXT NOT NULL DEFAULT 'FIXO' CHECK (vinculo IN ('FIXO','FREELANCE')),
  ativo                  BOOLEAN NOT NULL DEFAULT true,
  tentativas_falhas      SMALLINT NOT NULL DEFAULT 0,
  bloqueado_ate          TIMESTAMPTZ,
  pin_tentativas_falhas  SMALLINT NOT NULL DEFAULT 0,
  pin_bloqueado_ate      TIMESTAMPTZ,
  criado_em              TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (loja_id, login)
);

CREATE TABLE papeis (
  id      SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  codigo  TEXT NOT NULL UNIQUE,
  nome    TEXT NOT NULL,
  diretoria BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE permissoes (
  id        SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  codigo    TEXT NOT NULL UNIQUE,
  descricao TEXT NOT NULL
);

CREATE TABLE papel_permissoes (
  papel_id     SMALLINT NOT NULL REFERENCES papeis(id),
  permissao_id SMALLINT NOT NULL REFERENCES permissoes(id),
  PRIMARY KEY (papel_id, permissao_id)
);

CREATE TABLE usuario_papeis (
  loja_id        UUID NOT NULL REFERENCES lojas(id),
  usuario_id     UUID NOT NULL REFERENCES usuarios(id),
  papel_id       SMALLINT NOT NULL REFERENCES papeis(id),
  valido_ate     TIMESTAMPTZ,              -- NULL = permanente; freelance sempre preenchido
  concedido_por  UUID NOT NULL REFERENCES usuarios(id),
  concedido_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (usuario_id, papel_id)
);

CREATE TABLE auditoria (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  usuario_id   UUID REFERENCES usuarios(id),
  acao         TEXT NOT NULL,
  entidade     TEXT NOT NULL,
  entidade_id  TEXT,
  dados        JSONB,
  ip           INET,
  request_id   TEXT,
  criado_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_auditoria_loja_data ON auditoria (loja_id, criado_em DESC);

-- O usuário tem algum destes papéis, ativo e dentro da validade?
CREATE FUNCTION tem_papel(uid UUID, codigos TEXT[]) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1
      FROM usuario_papeis up
      JOIN papeis   p ON p.id = up.papel_id
      JOIN usuarios u ON u.id = up.usuario_id
     WHERE up.usuario_id = uid
       AND p.codigo = ANY (codigos)
       AND u.ativo
       AND (up.valido_ate IS NULL OR up.valido_ate > now())
  )
$$;

-- Quem pode conceder cada papel
CREATE FUNCTION validar_concessao_papel() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  papel      papeis%ROWTYPE;
  vinc       TEXT;
  tem_dona   BOOLEAN;
BEGIN
  SELECT * INTO papel FROM papeis WHERE id = NEW.papel_id;
  SELECT vinculo INTO vinc FROM usuarios WHERE id = NEW.usuario_id;

  -- Bootstrap: o primeiro Dono de uma loja concede o papel a si mesmo
  SELECT EXISTS (
    SELECT 1 FROM usuario_papeis up JOIN papeis p ON p.id = up.papel_id
     WHERE up.loja_id = NEW.loja_id AND p.codigo = 'DONO'
  ) INTO tem_dona;
  IF NOT tem_dona AND papel.codigo = 'DONO' AND NEW.concedido_por = NEW.usuario_id THEN
    RETURN NEW;
  END IF;

  IF papel.diretoria THEN
    IF NOT tem_papel(NEW.concedido_por, ARRAY['DONO']) THEN
      RAISE EXCEPTION 'Só um Dono pode atribuir papéis da Diretoria.';
    END IF;
  ELSIF NOT tem_papel(NEW.concedido_por, ARRAY['DONO','RH','DESENVOLVEDOR']) THEN
    RAISE EXCEPTION 'Sem permissão para atribuir papéis.';
  END IF;

  IF papel.codigo = 'DESENVOLVEDOR' AND NOT tem_papel(NEW.concedido_por, ARRAY['DONO']) THEN
    RAISE EXCEPTION 'Só um Dono pode atribuir o papel de Desenvolvedor.';
  END IF;

  IF vinc = 'FREELANCE' THEN
    IF NEW.valido_ate IS NULL THEN
      RAISE EXCEPTION 'Acesso de freelance precisa de data de expiração.';
    END IF;
    IF papel.codigo NOT IN ('ATENDENTE','COSTUREIRA','AJUDANTE_COSTURA','LIMPEZA') THEN
      RAISE EXCEPTION 'Freelance só recebe papéis operacionais.';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_validar_concessao_papel
  BEFORE INSERT OR UPDATE ON usuario_papeis
  FOR EACH ROW EXECUTE FUNCTION validar_concessao_papel();

-- =============================================================================
-- 2. CATÁLOGO E FISCAL
-- =============================================================================

CREATE TABLE perfis_fiscais (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id               UUID NOT NULL REFERENCES lojas(id),
  nome                  TEXT NOT NULL,
  natureza              TEXT NOT NULL CHECK (natureza IN
                          ('VENDA_MERCADORIA','LOCACAO_BEM_MOVEL','SERVICO_AJUSTE')),
  ncm                   CHAR(8),
  cfop                  CHAR(4),
  origem                SMALLINT,
  csosn_cst             TEXT,
  codigo_servico        TEXT,
  cst_ibs_cbs           TEXT,
  cclasstrib            TEXT,
  aliquota_estimada_pct NUMERIC(5,2) CHECK (aliquota_estimada_pct BETWEEN 0 AND 100),
  definido_por          UUID NOT NULL REFERENCES usuarios(id),
  vigente_desde         TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (loja_id, nome),
  CONSTRAINT venda_exige_ncm   CHECK (natureza <> 'VENDA_MERCADORIA' OR ncm IS NOT NULL),
  CONSTRAINT servico_exige_cod CHECK (natureza <> 'SERVICO_AJUSTE' OR codigo_servico IS NOT NULL)
);

CREATE TABLE categorias (
  id                     SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id                UUID NOT NULL REFERENCES lojas(id),
  nome                   TEXT NOT NULL,
  prefixo_sku            TEXT NOT NULL CHECK (prefixo_sku ~ '^[A-Z]{2,5}$'),
  proximo_numero         INT NOT NULL DEFAULT 1 CHECK (proximo_numero >= 1),
  perfil_venda_padrao    BIGINT REFERENCES perfis_fiscais(id),
  perfil_locacao_padrao  BIGINT REFERENCES perfis_fiscais(id),
  UNIQUE (loja_id, nome),
  UNIQUE (loja_id, prefixo_sku)
);

CREATE TABLE produtos (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id                  UUID NOT NULL REFERENCES lojas(id),
  categoria_id             SMALLINT NOT NULL REFERENCES categorias(id),
  sku                      TEXT NOT NULL,          -- gerado pelo banco
  nome                     TEXT NOT NULL,
  descricao                TEXT,
  tamanho                  TEXT,
  cor                      TEXT,
  pode_alugar              BOOLEAN NOT NULL DEFAULT false,
  pode_vender              BOOLEAN NOT NULL DEFAULT false,
  preco_aluguel_centavos   BIGINT CHECK (preco_aluguel_centavos > 0),
  preco_venda_centavos     BIGINT CHECK (preco_venda_centavos > 0),
  -- Estado do CADASTRO. A situação do dia (reservado, alugado, em limpeza)
  -- é calculada pelos pedidos na view produtos_situacao.
  status                   TEXT NOT NULL DEFAULT 'DISPONIVEL'
                           CHECK (status IN ('DISPONIVEL','MANUTENCAO','VENDIDO','ARQUIVADO')),
  foto_path                TEXT,
  perfil_fiscal_venda_id   BIGINT REFERENCES perfis_fiscais(id),
  perfil_fiscal_locacao_id BIGINT REFERENCES perfis_fiscais(id),
  criado_em                TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (loja_id, sku),
  CONSTRAINT modalidade_definida    CHECK (pode_alugar OR pode_vender),
  CONSTRAINT preco_aluguel_coerente CHECK (NOT pode_alugar OR preco_aluguel_centavos IS NOT NULL),
  CONSTRAINT preco_venda_coerente   CHECK (NOT pode_vender OR preco_venda_centavos IS NOT NULL)
);
CREATE INDEX idx_produtos_busca_nome ON produtos USING gin (f_unaccent(lower(nome)) gin_trgm_ops);
CREATE INDEX idx_produtos_categoria  ON produtos (loja_id, categoria_id, status);

-- SKU: contador da categoria, atômico e sem buracos (rollback desfaz o incremento).
-- Não usa lpad(), que truncaria números acima de 9999.
CREATE FUNCTION gerar_sku(p_loja UUID, p_categoria SMALLINT) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE
  v_prefixo TEXT;
  v_numero  INT;
BEGIN
  UPDATE categorias
     SET proximo_numero = proximo_numero + 1
   WHERE id = p_categoria AND loja_id = p_loja
  RETURNING prefixo_sku, proximo_numero - 1 INTO v_prefixo, v_numero;

  IF v_prefixo IS NULL THEN
    RAISE EXCEPTION 'Categoria % não encontrada nesta loja.', p_categoria;
  END IF;
  RETURN v_prefixo || '-' || CASE WHEN v_numero < 10000
                                  THEN to_char(v_numero, 'FM0000')
                                  ELSE v_numero::text END;
END $$;

CREATE FUNCTION preparar_produto() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cat categorias%ROWTYPE;
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.sku := gerar_sku(NEW.loja_id, NEW.categoria_id);   -- sempre do banco
    SELECT * INTO cat FROM categorias WHERE id = NEW.categoria_id;
    NEW.perfil_fiscal_venda_id   := COALESCE(NEW.perfil_fiscal_venda_id,   cat.perfil_venda_padrao);
    NEW.perfil_fiscal_locacao_id := COALESCE(NEW.perfil_fiscal_locacao_id, cat.perfil_locacao_padrao);
  ELSE
    IF NEW.sku <> OLD.sku THEN
      RAISE EXCEPTION 'O SKU de uma peça nunca muda.';
    END IF;
    IF OLD.status = 'VENDIDO' AND NEW.status <> 'VENDIDO' THEN
      RAISE EXCEPTION 'Peça vendida não volta ao estoque.';
    END IF;
    NEW.atualizado_em := now();
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_preparar_produto
  BEFORE INSERT OR UPDATE ON produtos
  FOR EACH ROW EXECUTE FUNCTION preparar_produto();

-- =============================================================================
-- 3. CLIENTES E MEDIDAS
-- =============================================================================

CREATE TABLE clientes (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id                UUID NOT NULL REFERENCES lojas(id),
  nome                   TEXT NOT NULL,
  telefone               TEXT,
  cpf                    TEXT CHECK (cpf IS NULL OR cpf ~ '^\d{11}$'),
  consentimento_lgpd_em  TIMESTAMPTZ,
  criado_em              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_clientes_busca_nome ON clientes USING gin (f_unaccent(lower(nome)) gin_trgm_ops);

-- Histórico: medida nova = linha nova
CREATE TABLE medidas (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id         UUID NOT NULL REFERENCES lojas(id),
  cliente_id      UUID NOT NULL REFERENCES clientes(id),
  busto_cm        NUMERIC(5,1),
  cintura_cm      NUMERIC(5,1),
  quadril_cm      NUMERIC(5,1),
  altura_cm       NUMERIC(5,1),
  comprimento_cm  NUMERIC(5,1),
  observacoes     TEXT,
  tomada_por      UUID NOT NULL REFERENCES usuarios(id),
  tomada_em       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_medidas_cliente ON medidas (cliente_id, tomada_em DESC);
CREATE TRIGGER trg_medidas_imutaveis BEFORE UPDATE OR DELETE ON medidas
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- =============================================================================
-- 4. CONFIGURAÇÃO COMERCIAL (versionada, só Dono ou Gerente)
-- =============================================================================

CREATE TABLE config_pagamento (
  id                              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id                         UUID NOT NULL REFERENCES lojas(id),
  entrada_minima_pct              NUMERIC(5,2) NOT NULL DEFAULT 25
                                  CHECK (entrada_minima_pct BETWEEN 0 AND 100),
  max_parcelas_aluguel            SMALLINT NOT NULL DEFAULT 6 CHECK (max_parcelas_aluguel BETWEEN 1 AND 12),
  max_parcelas_venda              SMALLINT NOT NULL DEFAULT 6 CHECK (max_parcelas_venda  BETWEEN 1 AND 12),
  valor_minimo_parcela_centavos   BIGINT NOT NULL DEFAULT 5000 CHECK (valor_minimo_parcela_centavos > 0),
  desconto_maximo_pct             NUMERIC(5,2) NOT NULL DEFAULT 10
                                  CHECK (desconto_maximo_pct BETWEEN 0 AND 100),
  teto_saida_atendente_centavos   BIGINT NOT NULL DEFAULT 10000 CHECK (teto_saida_atendente_centavos >= 0),
  parcelado_emissor_a_partir      SMALLINT CHECK (parcelado_emissor_a_partir BETWEEN 2 AND 12),
  dias_limpeza                    SMALLINT NOT NULL DEFAULT 3 CHECK (dias_limpeza BETWEEN 0 AND 30),
  definido_por                    UUID NOT NULL REFERENCES usuarios(id),
  vigente_desde                   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_config_vigente ON config_pagamento (loja_id, vigente_desde DESC);

CREATE TABLE config_juros (
  loja_id        UUID NOT NULL REFERENCES lojas(id),
  config_id      BIGINT NOT NULL REFERENCES config_pagamento(id),
  parcelas       SMALLINT NOT NULL CHECK (parcelas BETWEEN 2 AND 12),
  acrescimo_pct  NUMERIC(5,2) NOT NULL CHECK (acrescimo_pct BETWEEN 0 AND 100),
  PRIMARY KEY (config_id, parcelas)
);

CREATE FUNCTION config_vigente(p_loja UUID) RETURNS BIGINT LANGUAGE sql STABLE AS $$
  SELECT id FROM config_pagamento
   WHERE loja_id = p_loja AND vigente_desde <= now()
   ORDER BY vigente_desde DESC, id DESC
   LIMIT 1
$$;

CREATE FUNCTION exigir_dono_ou_gerente() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT tem_papel(NEW.definido_por, ARRAY['DONO','GERENTE']) THEN
    RAISE EXCEPTION 'Só a Dona ou a Gerente definem regras comerciais e taxas.';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_config_pagamento_autor BEFORE INSERT ON config_pagamento
  FOR EACH ROW EXECUTE FUNCTION exigir_dono_ou_gerente();
CREATE TRIGGER trg_config_pagamento_imutavel BEFORE UPDATE OR DELETE ON config_pagamento
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();
CREATE TRIGGER trg_config_juros_imutavel BEFORE UPDATE OR DELETE ON config_juros
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- =============================================================================
-- 5. MAQUININHAS E TAXAS
-- =============================================================================

CREATE TABLE maquininhas (
  id                    SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id               UUID NOT NULL REFERENCES lojas(id),
  apelido               TEXT NOT NULL,
  operadora             TEXT NOT NULL,            -- 'NAO_IDENTIFICADA' até a visita à loja
  cor_hex               TEXT NOT NULL DEFAULT '#6B7280' CHECK (cor_hex ~ '^#[0-9A-Fa-f]{6}$'),
  plano_recebimento     TEXT NOT NULL DEFAULT 'NAO_INFORMADO'
                        CHECK (plano_recebimento IN ('NA_HORA','1_DIA_UTIL','14_DIAS','30_DIAS',
                                                     'SEM_ANTECIPACAO','NAO_INFORMADO')),
  faixa_confirmada_mes  DATE,
  ativa                 BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (loja_id, apelido)
);

-- Toda loja nova nasce com uma maquininha genérica
CREATE FUNCTION criar_maquininha_generica() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO maquininhas (loja_id, apelido, operadora)
  VALUES (NEW.id, 'Maquininha (não identificada)', 'NAO_IDENTIFICADA');
  RETURN NEW;
END $$;

CREATE TRIGGER trg_loja_maquininha_generica AFTER INSERT ON lojas
  FOR EACH ROW EXECUTE FUNCTION criar_maquininha_generica();

CREATE TABLE taxas_maquininha (
  id                           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id                      UUID NOT NULL REFERENCES lojas(id),
  maquininha_id                SMALLINT NOT NULL REFERENCES maquininhas(id),
  modalidade                   TEXT NOT NULL CHECK (modalidade IN
                                 ('PIX','DEBITO','CREDITO_VISTA','CREDITO_LOJISTA','CREDITO_EMISSOR')),
  parcelas                     SMALLINT NOT NULL DEFAULT 1 CHECK (parcelas BETWEEN 1 AND 12),
  bandeiras                    TEXT[] NOT NULL DEFAULT '{TODAS}',
  faixa_min_centavos           BIGINT NOT NULL DEFAULT 0 CHECK (faixa_min_centavos >= 0),
  faixa_max_centavos           BIGINT,
  promocional_ate              DATE,
  promocional_limite_centavos  BIGINT,
  taxa_pct                     NUMERIC(6,3) NOT NULL CHECK (taxa_pct BETWEEN 0 AND 100),
  prazo_dias                   SMALLINT NOT NULL DEFAULT 1 CHECK (prazo_dias >= 0),
  definido_por                 UUID NOT NULL REFERENCES usuarios(id),
  vigente_desde                TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT faixa_valida CHECK (faixa_max_centavos IS NULL OR faixa_max_centavos > faixa_min_centavos),
  CONSTRAINT parcelas_coerentes CHECK (
    (modalidade IN ('PIX','DEBITO','CREDITO_VISTA') AND parcelas = 1)
    OR (modalidade IN ('CREDITO_LOJISTA','CREDITO_EMISSOR') AND parcelas >= 2))
);
CREATE INDEX idx_taxas_busca
  ON taxas_maquininha (maquininha_id, modalidade, parcelas, vigente_desde DESC);

CREATE TRIGGER trg_taxas_autor BEFORE INSERT ON taxas_maquininha
  FOR EACH ROW EXECUTE FUNCTION exigir_dono_ou_gerente();
CREATE TRIGGER trg_taxas_imutaveis BEFORE UPDATE OR DELETE ON taxas_maquininha
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- =============================================================================
-- 6. PEDIDOS E CICLO DE VIDA
-- =============================================================================

CREATE TABLE pedidos (
  id                             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id                        UUID NOT NULL REFERENCES lojas(id),
  tipo                           TEXT NOT NULL CHECK (tipo IN ('ALUGUEL','VENDA')),
  com_ajuste                     BOOLEAN NOT NULL DEFAULT false,
  fluxo                          TEXT GENERATED ALWAYS AS (
                                   CASE WHEN tipo = 'ALUGUEL' THEN 'ALUGUEL'
                                        WHEN com_ajuste       THEN 'VENDA_AJUSTE'
                                        ELSE 'VENDA_SIMPLES' END) STORED,
  produto_id                     UUID NOT NULL REFERENCES produtos(id),
  cliente_id                     UUID REFERENCES clientes(id),
  config_id                      BIGINT NOT NULL REFERENCES config_pagamento(id),
  status                         TEXT NOT NULL,
  -- Valores (o banco calcula total, entrada mínima e limite de parcelas)
  valor_peca_centavos            BIGINT NOT NULL CHECK (valor_peca_centavos > 0),
  valor_ajuste_centavos          BIGINT NOT NULL DEFAULT 0 CHECK (valor_ajuste_centavos >= 0),
  desconto_centavos              BIGINT NOT NULL DEFAULT 0 CHECK (desconto_centavos >= 0),
  desconto_motivo                TEXT,
  desconto_aprovado_por          UUID REFERENCES usuarios(id),
  valor_total_centavos           BIGINT NOT NULL,
  entrada_minima_centavos        BIGINT NOT NULL,
  entrada_excecao_aprovada_por   UUID REFERENCES usuarios(id),
  max_parcelas_permitidas        SMALLINT NOT NULL,
  -- Datas
  data_festa                     DATE,
  inicio_bloqueio                DATE NOT NULL DEFAULT current_date,
  data_retirada                  DATE,
  data_devolucao                 DATE,
  periodo_bloqueado              DATERANGE,
  -- Controle
  chave_idempotencia             UUID NOT NULL,
  criado_por                     UUID NOT NULL REFERENCES usuarios(id),
  criado_em                      TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em                  TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (loja_id, chave_idempotencia),
  CONSTRAINT status_valido CHECK (status IN
    ('RESERVADO','PROVA_1','EM_AJUSTE','PROVA_2','PRONTO','ALUGADO','DEVOLVIDO',
     'EM_LIMPEZA','CONCLUIDO','RETIRADO','VENDIDO','CANCELADO')),
  CONSTRAINT total_confere CHECK (
    valor_total_centavos = valor_peca_centavos + valor_ajuste_centavos - desconto_centavos),
  CONSTRAINT total_nao_negativo CHECK (valor_total_centavos >= 0),
  CONSTRAINT desconto_exige_aprovacao CHECK (
    desconto_centavos = 0 OR (desconto_aprovado_por IS NOT NULL AND desconto_motivo IS NOT NULL)),
  CONSTRAINT aluguel_sempre_com_ajuste CHECK (tipo <> 'ALUGUEL' OR com_ajuste),
  CONSTRAINT ajuste_cobrado_na_venda CHECK (
    tipo <> 'VENDA' OR com_ajuste = (valor_ajuste_centavos > 0)),
  CONSTRAINT ciclo_exige_cliente CHECK (
    (tipo = 'VENDA' AND NOT com_ajuste) OR cliente_id IS NOT NULL),
  CONSTRAINT aluguel_tem_datas CHECK (
    tipo <> 'ALUGUEL' OR (data_retirada IS NOT NULL AND data_devolucao IS NOT NULL
                          AND data_devolucao >= data_retirada)),
  CONSTRAINT venda_ajuste_tem_retirada CHECK (
    NOT (tipo = 'VENDA' AND com_ajuste) OR data_retirada IS NOT NULL),

  -- O mesmo vestido nunca em dois pedidos ativos com períodos sobrepostos
  CONSTRAINT sem_conflito_de_datas EXCLUDE USING gist (
    produto_id WITH =, periodo_bloqueado WITH &&)
    WHERE (status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO'))
);
CREATE INDEX idx_pedidos_produto ON pedidos (produto_id, status);
CREATE INDEX idx_pedidos_cliente ON pedidos (cliente_id);
CREATE INDEX idx_pedidos_devolucao ON pedidos (loja_id, data_devolucao)
  WHERE status IN ('ALUGADO');

CREATE TABLE transicoes_permitidas (
  fluxo TEXT NOT NULL CHECK (fluxo IN ('VENDA_SIMPLES','VENDA_AJUSTE','ALUGUEL')),
  de    TEXT NOT NULL,
  para  TEXT NOT NULL,
  PRIMARY KEY (fluxo, de, para)
);

INSERT INTO transicoes_permitidas (fluxo, de, para) VALUES
  ('ALUGUEL','RESERVADO','PROVA_1'),
  ('ALUGUEL','PROVA_1','EM_AJUSTE'),
  ('ALUGUEL','EM_AJUSTE','PROVA_2'),
  ('ALUGUEL','PROVA_2','EM_AJUSTE'),     -- segundo ajuste, se preciso
  ('ALUGUEL','PROVA_2','PRONTO'),
  ('ALUGUEL','PRONTO','ALUGADO'),        -- retirada: exige saldo zero
  ('ALUGUEL','ALUGADO','DEVOLVIDO'),
  ('ALUGUEL','DEVOLVIDO','EM_LIMPEZA'),
  ('ALUGUEL','EM_LIMPEZA','CONCLUIDO'),
  ('VENDA_AJUSTE','RESERVADO','PROVA_1'),
  ('VENDA_AJUSTE','PROVA_1','EM_AJUSTE'),
  ('VENDA_AJUSTE','EM_AJUSTE','PROVA_2'),
  ('VENDA_AJUSTE','PROVA_2','EM_AJUSTE'),
  ('VENDA_AJUSTE','PROVA_2','PRONTO'),
  ('VENDA_AJUSTE','PRONTO','RETIRADO');  -- retirada: exige saldo zero
-- Qualquer status não final pode ir para CANCELADO (tratado no trigger).

CREATE TABLE historico_status (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id     UUID NOT NULL REFERENCES lojas(id),
  pedido_id   UUID NOT NULL REFERENCES pedidos(id),
  de          TEXT,
  para        TEXT NOT NULL,
  usuario_id  UUID REFERENCES usuarios(id),
  criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_historico_pedido ON historico_status (pedido_id, criado_em);
CREATE TRIGGER trg_historico_imutavel BEFORE UPDATE OR DELETE ON historico_status
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

CREATE FUNCTION validar_desconto(p pedidos, cfg config_pagamento) RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
  IF p.desconto_centavos = 0 THEN RETURN; END IF;
  IF NOT tem_papel(p.desconto_aprovado_por, ARRAY['DONO','GERENTE']) THEN
    RAISE EXCEPTION 'Desconto precisa ser aprovado pela Dona ou pela Gerente.';
  END IF;
  IF p.desconto_centavos * 100 >
     (p.valor_peca_centavos + p.valor_ajuste_centavos) * cfg.desconto_maximo_pct THEN
    RAISE EXCEPTION 'Desconto acima do teto de % %%.', cfg.desconto_maximo_pct;
  END IF;
END $$;

-- INSERT: valida a peça, calcula valores e define o status inicial
CREATE FUNCTION preparar_pedido() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  prod produtos%ROWTYPE;
  cfg  config_pagamento%ROWTYPE;
BEGIN
  SELECT * INTO prod FROM produtos WHERE id = NEW.produto_id FOR UPDATE;
  SELECT * INTO cfg  FROM config_pagamento WHERE id = NEW.config_id;

  IF prod.loja_id <> NEW.loja_id OR cfg.loja_id <> NEW.loja_id THEN
    RAISE EXCEPTION 'Peça ou configuração de outra loja.';
  END IF;
  IF prod.status IN ('VENDIDO','ARQUIVADO') THEN
    RAISE EXCEPTION 'Esta peça não está mais disponível (%).', lower(prod.status);
  END IF;
  IF NEW.tipo = 'VENDA' AND NOT prod.pode_vender THEN
    RAISE EXCEPTION 'Esta peça é só para aluguel.';
  END IF;
  IF NEW.tipo = 'ALUGUEL' AND NOT prod.pode_alugar THEN
    RAISE EXCEPTION 'Esta peça é só para venda.';
  END IF;
  IF NEW.tipo = 'VENDA' AND EXISTS (
       SELECT 1 FROM pedidos
        WHERE produto_id = NEW.produto_id AND tipo = 'ALUGUEL'
          AND status NOT IN ('CANCELADO','CONCLUIDO')) THEN
    RAISE EXCEPTION 'Esta peça tem aluguel em andamento ou agendado e não pode ser vendida agora.';
  END IF;
  IF EXISTS (
       SELECT 1 FROM pedidos
        WHERE produto_id = NEW.produto_id AND tipo = 'VENDA'
          AND status NOT IN ('CANCELADO')) THEN
    RAISE EXCEPTION 'Esta peça já tem uma venda registrada.';
  END IF;

  -- Valores calculados pelo banco
  NEW.valor_total_centavos    := NEW.valor_peca_centavos + NEW.valor_ajuste_centavos
                                 - NEW.desconto_centavos;
  NEW.entrada_minima_centavos := CEIL(NEW.valor_total_centavos * cfg.entrada_minima_pct / 100)::bigint;
  NEW.max_parcelas_permitidas := CASE WHEN NEW.tipo = 'ALUGUEL' THEN cfg.max_parcelas_aluguel
                                      ELSE cfg.max_parcelas_venda END;
  PERFORM validar_desconto(NEW, cfg);

  IF NEW.entrada_excecao_aprovada_por IS NOT NULL
     AND NOT tem_papel(NEW.entrada_excecao_aprovada_por, ARRAY['DONO','GERENTE']) THEN
    RAISE EXCEPTION 'Exceção de entrada precisa ser aprovada pela Dona ou pela Gerente.';
  END IF;

  -- Período bloqueado e status inicial
  IF NEW.tipo = 'ALUGUEL' THEN
    NEW.periodo_bloqueado := daterange(NEW.inicio_bloqueio,
                                       NEW.data_devolucao + cfg.dias_limpeza, '[]');
    NEW.status := 'RESERVADO';
  ELSIF NEW.com_ajuste THEN
    NEW.periodo_bloqueado := daterange(NEW.inicio_bloqueio, NEW.data_retirada, '[]');
    NEW.status := 'RESERVADO';
  ELSE
    NEW.periodo_bloqueado := NULL;
    NEW.status := 'VENDIDO';
    UPDATE produtos SET status = 'VENDIDO' WHERE id = NEW.produto_id;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_preparar_pedido BEFORE INSERT ON pedidos
  FOR EACH ROW EXECUTE FUNCTION preparar_pedido();

-- UPDATE: máquina de estados, retirada quitada, desconto posterior
CREATE FUNCTION controlar_pedido() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cfg config_pagamento%ROWTYPE;
BEGIN
  IF NEW.tipo <> OLD.tipo OR NEW.com_ajuste <> OLD.com_ajuste
     OR NEW.produto_id <> OLD.produto_id OR NEW.config_id <> OLD.config_id THEN
    RAISE EXCEPTION 'Tipo, peça e regras de um pedido não mudam. Cancele e crie outro.';
  END IF;

  -- Mudança de valores (ex.: desconto concedido depois)
  IF (NEW.valor_peca_centavos, NEW.valor_ajuste_centavos, NEW.desconto_centavos)
     IS DISTINCT FROM (OLD.valor_peca_centavos, OLD.valor_ajuste_centavos, OLD.desconto_centavos) THEN
    SELECT * INTO cfg FROM config_pagamento WHERE id = NEW.config_id;
    NEW.valor_total_centavos := NEW.valor_peca_centavos + NEW.valor_ajuste_centavos
                                - NEW.desconto_centavos;
    NEW.entrada_minima_centavos := CEIL(NEW.valor_total_centavos * cfg.entrada_minima_pct / 100)::bigint;
    PERFORM validar_desconto(NEW, cfg);
    IF NEW.valor_total_centavos < valor_pago(NEW.id) THEN
      RAISE EXCEPTION 'O novo total ficaria menor do que o valor já pago.';
    END IF;
  ELSE
    NEW.valor_total_centavos    := OLD.valor_total_centavos;
    NEW.entrada_minima_centavos := OLD.entrada_minima_centavos;
  END IF;
  NEW.max_parcelas_permitidas := OLD.max_parcelas_permitidas;

  -- Máquina de estados
  IF NEW.status <> OLD.status THEN
    IF OLD.status IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO') THEN
      RAISE EXCEPTION 'Pedido encerrado (%) não muda de status.', OLD.status;
    END IF;
    IF NEW.status = 'CANCELADO' THEN
      IF NOT tem_papel(ctx_usuario(), ARRAY['DONO','GERENTE']) THEN
        RAISE EXCEPTION 'Cancelamento precisa da Dona ou da Gerente.';
      END IF;
      IF OLD.status IN ('ALUGADO','DEVOLVIDO','EM_LIMPEZA') THEN
        RAISE EXCEPTION 'Peça já retirada: não é possível cancelar.';
      END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM transicoes_permitidas
                       WHERE fluxo = OLD.fluxo AND de = OLD.status AND para = NEW.status) THEN
      -- OLD.fluxo: colunas geradas ainda não estão calculadas em NEW num trigger BEFORE
      RAISE EXCEPTION 'Mudança de % para % não é permitida.', OLD.status, NEW.status;
    END IF;

    IF NEW.status IN ('ALUGADO','RETIRADO') AND valor_pago(NEW.id) < NEW.valor_total_centavos THEN
      RAISE EXCEPTION 'Retirada bloqueada: ainda falta pagar R$ %.',
        to_char((NEW.valor_total_centavos - valor_pago(NEW.id)) / 100.0, 'FM999G999G990D00');
    END IF;

    IF NEW.status = 'RETIRADO' THEN
      UPDATE produtos SET status = 'VENDIDO' WHERE id = NEW.produto_id;
    END IF;

    INSERT INTO historico_status (loja_id, pedido_id, de, para, usuario_id)
    VALUES (NEW.loja_id, NEW.id, OLD.status, NEW.status, ctx_usuario());
  END IF;

  NEW.atualizado_em := now();
  RETURN NEW;
END $$;

CREATE TRIGGER trg_controlar_pedido BEFORE UPDATE ON pedidos
  FOR EACH ROW EXECUTE FUNCTION controlar_pedido();

-- Registra o status inicial no histórico
CREATE FUNCTION historico_inicial() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO historico_status (loja_id, pedido_id, de, para, usuario_id)
  VALUES (NEW.loja_id, NEW.id, NULL, NEW.status, NEW.criado_por);
  RETURN NEW;
END $$;

CREATE TRIGGER trg_historico_inicial AFTER INSERT ON pedidos
  FOR EACH ROW EXECUTE FUNCTION historico_inicial();

-- Verificado no COMMIT: pedido e pagamento inicial nascem juntos.
-- Venda simples: quitada. Reserva: entrada mínima paga (ou exceção aprovada).
CREATE FUNCTION checar_pagamento_inicial() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  p    pedidos%ROWTYPE;
  pago BIGINT;
BEGIN
  SELECT * INTO p FROM pedidos WHERE id = NEW.id;
  pago := valor_pago(p.id);
  IF p.fluxo = 'VENDA_SIMPLES' AND pago < p.valor_total_centavos THEN
    RAISE EXCEPTION 'Venda simples precisa ser paga no ato.';
  END IF;
  IF p.fluxo <> 'VENDA_SIMPLES' AND pago < p.entrada_minima_centavos
     AND p.entrada_excecao_aprovada_por IS NULL THEN
    RAISE EXCEPTION 'Reserva sem entrada: mínimo de R$ %.',
      to_char(p.entrada_minima_centavos / 100.0, 'FM999G999G990D00');
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER trg_pagamento_inicial
  AFTER INSERT ON pedidos
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION checar_pagamento_inicial();

-- =============================================================================
-- 7. AGENDA, COSTURA E LIMPEZA
-- =============================================================================

CREATE TABLE agendamentos (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id         UUID NOT NULL REFERENCES lojas(id),
  pedido_id       UUID NOT NULL REFERENCES pedidos(id),
  tipo            TEXT NOT NULL CHECK (tipo IN ('PROVA_1','PROVA_2','RETIRADA','DEVOLUCAO')),
  inicio          TIMESTAMPTZ NOT NULL,
  responsavel_id  UUID REFERENCES usuarios(id),
  status          TEXT NOT NULL DEFAULT 'AGENDADO'
                  CHECK (status IN ('AGENDADO','FEITO','FALTOU','REMARCADO')),
  UNIQUE (pedido_id, tipo)
);
CREATE INDEX idx_agenda_dia ON agendamentos (loja_id, inicio);

CREATE TABLE ordens_ajuste (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id        UUID NOT NULL REFERENCES lojas(id),
  pedido_id      UUID NOT NULL REFERENCES pedidos(id),
  descricao      TEXT NOT NULL,
  costureira_id  UUID REFERENCES usuarios(id),
  prazo          DATE NOT NULL,
  status         TEXT NOT NULL DEFAULT 'NA_FILA'
                 CHECK (status IN ('NA_FILA','EM_ANDAMENTO','PRONTO')),
  criado_em      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_fila_costura ON ordens_ajuste (loja_id, prazo) WHERE status <> 'PRONTO';

CREATE TABLE limpezas (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id         UUID NOT NULL REFERENCES lojas(id),
  produto_id      UUID NOT NULL REFERENCES produtos(id),
  pedido_id       UUID REFERENCES pedidos(id),
  enviado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  retornado_em    TIMESTAMPTZ,
  responsavel_id  UUID REFERENCES usuarios(id),
  CHECK (retornado_em IS NULL OR retornado_em >= enviado_em)
);
CREATE INDEX idx_limpeza_pendente ON limpezas (loja_id) WHERE retornado_em IS NULL;

-- =============================================================================
-- 8. LIVRO CAIXA E RECEBÍVEIS
-- =============================================================================

CREATE TABLE transacoes (
  id                         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id                    UUID NOT NULL REFERENCES lojas(id),
  tipo                       TEXT NOT NULL CHECK (tipo IN ('ENTRADA','SAIDA')),
  natureza                   TEXT NOT NULL DEFAULT 'AVULSO' CHECK (natureza IN
                               ('ENTRADA_RESERVA','SALDO','PAGAMENTO_INTEGRAL','SERVICO_AJUSTE',
                                'DESPESA','ESTORNO','AVULSO')),
  valor_centavos             BIGINT NOT NULL CHECK (valor_centavos > 0),
  acrescimo_juros_centavos   BIGINT NOT NULL DEFAULT 0 CHECK (acrescimo_juros_centavos >= 0),
  forma_pagamento            TEXT NOT NULL CHECK (forma_pagamento IN ('DINHEIRO','PIX','DEBITO','CREDITO')),
  parcelas                   SMALLINT NOT NULL DEFAULT 1 CHECK (parcelas BETWEEN 1 AND 12),
  modalidade_cartao          TEXT CHECK (modalidade_cartao IN ('LOJISTA','EMISSOR')),
  bandeira                   TEXT,
  maquininha_id              SMALLINT REFERENCES maquininhas(id),
  nsu                        TEXT,
  codigo_autorizacao         TEXT,
  taxa_aplicada_pct          NUMERIC(6,3),
  valor_liquido_centavos     BIGINT,              -- NULL = "a confirmar"
  pedido_id                  UUID REFERENCES pedidos(id),
  estorno_de                 BIGINT UNIQUE REFERENCES transacoes(id),
  descricao                  TEXT NOT NULL,
  chave_idempotencia         UUID NOT NULL,
  usuario_id                 UUID NOT NULL REFERENCES usuarios(id),
  aprovado_por               UUID REFERENCES usuarios(id),
  criado_em                  TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (loja_id, chave_idempotencia),
  CONSTRAINT parcelado_so_no_credito CHECK (parcelas = 1 OR forma_pagamento = 'CREDITO'),
  CONSTRAINT credito_tem_modalidade CHECK (
    (forma_pagamento = 'CREDITO') = (modalidade_cartao IS NOT NULL)),
  CONSTRAINT juros_so_no_lojista CHECK (
    acrescimo_juros_centavos = 0
    OR (forma_pagamento = 'CREDITO' AND modalidade_cartao = 'LOJISTA' AND parcelas >= 2)),
  CONSTRAINT cartao_exige_maquininha CHECK (
    forma_pagamento NOT IN ('DEBITO','CREDITO') OR maquininha_id IS NOT NULL),
  CONSTRAINT estorno_coerente CHECK ((estorno_de IS NULL) = (natureza <> 'ESTORNO'))
);
CREATE INDEX idx_transacoes_caixa  ON transacoes (loja_id, criado_em DESC);
CREATE INDEX idx_transacoes_pedido ON transacoes (pedido_id) WHERE pedido_id IS NOT NULL;

-- Valor já pago de um pedido (entradas menos saídas vinculadas)
CREATE FUNCTION valor_pago(p_pedido UUID) RETURNS BIGINT LANGUAGE sql STABLE AS $$
  SELECT COALESCE(SUM(CASE WHEN tipo = 'ENTRADA' THEN valor_centavos
                           ELSE -valor_centavos END), 0)::bigint
    FROM transacoes WHERE pedido_id = p_pedido
$$;

CREATE TRIGGER trg_transacoes_imutaveis BEFORE UPDATE OR DELETE ON transacoes
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

CREATE FUNCTION validar_transacao() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  p      pedidos%ROWTYPE;
  orig   transacoes%ROWTYPE;
  cfg    config_pagamento%ROWTYPE;
  vinc   TEXT;
  saldo  BIGINT;
BEGIN
  SELECT vinculo INTO vinc FROM usuarios WHERE id = NEW.usuario_id;
  IF vinc = 'FREELANCE' THEN
    RAISE EXCEPTION 'Freelance não opera o caixa.';
  END IF;
  IF NOT tem_papel(NEW.usuario_id, ARRAY['ATENDENTE','GERENTE','DONO','FINANCEIRO']) THEN
    RAISE EXCEPTION 'Sem permissão para lançar no caixa.';
  END IF;

  -- Estorno: inverso exato do original, aprovado por outra pessoa
  IF NEW.estorno_de IS NOT NULL THEN
    SELECT * INTO orig FROM transacoes WHERE id = NEW.estorno_de;
    IF orig.estorno_de IS NOT NULL THEN
      RAISE EXCEPTION 'Não se estorna um estorno. Faça um novo lançamento.';
    END IF;
    IF NEW.tipo = orig.tipo OR NEW.valor_centavos <> orig.valor_centavos THEN
      RAISE EXCEPTION 'O estorno precisa ter tipo inverso e o mesmo valor do original.';
    END IF;
    IF NEW.aprovado_por IS NULL
       OR NOT tem_papel(NEW.aprovado_por, ARRAY['DONO','GERENTE','FINANCEIRO']) THEN
      RAISE EXCEPTION 'Estorno precisa de aprovação da Dona, Gerente ou Financeiro.';
    END IF;
    IF NEW.aprovado_por = orig.usuario_id THEN
      RAISE EXCEPTION 'Ninguém aprova o estorno do próprio lançamento.';
    END IF;
    NEW.pedido_id := orig.pedido_id;
    RETURN NEW;
  END IF;

  -- Vínculo com pedido: limite de parcelas, parcela mínima, sem pagar a mais
  IF NEW.pedido_id IS NOT NULL THEN
    SELECT * INTO p FROM pedidos WHERE id = NEW.pedido_id FOR UPDATE;
    SELECT * INTO cfg FROM config_pagamento WHERE id = p.config_id;

    IF p.status = 'CANCELADO' AND NEW.tipo = 'ENTRADA' THEN
      RAISE EXCEPTION 'Pedido cancelado não recebe pagamentos.';
    END IF;
    IF NEW.parcelas > p.max_parcelas_permitidas THEN
      RAISE EXCEPTION 'Este pedido permite até %x.', p.max_parcelas_permitidas;
    END IF;
    IF NEW.parcelas > 1
       AND (NEW.valor_centavos + NEW.acrescimo_juros_centavos) / NEW.parcelas
           < cfg.valor_minimo_parcela_centavos THEN
      RAISE EXCEPTION 'Parcela abaixo do mínimo de R$ %.',
        to_char(cfg.valor_minimo_parcela_centavos / 100.0, 'FM999G990D00');
    END IF;
    IF NEW.tipo = 'ENTRADA' THEN
      saldo := p.valor_total_centavos - valor_pago(p.id);
      IF NEW.valor_centavos > saldo THEN
        RAISE EXCEPTION 'Pagamento maior que o saldo do pedido (R$ %).',
          to_char(saldo / 100.0, 'FM999G999G990D00');
      END IF;
    END IF;
  END IF;

  -- Saída de caixa pela atendente: acima do teto precisa de aprovação
  IF NEW.tipo = 'SAIDA'
     AND NOT tem_papel(NEW.usuario_id, ARRAY['DONO','GERENTE','FINANCEIRO']) THEN
    SELECT * INTO cfg FROM config_pagamento WHERE id = config_vigente(NEW.loja_id);
    IF NEW.valor_centavos > COALESCE(cfg.teto_saida_atendente_centavos, 0)
       AND (NEW.aprovado_por IS NULL OR NOT tem_papel(NEW.aprovado_por, ARRAY['DONO','GERENTE'])) THEN
      RAISE EXCEPTION 'Saída acima do limite: chame a Gerente ou a Dona.';
    END IF;
  END IF;

  RETURN NEW;
END $$;

CREATE TRIGGER trg_validar_transacao BEFORE INSERT ON transacoes
  FOR EACH ROW EXECUTE FUNCTION validar_transacao();

CREATE TABLE recebiveis (
  id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id                 UUID NOT NULL REFERENCES lojas(id),
  transacao_id            BIGINT NOT NULL REFERENCES transacoes(id),
  parcela_num             SMALLINT NOT NULL CHECK (parcela_num >= 1),
  valor_liquido_centavos  BIGINT NOT NULL,
  data_prevista           DATE NOT NULL,
  data_recebida           DATE,
  status                  TEXT NOT NULL DEFAULT 'PREVISTO'
                          CHECK (status IN ('PREVISTO','RECEBIDO','DIVERGENTE')),
  UNIQUE (transacao_id, parcela_num)
);
CREATE INDEX idx_recebiveis_agenda ON recebiveis (loja_id, data_prevista) WHERE status = 'PREVISTO';

-- =============================================================================
-- 9. VIEWS
-- =============================================================================

CREATE VIEW pedidos_saldo WITH (security_invoker = true) AS
SELECT p.id AS pedido_id,
       p.loja_id,
       p.valor_total_centavos,
       valor_pago(p.id)                           AS pago_centavos,
       p.valor_total_centavos - valor_pago(p.id)  AS saldo_centavos
  FROM pedidos p;

-- Situação da peça HOJE, calculada pelos pedidos ativos
CREATE VIEW produtos_situacao WITH (security_invoker = true) AS
SELECT pr.*,
       CASE
         WHEN pr.status <> 'DISPONIVEL' THEN pr.status
         WHEN pe.status = 'ALUGADO'                     THEN 'ALUGADO'
         WHEN pe.status = 'EM_AJUSTE'                   THEN 'EM_AJUSTE'
         WHEN pe.status IN ('DEVOLVIDO','EM_LIMPEZA')   THEN 'EM_LIMPEZA'
         WHEN pe.status IS NOT NULL                     THEN 'RESERVADO'
         ELSE 'DISPONIVEL'
       END AS situacao_hoje,
       pe.id AS pedido_atual_id
  FROM produtos pr
  LEFT JOIN LATERAL (
    SELECT id, status FROM pedidos
     WHERE produto_id = pr.id
       AND status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO')
       AND periodo_bloqueado @> current_date
     LIMIT 1
  ) pe ON true;

-- =============================================================================
-- 10. PAPÉIS E PERMISSÕES (sementes)
-- =============================================================================

INSERT INTO papeis (codigo, nome, diretoria) VALUES
  ('ATENDENTE',        'Atendente',              false),
  ('COSTUREIRA',       'Costureira',             false),
  ('AJUDANTE_COSTURA', 'Ajudante de costura',    false),
  ('GERENTE',          'Gerente',                false),
  ('LIMPEZA',          'Limpeza',                false),
  ('DONO',             'Diretoria: Dono',        true),
  ('RH',               'Diretoria: RH',          true),
  ('FINANCEIRO',       'Diretoria: Financeiro',  true),
  ('DESENVOLVEDOR',    'Desenvolvedor',          false);

INSERT INTO permissoes (codigo, descricao) VALUES
  ('catalogo.ver',              'Ver catálogo'),
  ('catalogo.editar',           'Cadastrar e editar peças'),
  ('catalogo.arquivar',         'Arquivar peças'),
  ('clientes.ver',              'Ver clientes'),
  ('clientes.editar',           'Cadastrar clientes'),
  ('pedidos.ver',               'Ver pedidos'),
  ('pedidos.criar',             'Criar aluguel ou venda'),
  ('pedidos.cancelar',          'Cancelar pedidos'),
  ('agenda.ver',                'Ver agenda'),
  ('agenda.editar',             'Agendar provas e retirada'),
  ('entregas.registrar',        'Registrar entrega e devolução'),
  ('medidas.ver',               'Ver medidas'),
  ('medidas.editar',            'Tirar e editar medidas'),
  ('ajustes.ver',               'Ver fila de ajustes'),
  ('ajustes.atualizar',         'Atualizar fila de ajustes'),
  ('limpeza.ver',               'Ver limpeza'),
  ('limpeza.registrar',         'Enviar e receber da limpeza'),
  ('caixa.entrada',             'Lançar entrada'),
  ('caixa.saida',               'Lançar saída até o teto'),
  ('caixa.saida_ilimitada',     'Lançar saída sem teto'),
  ('caixa.estornar',            'Aprovar estornos'),
  ('caixa.fechar',              'Fechar caixa do dia'),
  ('relatorios.dia',            'Resumo do dia'),
  ('relatorios.completo',       'Relatórios financeiros completos'),
  ('descontos.pedir',           'Pedir desconto ou exceção de entrada'),
  ('descontos.aprovar',         'Aprovar desconto ou exceção de entrada'),
  ('config.pagamento',          'Configurar entrada, parcelas, juros, teto de desconto'),
  ('config.maquininhas',        'Cadastrar maquininhas e taxas'),
  ('config.ver',                'Ver configurações'),
  ('conciliacao',               'Conciliar recebíveis'),
  ('fiscal.editar',             'Editar perfis fiscais'),
  ('rh.funcionarios',           'Dados de funcionários'),
  ('usuarios.operacionais',     'Criar e desativar usuários operacionais'),
  ('usuarios.diretoria',        'Atribuir papéis de Diretoria'),
  ('backup.fazer',              'Fazer backup'),
  ('backup.restaurar',          'Executar restauração (com aprovação)'),
  ('backup.aprovar_restauracao','Aprovar restauração'),
  ('auditoria.ver',             'Ver auditoria'),
  ('sistema.ver_tudo',          'Visão somente leitura de todas as áreas, dados pessoais mascarados');

INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id
  FROM (VALUES
    ('ATENDENTE', ARRAY['catalogo.ver','catalogo.editar','clientes.ver','clientes.editar',
                        'pedidos.ver','pedidos.criar','agenda.ver','agenda.editar',
                        'entregas.registrar','medidas.ver','limpeza.ver','limpeza.registrar',
                        'caixa.entrada','caixa.saida','caixa.fechar','descontos.pedir']),
    ('COSTUREIRA', ARRAY['catalogo.ver','agenda.ver','medidas.ver','medidas.editar',
                         'ajustes.ver','ajustes.atualizar']),
    ('AJUDANTE_COSTURA', ARRAY['catalogo.ver','agenda.ver','medidas.ver',
                               'ajustes.ver','ajustes.atualizar']),
    ('GERENTE', ARRAY['catalogo.ver','catalogo.editar','catalogo.arquivar','clientes.ver',
                      'clientes.editar','pedidos.ver','pedidos.criar','pedidos.cancelar',
                      'agenda.ver','agenda.editar','entregas.registrar','medidas.ver',
                      'medidas.editar','ajustes.ver','ajustes.atualizar','limpeza.ver',
                      'limpeza.registrar','caixa.entrada','caixa.saida','caixa.saida_ilimitada',
                      'caixa.estornar','caixa.fechar','relatorios.dia','descontos.pedir',
                      'descontos.aprovar','config.pagamento','config.maquininhas','config.ver']),
    ('LIMPEZA', ARRAY['catalogo.ver','limpeza.ver','limpeza.registrar']),
    ('DONO', ARRAY['catalogo.ver','catalogo.editar','catalogo.arquivar','clientes.ver',
                   'clientes.editar','pedidos.ver','pedidos.criar','pedidos.cancelar',
                   'agenda.ver','agenda.editar','entregas.registrar','medidas.ver',
                   'ajustes.ver','limpeza.ver','caixa.entrada','caixa.saida',
                   'caixa.saida_ilimitada','caixa.estornar','caixa.fechar','relatorios.dia',
                   'relatorios.completo','descontos.pedir','descontos.aprovar',
                   'config.pagamento','config.maquininhas','config.ver','conciliacao',
                   'fiscal.editar','rh.funcionarios','usuarios.operacionais',
                   'usuarios.diretoria','backup.aprovar_restauracao','auditoria.ver']),
    ('RH', ARRAY['catalogo.ver','rh.funcionarios','usuarios.operacionais']),
    ('FINANCEIRO', ARRAY['catalogo.ver','clientes.ver','pedidos.ver','caixa.entrada',
                         'caixa.saida','caixa.saida_ilimitada','caixa.estornar','caixa.fechar',
                         'relatorios.dia','relatorios.completo','config.ver','conciliacao',
                         'fiscal.editar','auditoria.ver']),
    ('DESENVOLVEDOR', ARRAY['sistema.ver_tudo','catalogo.ver','clientes.ver','pedidos.ver',
                            'agenda.ver','ajustes.ver','limpeza.ver','relatorios.completo',
                            'config.ver','usuarios.operacionais','backup.fazer',
                            'backup.restaurar','auditoria.ver'])
  ) AS m(papel, perms)
  CROSS JOIN LATERAL unnest(m.perms) AS perm(codigo)
  JOIN papeis     pa ON pa.codigo = m.papel
  JOIN permissoes pe ON pe.codigo = perm.codigo;

-- =============================================================================
-- 11. PAPEL DO BANCO PARA O APP, PERMISSÕES E ROW-LEVEL SECURITY
-- =============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user LOGIN;   -- senha definida fora do código
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO app_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO app_user;

-- INSERT onde o app cria registros (nada de catálogo de papéis/permissões/transições)
GRANT INSERT ON lojas, usuarios, usuario_papeis, auditoria, perfis_fiscais, categorias,
                produtos, clientes, medidas, config_pagamento, config_juros, maquininhas,
                taxas_maquininha, pedidos, historico_status, agendamentos, ordens_ajuste,
                limpezas, transacoes, recebiveis
      TO app_user;

-- UPDATE só em tabelas mutáveis. Livro caixa, auditoria, histórico, medidas,
-- configurações e taxas NÃO recebem UPDATE. Nenhuma tabela recebe DELETE.
GRANT UPDATE ON lojas, usuarios, usuario_papeis, perfis_fiscais, categorias, produtos,
                clientes, maquininhas, pedidos, agendamentos, ordens_ajuste, limpezas,
                recebiveis
      TO app_user;

-- Row-Level Security: o app só enxerga e grava dados da loja da sessão
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'usuarios','usuario_papeis','auditoria','perfis_fiscais','categorias','produtos',
    'clientes','medidas','config_pagamento','config_juros','maquininhas','taxas_maquininha',
    'pedidos','historico_status','agendamentos','ordens_ajuste','limpezas','transacoes',
    'recebiveis']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY isolamento_loja ON %I TO app_user
         USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja())', t);
  END LOOP;
END $$;

ALTER TABLE lojas ENABLE ROW LEVEL SECURITY;
CREATE POLICY isolamento_loja ON lojas TO app_user
  USING (id = ctx_loja()) WITH CHECK (id = ctx_loja());

