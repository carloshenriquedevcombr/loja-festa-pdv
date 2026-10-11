-- 0010 — Marca (logo e ícone), rodapé do site (lojas/unidades, páginas institucionais, novidades),
--        formas de pagamento e parcelamento mostrados no site.

ALTER TABLE lojas
  ADD COLUMN logo_arquivo        TEXT CHECK (logo_arquivo  IS NULL OR logo_arquivo  ~ '^[0-9a-f]{32}$'),
  ADD COLUMN icone_arquivo       TEXT CHECK (icone_arquivo IS NULL OR icone_arquivo ~ '^[0-9a-f]{32}$'),
  ADD COLUMN email_contato       TEXT CHECK (email_contato IS NULL OR email_contato ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  ADD COLUMN sobre_curto         TEXT CHECK (length(sobre_curto) <= 400),
  ADD COLUMN formas_site         TEXT[] NOT NULL DEFAULT '{Pix,Dinheiro,Cartão de débito,Cartão de crédito}',
  ADD COLUMN bandeiras_site      TEXT[] NOT NULL DEFAULT '{Visa,Mastercard,Elo,American Express,Hipercard}',
  ADD COLUMN parcelas_no_site    BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN novidades_ativa     BOOLEAN NOT NULL DEFAULT true;

-- Lojas físicas (a marca pode ter mais de uma). Nada é apagado: "ativa = false" tira do site.
CREATE TABLE unidades (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id     UUID NOT NULL REFERENCES lojas(id),
  nome        TEXT NOT NULL CHECK (length(nome) BETWEEN 2 AND 80),
  endereco    TEXT NOT NULL CHECK (length(endereco) BETWEEN 5 AND 200),
  cidade      TEXT CHECK (length(cidade) <= 80),
  telefone    TEXT CHECK (telefone IS NULL OR telefone ~ '^\d{10,11}$'),
  whatsapp    TEXT CHECK (whatsapp IS NULL OR whatsapp ~ '^\d{10,11}$'),
  horario     TEXT CHECK (length(horario) <= 120),
  mapa_link   TEXT CHECK (mapa_link IS NULL OR mapa_link ~ '^https://(www\.)?(google\.[a-z.]+/maps|maps\.google\.[a-z.]+|maps\.app\.goo\.gl|goo\.gl/maps)/'),
  principal   BOOLEAN NOT NULL DEFAULT false,
  ordem       SMALLINT NOT NULL DEFAULT 0,
  ativa       BOOLEAN NOT NULL DEFAULT true,
  criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_unidade_principal ON unidades (loja_id) WHERE principal AND ativa;

-- Páginas do rodapé (Quem somos, Trocas e devoluções, Termos, Privacidade): texto da loja, com histórico
CREATE TABLE paginas (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id         UUID NOT NULL REFERENCES lojas(id),
  slug            TEXT NOT NULL CHECK (slug IN ('quem-somos','trocas-e-devolucoes','termos-de-uso','politica-de-privacidade')),
  texto           TEXT NOT NULL CHECK (length(texto) BETWEEN 20 AND 20000),
  definido_por    UUID NOT NULL REFERENCES usuarios(id),
  definido_em     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_paginas_vigente ON paginas (loja_id, slug, definido_em DESC);
CREATE TRIGGER trg_paginas_imutaveis BEFORE UPDATE OR DELETE ON paginas FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- Texto do contrato também com histórico (quem mudou e quando); o contrato assinado guarda o texto do dia
CREATE TABLE contrato_modelos (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id         UUID NOT NULL REFERENCES lojas(id),
  texto           TEXT NOT NULL CHECK (length(texto) BETWEEN 200 AND 30000),
  definido_por    UUID NOT NULL REFERENCES usuarios(id),
  definido_em     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_contrato_modelos_imutaveis BEFORE UPDATE OR DELETE ON contrato_modelos
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- "Receba nossas novidades": dados cifrados, com consentimento e saída a qualquer momento
CREATE TABLE inscritos (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id          UUID NOT NULL REFERENCES lojas(id),
  nome_c           BYTEA NOT NULL,
  telefone_c       BYTEA,
  telefone_idx     TEXT,
  email_c          BYTEA,
  email_idx        TEXT,
  consentimento_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  origem_hash      TEXT,
  saiu_em          TIMESTAMPTZ,
  CHECK (telefone_c IS NOT NULL OR email_c IS NOT NULL)
);
CREATE UNIQUE INDEX uq_inscritos_email ON inscritos (loja_id, email_idx) WHERE email_idx IS NOT NULL;
CREATE UNIQUE INDEX uq_inscritos_fone  ON inscritos (loja_id, telefone_idx) WHERE telefone_idx IS NOT NULL;

GRANT SELECT, INSERT, UPDATE ON unidades, inscritos TO app_user;
GRANT SELECT, INSERT ON paginas, contrato_modelos TO app_user;
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['unidades','paginas','contrato_modelos','inscritos'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY isolamento_loja ON %I TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja())', t);
  END LOOP;
END $$;

-- Endereço antigo da loja vira a unidade principal
INSERT INTO unidades (loja_id, nome, endereco, telefone, horario, principal)
SELECT id, nome, endereco, NULLIF(regexp_replace(coalesce(telefone, ''), '\D', '', 'g'), ''), horario, true
  FROM lojas WHERE endereco IS NOT NULL AND length(endereco) >= 5
   AND (regexp_replace(coalesce(telefone, ''), '\D', '', 'g') = '' OR regexp_replace(telefone, '\D', '', 'g') ~ '^\d{10,11}$');

INSERT INTO permissoes (codigo, descricao) VALUES ('contrato.editar', 'Alterar o texto do contrato') ON CONFLICT DO NOTHING;
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id FROM papeis pa, permissoes pe
 WHERE pa.codigo IN ('DONO','GERENTE') AND pe.codigo = 'contrato.editar'
ON CONFLICT DO NOTHING;
