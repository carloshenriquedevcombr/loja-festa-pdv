-- 0014 — Site: conta da cliente (código no WhatsApp ou e-mail e senha), sacola e pagamento no site
--        (Mercado Pago), entrega (retirada, motoboy/aplicativo nas cidades da loja, Melhor Envio).
--
-- Dados de pagamento (cartão, Pix, boleto) NUNCA passam pelo nosso servidor: a cliente paga na página do
-- Mercado Pago. Aqui ficam só o valor, a forma (Pix/crédito/débito) e o número do pagamento.

-- --------------------------------------------------------------------------- --
-- "Usuário" do sistema que registra o que o site faz sozinho (pedido e pagamento online).
-- Não entra no sistema (sem senha válida) e não aparece na equipe.
-- --------------------------------------------------------------------------- --
ALTER TABLE usuarios ADD COLUMN sistema BOOLEAN NOT NULL DEFAULT false;

CREATE FUNCTION usuario_site() RETURNS UUID LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE uid UUID;
BEGIN
  IF ctx_loja() IS NULL THEN RAISE EXCEPTION 'Loja não definida.'; END IF;
  SELECT id INTO uid FROM usuarios WHERE loja_id = ctx_loja() AND sistema;
  IF uid IS NULL THEN
    INSERT INTO usuarios (loja_id, nome, login, senha_hash, ativo, sistema)
    VALUES (ctx_loja(), 'Loja online (site)', 'sistema-site', '$argon2id$sem-senha-nao-entra', true, true)
    RETURNING id INTO uid;
  END IF;
  RETURN uid;
END $$;
REVOKE EXECUTE ON FUNCTION usuario_site() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION usuario_site() TO app_user;

-- O caixa aceita ENTRADA lançada pelo site (pagamento online confirmado), nada mais.
DO $$
DECLARE def TEXT;
BEGIN
  def := pg_get_functiondef('validar_transacao'::regproc);
  IF position($x$IF NOT tem_papel(NEW.usuario_id, ARRAY['ATENDENTE','GERENTE','DONO','FINANCEIRO']) THEN$x$ IN def) = 0 THEN
    RAISE EXCEPTION 'validar_transacao mudou: revise a migração 0014';
  END IF;
  def := replace(def, $x$IF NOT tem_papel(NEW.usuario_id, ARRAY['ATENDENTE','GERENTE','DONO','FINANCEIRO']) THEN$x$,
    $x$IF NOT tem_papel(NEW.usuario_id, ARRAY['ATENDENTE','GERENTE','DONO','FINANCEIRO'])
     AND NOT (NEW.tipo = 'ENTRADA' AND NEW.estorno_de IS NULL
              AND EXISTS (SELECT 1 FROM usuarios su WHERE su.id = NEW.usuario_id AND su.sistema)) THEN$x$);
  EXECUTE def;
END $$;

-- --------------------------------------------------------------------------- --
-- Conta da cliente no site
-- --------------------------------------------------------------------------- --
CREATE TABLE contas_clientes (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id         UUID NOT NULL REFERENCES lojas(id),
  cliente_id      UUID NOT NULL UNIQUE REFERENCES clientes(id),
  email_idx       TEXT,                                   -- índice cego do e-mail (login); o e-mail fica cifrado em clientes
  senha_hash      TEXT CHECK (senha_hash IS NULL OR senha_hash LIKE '$argon2id$%'),
  falhas          SMALLINT NOT NULL DEFAULT 0,
  travada_ate     TIMESTAMPTZ,
  ativa           BOOLEAN NOT NULL DEFAULT true,
  criada_em       TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultimo_acesso   TIMESTAMPTZ,
  exclusao_pedida_em TIMESTAMPTZ
);
CREATE UNIQUE INDEX uq_conta_email ON contas_clientes (loja_id, email_idx) WHERE email_idx IS NOT NULL;

CREATE TABLE sessoes_clientes (
  id           TEXT PRIMARY KEY,                           -- hash do identificador que vai no cookie cifrado
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  conta_id     UUID NOT NULL REFERENCES contas_clientes(id),
  navegador    TEXT NOT NULL,
  criada_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
  visto_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
  expira_em    TIMESTAMPTZ NOT NULL,
  encerrada_em TIMESTAMPTZ,
  motivo       TEXT
);
CREATE INDEX idx_sessoes_clientes_conta ON sessoes_clientes (conta_id) WHERE encerrada_em IS NULL;

