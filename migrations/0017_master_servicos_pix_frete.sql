-- 0017 — Usuário MASTER (acesso a tudo), serviços avulsos (recebimento sempre com motivo cadastrado),
--        ocasião principal da peça, venda com ajuste pela sacola, Pix da loja como link de pagamento
--        (qualquer banco: Inter, Itaú…), token do Melhor Envio guardado cifrado pela tela e sacola ligada
--        sozinha para quem já configurou o Mercado Pago.

-- --------------------------------------------------------------------------- --
-- MASTER: tem todas as permissões (inclusive as que vierem depois) e passa em toda trava de cargo do banco
-- --------------------------------------------------------------------------- --
INSERT INTO papeis (codigo, nome, diretoria) VALUES ('MASTER', 'Diretoria: Master (acesso total)', true)
ON CONFLICT (codigo) DO NOTHING;

CREATE OR REPLACE FUNCTION tem_papel(uid UUID, codigos TEXT[]) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1
      FROM usuario_papeis up
      JOIN papeis   p ON p.id = up.papel_id
      JOIN usuarios u ON u.id = up.usuario_id
     WHERE up.usuario_id = uid
       AND (p.codigo = ANY (codigos) OR p.codigo = 'MASTER')
       AND u.ativo
       AND (up.valido_ate IS NULL OR up.valido_ate > now())
  )
$$;

INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id FROM papeis pa, permissoes pe WHERE pa.codigo = 'MASTER'
ON CONFLICT DO NOTHING;

CREATE FUNCTION master_ganha_permissao() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO papel_permissoes (papel_id, permissao_id)
  SELECT id, NEW.id FROM papeis WHERE codigo = 'MASTER' ON CONFLICT DO NOTHING;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_master_ganha_permissao AFTER INSERT ON permissoes FOR EACH ROW EXECUTE FUNCTION master_ganha_permissao();

-- Só um Dono ou outro Master concede o MASTER
DO $$
DECLARE def TEXT;
BEGIN
  def := pg_get_functiondef('validar_concessao_papel'::regproc);
  IF position($x$IF papel.codigo = 'DESENVOLVEDOR'$x$ IN def) = 0 THEN
    RAISE EXCEPTION 'validar_concessao_papel mudou: revise a migração 0017';
  END IF;
  def := replace(def, $x$IF papel.codigo = 'DESENVOLVEDOR'$x$,
    $x$IF papel.codigo = 'MASTER' AND NOT EXISTS (
       SELECT 1 FROM usuario_papeis up JOIN papeis p ON p.id = up.papel_id JOIN usuarios u ON u.id = up.usuario_id
        WHERE up.usuario_id = NEW.concedido_por AND p.codigo IN ('DONO','MASTER') AND u.ativo) THEN
    RAISE EXCEPTION 'Só a Dona ou um Master concede o acesso Master.';
  END IF;

  IF papel.codigo = 'DESENVOLVEDOR'$x$);
  EXECUTE def;
END $$;

