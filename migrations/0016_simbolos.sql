-- 0016 — Símbolos oficiais das redes sociais e das formas de pagamento (Instagram, Visa, Pix...) no site.
-- A loja envia o arquivo oficial baixado do kit de marca de cada empresa; sem arquivo, aparece o nome.
CREATE TABLE simbolos (
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  chave        TEXT NOT NULL CHECK (chave ~ '^[a-z0-9-]{2,40}$'),
  arquivo      TEXT NOT NULL CHECK (arquivo ~ '^[0-9a-f]{32}$'),
  definido_por UUID NOT NULL REFERENCES usuarios(id),
  definido_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (loja_id, chave)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON simbolos TO app_user;
ALTER TABLE simbolos ENABLE ROW LEVEL SECURITY;
CREATE POLICY isolamento_loja ON simbolos TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja());