-- --------------------------------------------------------------------------- --
-- Entrega e regras da sacola
-- --------------------------------------------------------------------------- --
ALTER TABLE lojas
  ADD COLUMN sacola_ativa         BOOLEAN NOT NULL DEFAULT false,      -- a Dona liga quando o pagamento online estiver pronto
  ADD COLUMN sacola_pagamento     TEXT NOT NULL DEFAULT 'ENTRADA_OU_TOTAL' CHECK (sacola_pagamento IN ('ENTRADA_OU_TOTAL','TOTAL')),
  ADD COLUMN sacola_minutos       SMALLINT NOT NULL DEFAULT 60 CHECK (sacola_minutos BETWEEN 15 AND 1440),
  ADD COLUMN entrega_retirada     BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN entrega_envio        BOOLEAN NOT NULL DEFAULT false,      -- Melhor Envio (só venda)
  ADD COLUMN cep_origem           TEXT CHECK (cep_origem IS NULL OR cep_origem ~ '^\d{8}$'),
  ADD COLUMN pacote_peso_g        INTEGER NOT NULL DEFAULT 1500 CHECK (pacote_peso_g BETWEEN 100 AND 30000),
  ADD COLUMN pacote_altura_cm     SMALLINT NOT NULL DEFAULT 10 CHECK (pacote_altura_cm BETWEEN 2 AND 100),
  ADD COLUMN pacote_largura_cm    SMALLINT NOT NULL DEFAULT 30 CHECK (pacote_largura_cm BETWEEN 10 AND 100),
  ADD COLUMN pacote_comprimento_cm SMALLINT NOT NULL DEFAULT 40 CHECK (pacote_comprimento_cm BETWEEN 15 AND 100);