-- --------------------------------------------------------------------------- --
-- Serviços avulsos: "Recebi dinheiro" sem pedido só com um serviço cadastrado e a cliente
-- --------------------------------------------------------------------------- --
CREATE TABLE servicos (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id       UUID NOT NULL REFERENCES lojas(id),
  nome          TEXT NOT NULL CHECK (length(nome) BETWEEN 2 AND 80),
  preco_centavos BIGINT CHECK (preco_centavos IS NULL OR preco_centavos > 0),
  descricao     TEXT CHECK (length(descricao) <= 200),
  ativo         BOOLEAN NOT NULL DEFAULT true,
  criado_em     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_servico_nome ON servicos (loja_id, lower(nome)) WHERE ativo;
INSERT INTO servicos (loja_id, nome, descricao)
SELECT l.id, s.nome, s.descr FROM lojas l,
  (VALUES ('Ajuste / conserto avulso', 'Barra, alça, zíper em roupa da cliente'),
          ('Limpeza de peça', 'Lavanderia de roupa da cliente'),
          ('Manutenção de peça', 'Reparo de peça da cliente'),
          ('Aluguel de acessório', 'Bolsa, tiara, véu, gravata'),
          ('Taxa de atraso', 'Devolução depois da data do contrato'),
          ('Taxa de dano', 'Mancha, rasgo ou perda, conforme contrato')) AS s(nome, descr);

ALTER TABLE transacoes
  ADD COLUMN servico_id UUID REFERENCES servicos(id),
  ADD COLUMN cliente_id UUID REFERENCES clientes(id);
ALTER TABLE transacoes DROP CONSTRAINT transacoes_natureza_check;
ALTER TABLE transacoes ADD CONSTRAINT transacoes_natureza_check CHECK (natureza IN
  ('ENTRADA_RESERVA','SALDO','PAGAMENTO_INTEGRAL','SERVICO_AJUSTE','DESPESA','ESTORNO','AVULSO','DEVOLUCAO_CLIENTE',
   'PARCELA_CREDIARIO','FRETE','SERVICO'));
-- Daqui para frente: entrada avulsa precisa de serviço cadastrado (os lançamentos antigos continuam como estão)
ALTER TABLE transacoes ADD CONSTRAINT entrada_avulsa_com_motivo CHECK (
  NOT (tipo = 'ENTRADA' AND natureza = 'AVULSO') OR servico_id IS NOT NULL) NOT VALID;
ALTER TABLE transacoes ADD CONSTRAINT servico_com_cliente CHECK (
  natureza <> 'SERVICO' OR (servico_id IS NOT NULL AND cliente_id IS NOT NULL));

-- --------------------------------------------------------------------------- --
-- Ocasião principal da peça (o site usa para "Escolha pela ocasião" sem repetir a mesma peça)
-- --------------------------------------------------------------------------- --
ALTER TABLE produtos ADD COLUMN ocasiao_principal SMALLINT REFERENCES ocasioes(id);
UPDATE produtos SET ocasiao_principal = ocasioes[1] WHERE ocasioes IS NOT NULL AND array_length(ocasioes, 1) > 0;

-- --------------------------------------------------------------------------- --
-- Sacola: compra com ajuste na loja (acréscimo) e Pix da loja
-- --------------------------------------------------------------------------- --
ALTER TABLE lojas ADD COLUMN ajuste_site_centavos BIGINT CHECK (ajuste_site_centavos IS NULL OR ajuste_site_centavos > 0);
ALTER TABLE compras_itens ADD COLUMN ajuste_centavos BIGINT NOT NULL DEFAULT 0 CHECK (ajuste_centavos >= 0);
ALTER TABLE compras_itens DROP CONSTRAINT compras_itens_check2;          -- venda: paga tudo (peça + ajuste)
ALTER TABLE compras_itens ADD CONSTRAINT venda_paga_inteira CHECK (
  tipo <> 'VENDA' OR valor_pagar_centavos = valor_peca_centavos + ajuste_centavos);
ALTER TABLE compras_itens DROP CONSTRAINT compras_itens_check;           -- nunca mais que o total
ALTER TABLE compras_itens ADD CONSTRAINT pagar_ate_o_total CHECK (
  valor_pagar_centavos > 0 AND valor_pagar_centavos <= valor_peca_centavos + ajuste_centavos);
ALTER TABLE compras_site ADD COLUMN meio TEXT NOT NULL DEFAULT 'MERCADO_PAGO' CHECK (meio IN ('MERCADO_PAGO','PIX_LOJA'));
ALTER TABLE compras_site ADD COLUMN pix_copia_cola TEXT CHECK (length(pix_copia_cola) <= 400);

-- Link de pagamento pelo Pix da loja (qualquer banco): a loja confere no extrato e confirma
ALTER TABLE links_pagamento DROP CONSTRAINT links_pagamento_provedor_check;
ALTER TABLE links_pagamento ADD CONSTRAINT links_pagamento_provedor_check CHECK (provedor IN ('MERCADO_PAGO','PIX_LOJA'));
ALTER TABLE links_pagamento ADD COLUMN pix_copia_cola TEXT CHECK (length(pix_copia_cola) <= 400);

-- --------------------------------------------------------------------------- --
-- Melhor Envio: token colado na tela (cifrado; ninguém lê de volta)
-- --------------------------------------------------------------------------- --
CREATE TABLE frete_config (
  loja_id       UUID PRIMARY KEY REFERENCES lojas(id),
  token_cifrado BYTEA NOT NULL,
  token_final   TEXT NOT NULL,
  sandbox       BOOLEAN NOT NULL DEFAULT false,
  conta         TEXT,
  definido_por  UUID NOT NULL REFERENCES usuarios(id),
  definido_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE frete_config ENABLE ROW LEVEL SECURITY;
CREATE POLICY isolamento_loja ON frete_config TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja());
GRANT SELECT (loja_id, token_final, sandbox, conta, definido_por, definido_em) ON frete_config TO app_user;
GRANT INSERT, UPDATE, DELETE ON frete_config TO app_user;
CREATE FUNCTION token_frete(p_chave TEXT) RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT pgp_sym_decrypt(token_cifrado, p_chave) FROM frete_config WHERE loja_id = ctx_loja()
$$;
REVOKE EXECUTE ON FUNCTION token_frete(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION token_frete(TEXT) TO app_user;

-- Quem já ligou o Mercado Pago: a sacola do site já vem ligada (a Dona desliga em Sacola e entrega se quiser)
UPDATE lojas l SET sacola_ativa = true
 WHERE EXISTS (SELECT 1 FROM pagamento_online p WHERE p.loja_id = l.id AND p.ativo);

GRANT SELECT, INSERT, UPDATE ON servicos TO app_user;
GRANT UPDATE (ajuste_site_centavos) ON lojas TO app_user;
GRANT UPDATE (ocasiao_principal) ON produtos TO app_user;
ALTER TABLE servicos ENABLE ROW LEVEL SECURITY;
CREATE POLICY isolamento_loja ON servicos TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja());
INSERT INTO permissoes (codigo, descricao) VALUES ('servicos.editar', 'Cadastrar serviços avulsos') ON CONFLICT DO NOTHING;
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id FROM papeis pa, permissoes pe
 WHERE pa.codigo IN ('DONO','GERENTE') AND pe.codigo = 'servicos.editar'
ON CONFLICT DO NOTHING;

-- Loja nova já nasce com os serviços básicos
CREATE FUNCTION servicos_padrao() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO servicos (loja_id, nome, descricao)
  VALUES (NEW.id, 'Ajuste / conserto avulso', 'Barra, alça, zíper em roupa da cliente'),
         (NEW.id, 'Limpeza de peça', 'Lavanderia de roupa da cliente'),
         (NEW.id, 'Manutenção de peça', 'Reparo de peça da cliente'),
         (NEW.id, 'Aluguel de acessório', 'Bolsa, tiara, véu, gravata'),
         (NEW.id, 'Taxa de atraso', 'Devolução depois da data do contrato'),
         (NEW.id, 'Taxa de dano', 'Mancha, rasgo ou perda, conforme contrato');
  RETURN NULL;
END $$;
CREATE TRIGGER trg_servicos_padrao AFTER INSERT ON lojas FOR EACH ROW EXECUTE FUNCTION servicos_padrao();