-- Cidades onde a loja entrega por motoboy ou aplicativo (99, Uber Flash…), com o valor que a Dona escolhe
CREATE TABLE entrega_cidades (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id     UUID NOT NULL REFERENCES lojas(id),
  cidade      TEXT NOT NULL CHECK (length(cidade) BETWEEN 2 AND 80),
  uf          TEXT NOT NULL CHECK (uf ~ '^[A-Z]{2}$'),
  meio        TEXT NOT NULL DEFAULT 'MOTOBOY' CHECK (meio IN ('MOTOBOY','APLICATIVO')),
  valor_centavos BIGINT NOT NULL CHECK (valor_centavos >= 0),
  prazo       TEXT CHECK (length(prazo) <= 60),
  aluguel     BOOLEAN NOT NULL DEFAULT true,               -- também leva/busca peça alugada
  ativa       BOOLEAN NOT NULL DEFAULT true,
  criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_entrega_cidade ON entrega_cidades (loja_id, lower(cidade), uf) WHERE ativa;

-- --------------------------------------------------------------------------- --
-- Compra feita no site (a sacola paga de uma vez no Mercado Pago)
-- --------------------------------------------------------------------------- --
CREATE TABLE compras_site (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id              UUID NOT NULL REFERENCES lojas(id),
  conta_id             UUID NOT NULL REFERENCES contas_clientes(id),
  cliente_id           UUID NOT NULL REFERENCES clientes(id),
  status               TEXT NOT NULL DEFAULT 'ABERTA' CHECK (status IN ('ABERTA','PAGA','CONFERIR','VENCIDA','CANCELADA')),
  pagar                TEXT NOT NULL CHECK (pagar IN ('ENTRADA','TOTAL')),
  valor_itens_centavos BIGINT NOT NULL CHECK (valor_itens_centavos > 0),
  frete_centavos       BIGINT NOT NULL DEFAULT 0 CHECK (frete_centavos >= 0),
  valor_total_centavos BIGINT NOT NULL,
  entrega              TEXT NOT NULL CHECK (entrega IN ('RETIRADA','LOCAL','ENVIO')),
  entrega_cidade_id    UUID REFERENCES entrega_cidades(id),
  endereco_c           BYTEA,                               -- endereço de entrega, cifrado
  cep                  TEXT CHECK (cep IS NULL OR cep ~ '^\d{8}$'),
  frete_servico        TEXT CHECK (length(frete_servico) <= 80),
  frete_servico_id     INTEGER,
  frete_prazo_dias     SMALLINT,
  id_externo           TEXT,
  url_externa          TEXT CHECK (url_externa IS NULL OR url_externa ~ '^https://'),
  expira_em            TIMESTAMPTZ NOT NULL,
  pagamento_externo_id TEXT UNIQUE,
  forma_paga           TEXT CHECK (forma_paga IS NULL OR forma_paga IN ('PIX','CREDITO','DEBITO')),
  pago_em              TIMESTAMPTZ,
  observacao           TEXT CHECK (length(observacao) <= 300),
  etiqueta_id          TEXT CHECK (length(etiqueta_id) <= 80),
  rastreio             TEXT CHECK (length(rastreio) <= 60),
  criado_em            TIMESTAMPTZ NOT NULL DEFAULT now(),
  chave_idempotencia   UUID NOT NULL UNIQUE,
  CHECK (valor_total_centavos = valor_itens_centavos + frete_centavos),
  CHECK (entrega <> 'LOCAL' OR (entrega_cidade_id IS NOT NULL AND endereco_c IS NOT NULL)),
  CHECK (entrega <> 'ENVIO' OR (cep IS NOT NULL AND endereco_c IS NOT NULL AND frete_servico_id IS NOT NULL))
);
CREATE INDEX idx_compras_conta ON compras_site (conta_id, criado_em DESC);

CREATE TABLE compras_itens (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id          UUID NOT NULL REFERENCES lojas(id),
  compra_id        UUID NOT NULL REFERENCES compras_site(id),
  produto_id       UUID NOT NULL REFERENCES produtos(id),
  tipo             TEXT NOT NULL CHECK (tipo IN ('ALUGUEL','VENDA')),
  data_retirada    DATE,
  data_devolucao   DATE,
  data_festa       DATE,
  valor_peca_centavos  BIGINT NOT NULL CHECK (valor_peca_centavos > 0),
  valor_pagar_centavos BIGINT NOT NULL CHECK (valor_pagar_centavos > 0 AND valor_pagar_centavos <= valor_peca_centavos),
  pedido_id        UUID REFERENCES pedidos(id),
  CHECK (tipo <> 'ALUGUEL' OR (data_retirada IS NOT NULL AND data_devolucao >= data_retirada)),
  CHECK (tipo <> 'VENDA' OR valor_pagar_centavos = valor_peca_centavos)
);
CREATE INDEX idx_compras_itens_produto ON compras_itens (produto_id);

-- Venda pelo site: a mesma peça não entra em duas sacolas pagando ao mesmo tempo
CREATE FUNCTION validar_item_compra() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM 1 FROM produtos WHERE id = NEW.produto_id FOR UPDATE;
  IF NEW.tipo = 'VENDA' AND EXISTS (
       SELECT 1 FROM compras_itens i JOIN compras_site c ON c.id = i.compra_id
        WHERE i.produto_id = NEW.produto_id AND i.tipo = 'VENDA' AND i.compra_id <> NEW.compra_id
          AND c.status = 'ABERTA' AND c.expira_em > now()) THEN
    RAISE EXCEPTION 'Esta peça acabou de ser escolhida por outra cliente. Tente de novo em alguns minutos.';
  END IF;
  IF NEW.tipo = 'VENDA' AND EXISTS (SELECT 1 FROM produtos WHERE id = NEW.produto_id AND status = 'VENDIDO') THEN
    RAISE EXCEPTION 'Esta peça já foi vendida.';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_validar_item_compra BEFORE INSERT ON compras_itens FOR EACH ROW EXECUTE FUNCTION validar_item_compra();

-- Link vencido: a compra vence e as peças voltam; pedido de aluguel da sacola esperando pagamento é cancelado.
CREATE OR REPLACE FUNCTION liberar_pedidos_vencidos() RETURNS INT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  UPDATE links_pagamento SET status = 'CANCELADO', observacao = coalesce(observacao, 'Venceu sem pagamento')
   WHERE loja_id = ctx_loja() AND status = 'ABERTO' AND expira_em < now();
  UPDATE compras_site SET status = 'VENCIDA'
   WHERE loja_id = ctx_loja() AND status = 'ABERTA' AND expira_em < now();
  WITH vencidos AS (
    UPDATE pedidos p SET status = 'CANCELADO'
     WHERE p.loja_id = ctx_loja() AND p.status = 'AGUARDANDO_PAGAMENTO' AND valor_pago(p.id) = 0
       AND NOT EXISTS (SELECT 1 FROM links_pagamento l WHERE l.pedido_id = p.id AND l.status = 'ABERTO')
       AND NOT EXISTS (SELECT 1 FROM compras_itens i JOIN compras_site c ON c.id = i.compra_id
                        WHERE i.pedido_id = p.id AND c.status IN ('ABERTA','CONFERIR'))
    RETURNING 1)
  SELECT count(*) INTO n FROM vencidos;
  RETURN n;
END $$;

GRANT SELECT, INSERT, UPDATE ON contas_clientes, sessoes_clientes, entrega_cidades, compras_site TO app_user;
GRANT SELECT, INSERT ON compras_itens TO app_user;
GRANT UPDATE (pedido_id) ON compras_itens TO app_user;
GRANT UPDATE (sacola_ativa, sacola_pagamento, sacola_minutos, entrega_retirada, entrega_envio, cep_origem,
              pacote_peso_g, pacote_altura_cm, pacote_largura_cm, pacote_comprimento_cm) ON lojas TO app_user;
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['contas_clientes','sessoes_clientes','entrega_cidades','compras_site','compras_itens'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY isolamento_loja ON %I TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja())', t);
  END LOOP;
END $$;
CREATE TRIGGER trg_compras_itens_sem_apagar BEFORE DELETE ON compras_itens FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();
CREATE TRIGGER trg_compras_sem_apagar BEFORE DELETE ON compras_site FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

INSERT INTO permissoes (codigo, descricao) VALUES ('entrega.config', 'Configurar sacola, entrega e frete do site') ON CONFLICT DO NOTHING;
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id FROM papeis pa, permissoes pe
 WHERE pa.codigo IN ('DONO','GERENTE') AND pe.codigo = 'entrega.config'
ON CONFLICT DO NOTHING;
