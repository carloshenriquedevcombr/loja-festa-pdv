-- Migração 0005 — vitrine na internet, fotos, ocasiões e link de pagamento
--   * Ocasiões em dois níveis (Casamento > Madrinha, Festa > Debutante...), editáveis pela loja
--   * Peça: feminino/masculino, adulto/infantil, comprimento, ocasiões, aparece no site, mostra preço
--   * Fotos das peças (o arquivo fica no disco; aqui só o nome e a ordem)
--   * Pedidos de prova feitos pelo site
--   * Link de pagamento (Mercado Pago): token guardado cifrado, links e o que foi pago

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- --------------------------------------------------------------------------- --
-- Ocasiões
-- --------------------------------------------------------------------------- --
CREATE TABLE ocasioes (
  id       SMALLINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id  UUID NOT NULL REFERENCES lojas(id),
  pai_id   SMALLINT REFERENCES ocasioes(id),
  nome     TEXT NOT NULL CHECK (length(nome) BETWEEN 2 AND 60),
  slug     TEXT NOT NULL CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
  ordem    SMALLINT NOT NULL DEFAULT 0,
  ativa    BOOLEAN NOT NULL DEFAULT true,
  UNIQUE NULLS NOT DISTINCT (loja_id, pai_id, slug)
);
CREATE INDEX idx_ocasioes_loja ON ocasioes (loja_id, pai_id, ordem);

-- Só dois níveis, e o "pai" é da mesma loja
CREATE FUNCTION validar_ocasiao() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p ocasioes%ROWTYPE;
BEGIN
  IF NEW.pai_id IS NOT NULL THEN
    SELECT * INTO p FROM ocasioes WHERE id = NEW.pai_id;
    IF p.id IS NULL OR p.loja_id <> NEW.loja_id THEN
      RAISE EXCEPTION 'Ocasião principal não encontrada.';
    END IF;
    IF p.pai_id IS NOT NULL THEN
      RAISE EXCEPTION 'Só existem dois níveis: ocasião e tipo (ex.: Casamento > Madrinha).';
    END IF;
    IF NEW.id = NEW.pai_id THEN
      RAISE EXCEPTION 'Uma ocasião não pode ficar dentro dela mesma.';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_validar_ocasiao BEFORE INSERT OR UPDATE ON ocasioes
  FOR EACH ROW EXECUTE FUNCTION validar_ocasiao();

-- Ocasiões iniciais de uma loja (a Dona pode renomear, desativar e criar outras)
CREATE FUNCTION semear_ocasioes(p_loja UUID) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  grupo  RECORD;
  pai    SMALLINT;
  filho  TEXT;
  n      INT;
BEGIN
  IF EXISTS (SELECT 1 FROM ocasioes WHERE loja_id = p_loja) THEN RETURN; END IF;
  FOR grupo IN SELECT * FROM (VALUES
      (1, 'Casamento', 'casamento', ARRAY['Noiva','Noivo','Madrinha','Padrinho','Mãe dos noivos',
                                          'Convidada','Daminha','Pajem']),
      (2, 'Festa', 'festa', ARRAY['Debutante','Formatura','Baile de gala','Aniversário','Batizado']),
      (3, 'Eventos', 'eventos', ARRAY['Coquetel','Réveillon','Cerimônia religiosa'])
    ) AS g(ordem, nome, slug, filhos)
  LOOP
    INSERT INTO ocasioes (loja_id, nome, slug, ordem) VALUES (p_loja, grupo.nome, grupo.slug, grupo.ordem)
      RETURNING id INTO pai;
    n := 0;
    FOREACH filho IN ARRAY grupo.filhos LOOP
      n := n + 1;
      INSERT INTO ocasioes (loja_id, pai_id, nome, slug, ordem)
      VALUES (p_loja, pai, filho,
              trim(both '-' from regexp_replace(lower(f_unaccent(filho)), '[^a-z0-9]+', '-', 'g')), n);
    END LOOP;
  END LOOP;
END $$;

SELECT semear_ocasioes(id) FROM lojas;

-- --------------------------------------------------------------------------- --
-- Peça: público, faixa etária, comprimento, ocasiões e site
-- --------------------------------------------------------------------------- --
ALTER TABLE produtos
  ADD COLUMN publico      TEXT NOT NULL DEFAULT 'FEMININO' CHECK (publico IN ('FEMININO','MASCULINO','UNISSEX')),
  ADD COLUMN faixa        TEXT NOT NULL DEFAULT 'ADULTO'   CHECK (faixa IN ('ADULTO','INFANTIL')),
  ADD COLUMN comprimento  TEXT CHECK (comprimento IN ('CURTO','MIDI','LONGO')),
  ADD COLUMN ocasioes     SMALLINT[] NOT NULL DEFAULT '{}',
  ADD COLUMN no_site      BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN preco_no_site BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN slug TEXT GENERATED ALWAYS AS (
      trim(both '-' from regexp_replace(lower(f_unaccent(nome || ' ' || sku)), '[^a-z0-9]+', '-', 'g'))
    ) STORED;
CREATE UNIQUE INDEX uq_produtos_slug ON produtos (loja_id, slug);
CREATE INDEX idx_produtos_ocasioes ON produtos USING gin (ocasioes);
CREATE INDEX idx_produtos_site ON produtos (loja_id, publico, faixa) WHERE no_site AND status = 'DISPONIVEL';

-- As ocasiões marcadas precisam existir e ser da mesma loja
CREATE FUNCTION validar_ocasioes_produto() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF cardinality(NEW.ocasioes) > 0 AND
     (SELECT count(*) FROM ocasioes WHERE id = ANY (NEW.ocasioes) AND loja_id = NEW.loja_id)
       <> cardinality(ARRAY(SELECT DISTINCT unnest(NEW.ocasioes))) THEN
    RAISE EXCEPTION 'Ocasião inválida para esta peça.';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_validar_ocasioes_produto BEFORE INSERT OR UPDATE OF ocasioes ON produtos
  FOR EACH ROW EXECUTE FUNCTION validar_ocasioes_produto();

-- A view acompanha as colunas novas da peça
DROP VIEW produtos_situacao;
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
GRANT SELECT ON produtos_situacao TO app_user;

-- --------------------------------------------------------------------------- --
-- Fotos
-- --------------------------------------------------------------------------- --
CREATE TABLE produto_fotos (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  produto_id   UUID NOT NULL REFERENCES produtos(id),
  arquivo      TEXT NOT NULL UNIQUE CHECK (arquivo ~ '^[0-9a-f]{32}$'),
  ordem        SMALLINT NOT NULL DEFAULT 0,
  largura      INT NOT NULL CHECK (largura > 0),
  altura       INT NOT NULL CHECK (altura > 0),
  ativa        BOOLEAN NOT NULL DEFAULT true,   -- "tirar a foto" só esconde; o arquivo fica
  enviada_por  UUID NOT NULL REFERENCES usuarios(id),
  criado_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_fotos_produto ON produto_fotos (produto_id, ordem) WHERE ativa;

-- --------------------------------------------------------------------------- --
-- Dados da loja para o site
-- --------------------------------------------------------------------------- --
ALTER TABLE lojas
  ADD COLUMN whatsapp        TEXT CHECK (whatsapp IS NULL OR whatsapp ~ '^\d{10,11}$'),
  ADD COLUMN instagram       TEXT CHECK (instagram IS NULL OR instagram ~ '^[A-Za-z0-9._]{1,30}$'),
  ADD COLUMN horario         TEXT,
  ADD COLUMN frase_site      TEXT,
  ADD COLUMN site_ativo      BOOLEAN NOT NULL DEFAULT true;

-- --------------------------------------------------------------------------- --
-- Pedidos de prova vindos do site
-- --------------------------------------------------------------------------- --
CREATE TABLE solicitacoes_prova (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id          UUID NOT NULL REFERENCES lojas(id),
  produto_id       UUID REFERENCES produtos(id),
  nome             TEXT NOT NULL CHECK (length(nome) BETWEEN 2 AND 120),
  telefone         TEXT NOT NULL CHECK (telefone ~ '^\d{10,11}$'),
  data_evento      DATE,
  data_desejada    DATE NOT NULL,
  periodo          TEXT NOT NULL CHECK (periodo IN ('MANHA','TARDE')),
  mensagem         TEXT CHECK (length(mensagem) <= 500),
  consentimento_em TIMESTAMPTZ NOT NULL,
  origem_hash      TEXT NOT NULL,               -- hash do IP: só para limitar abuso, não identifica
  status           TEXT NOT NULL DEFAULT 'NOVA' CHECK (status IN ('NOVA','AGENDADA','RECUSADA')),
  agendada_para    TIMESTAMPTZ,
  cliente_id       UUID REFERENCES clientes(id),
  tratada_por      UUID REFERENCES usuarios(id),
  tratada_em       TIMESTAMPTZ,
  criado_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT agendada_tem_data CHECK (status <> 'AGENDADA' OR agendada_para IS NOT NULL)
);
CREATE INDEX idx_solicitacoes_abertas ON solicitacoes_prova (loja_id, status, criado_em);
CREATE INDEX idx_solicitacoes_origem ON solicitacoes_prova (origem_hash, criado_em);

-- --------------------------------------------------------------------------- --
-- Pagamento online (link)
-- --------------------------------------------------------------------------- --
CREATE TABLE pagamento_online (
  loja_id        UUID PRIMARY KEY REFERENCES lojas(id),
  provedor       TEXT NOT NULL CHECK (provedor IN ('MERCADO_PAGO')),
  token_cifrado  BYTEA NOT NULL,           -- pgp_sym_encrypt(token, chave do servidor)
  token_final    TEXT NOT NULL,            -- 4 últimos caracteres, para a tela
  conta          TEXT,                     -- nome da conta confirmado no provedor
  ativo          BOOLEAN NOT NULL DEFAULT true,
  maquininha_id  SMALLINT NOT NULL REFERENCES maquininhas(id),
  definido_por   UUID NOT NULL REFERENCES usuarios(id),
  definido_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE links_pagamento (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id               UUID NOT NULL REFERENCES lojas(id),
  pedido_id             UUID NOT NULL REFERENCES pedidos(id),
  valor_centavos        BIGINT NOT NULL CHECK (valor_centavos > 0),
  max_parcelas          SMALLINT NOT NULL DEFAULT 1 CHECK (max_parcelas BETWEEN 1 AND 12),
  provedor              TEXT NOT NULL CHECK (provedor IN ('MERCADO_PAGO')),
  id_externo            TEXT,
  url_externa           TEXT CHECK (url_externa IS NULL OR url_externa ~ '^https://'),
  status                TEXT NOT NULL DEFAULT 'ABERTO'
                        CHECK (status IN ('ABERTO','PAGO','PAGO_CONFERIR','CANCELADO')),
  pagamento_externo_id  TEXT UNIQUE,
  forma_paga            TEXT,
  transacao_id          BIGINT REFERENCES transacoes(id),
  observacao            TEXT,
  criado_por            UUID NOT NULL REFERENCES usuarios(id),
  criado_em             TIMESTAMPTZ NOT NULL DEFAULT now(),
  pago_em               TIMESTAMPTZ,
  expira_em             TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_links_pedido ON links_pagamento (pedido_id, criado_em DESC);

-- --------------------------------------------------------------------------- --
-- Permissões e segurança
-- --------------------------------------------------------------------------- --
INSERT INTO permissoes (codigo, descricao) VALUES
  ('site.editar',       'Ocasiões, tipos de peça e dados do site'),
  ('site.solicitacoes', 'Atender pedidos de prova do site'),
  ('pagamento.link',    'Gerar link de pagamento');
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id
  FROM (VALUES ('DONO', 'site.editar'), ('GERENTE', 'site.editar'),
               ('DONO', 'site.solicitacoes'), ('GERENTE', 'site.solicitacoes'), ('ATENDENTE', 'site.solicitacoes'),
               ('DESENVOLVEDOR', 'site.solicitacoes'),
               ('DONO', 'pagamento.link'), ('GERENTE', 'pagamento.link'), ('ATENDENTE', 'pagamento.link'),
               ('FINANCEIRO', 'pagamento.link')) AS m(papel, perm)
  JOIN papeis pa ON pa.codigo = m.papel
  JOIN permissoes pe ON pe.codigo = m.perm;

GRANT INSERT, UPDATE ON ocasioes, produto_fotos, solicitacoes_prova, pagamento_online, links_pagamento TO app_user;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['ocasioes','produto_fotos','solicitacoes_prova','pagamento_online','links_pagamento']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY isolamento_loja ON %I TO app_user
         USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja())', t);
  END LOOP;
END $$;

-- O token cifrado nunca vai para a tela: app_user não lê a coluna. Só a função abaixo abre o token,
-- e só com a chave que fica no servidor (fora do banco). Ela roda como dona da tabela
-- (SECURITY DEFINER), por isso filtra explicitamente pela loja da sessão.
REVOKE SELECT ON pagamento_online FROM app_user;
GRANT SELECT (loja_id, provedor, token_final, conta, ativo, maquininha_id, definido_por, definido_em)
  ON pagamento_online TO app_user;

CREATE FUNCTION token_pagamento(p_chave TEXT) RETURNS TEXT
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT pgp_sym_decrypt(token_cifrado, p_chave) FROM pagamento_online WHERE loja_id = ctx_loja()
$$;
REVOKE EXECUTE ON FUNCTION token_pagamento(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION token_pagamento(TEXT) TO app_user;

-- --------------------------------------------------------------------------- --
-- Pedido feito pelo WhatsApp e pago pelo link: fica "Aguardando pagamento"
-- (a peça fica segura para a cliente enquanto o link vale). Quando o pagamento
-- entra, o próprio banco muda para Reservado. Link vencido sem pagamento: cancela.
-- --------------------------------------------------------------------------- --
ALTER TABLE pedidos ADD COLUMN pagar_pelo_link BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE pedidos DROP CONSTRAINT status_valido;
ALTER TABLE pedidos ADD CONSTRAINT status_valido CHECK (status IN
    ('AGUARDANDO_PAGAMENTO','RESERVADO','PROVA_1','EM_AJUSTE','PROVA_2','PRONTO','ALUGADO','DEVOLVIDO',
     'EM_LIMPEZA','CONCLUIDO','RETIRADO','VENDIDO','CANCELADO'));
ALTER TABLE pedidos ADD CONSTRAINT link_so_com_provas CHECK (NOT pagar_pelo_link OR com_ajuste);

INSERT INTO transicoes_permitidas (fluxo, de, para) VALUES
  ('ALUGUEL', 'AGUARDANDO_PAGAMENTO', 'RESERVADO'),
  ('VENDA_AJUSTE', 'AGUARDANDO_PAGAMENTO', 'RESERVADO');

-- Roda depois do trg_preparar_pedido (ordem alfabética): mantém o status de espera
CREATE FUNCTION pedido_aguardando_link() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.pagar_pelo_link THEN
    NEW.status := 'AGUARDANDO_PAGAMENTO';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_ze_pedido_aguardando_link BEFORE INSERT ON pedidos
  FOR EACH ROW EXECUTE FUNCTION pedido_aguardando_link();

CREATE OR REPLACE FUNCTION checar_pagamento_inicial() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  p    pedidos%ROWTYPE;
  pago BIGINT;
BEGIN
  SELECT * INTO p FROM pedidos WHERE id = NEW.id;
  IF p.status = 'AGUARDANDO_PAGAMENTO' THEN
    RETURN NULL;                       -- a entrada vem pelo link
  END IF;
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

CREATE OR REPLACE FUNCTION controlar_pedido() RETURNS trigger LANGUAGE plpgsql AS $$
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
      -- Pedido esperando o link e sem nenhum pagamento: qualquer atendente (ou o sistema) libera
      IF NOT (OLD.status = 'AGUARDANDO_PAGAMENTO' AND valor_pago(OLD.id) = 0)
         AND NOT tem_papel(ctx_usuario(), ARRAY['DONO','GERENTE']) THEN
        RAISE EXCEPTION 'Cancelamento precisa da Dona ou da Gerente.';
      END IF;
      IF OLD.status IN ('ALUGADO','DEVOLVIDO','EM_LIMPEZA') THEN
        RAISE EXCEPTION 'Peça já retirada: não é possível cancelar.';
      END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM transicoes_permitidas
                       WHERE fluxo = OLD.fluxo AND de = OLD.status AND para = NEW.status) THEN
      RAISE EXCEPTION 'Mudança de % para % não é permitida.', OLD.status, NEW.status;
    END IF;

    IF OLD.status = 'AGUARDANDO_PAGAMENTO' AND NEW.status = 'RESERVADO'
       AND valor_pago(NEW.id) < NEW.entrada_minima_centavos THEN
      RAISE EXCEPTION 'Ainda não entrou a entrada mínima de R$ %.',
        to_char(NEW.entrada_minima_centavos / 100.0, 'FM999G999G990D00');
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

-- Pagamento entrou (link, loja ou balcão): pedido em espera vira Reservado sozinho
CREATE FUNCTION confirmar_pedido_pago() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p pedidos%ROWTYPE;
BEGIN
  IF NEW.pedido_id IS NULL OR NEW.tipo <> 'ENTRADA' THEN RETURN NULL; END IF;
  SELECT * INTO p FROM pedidos WHERE id = NEW.pedido_id;
  IF p.status = 'AGUARDANDO_PAGAMENTO' AND valor_pago(p.id) >= p.entrada_minima_centavos THEN
    UPDATE pedidos SET status = 'RESERVADO' WHERE id = p.id;
    UPDATE links_pagamento SET status = 'CANCELADO', observacao = 'Pedido já confirmado'
     WHERE pedido_id = p.id AND status = 'ABERTO' AND pagamento_externo_id IS NULL;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_confirmar_pedido_pago AFTER INSERT ON transacoes
  FOR EACH ROW EXECUTE FUNCTION confirmar_pedido_pago();

-- Libera as peças presas por links vencidos e não pagos. O app chama de tempos em tempos.
CREATE FUNCTION liberar_pedidos_vencidos() RETURNS INT LANGUAGE plpgsql AS $$
DECLARE n INT;
BEGIN
  UPDATE links_pagamento SET status = 'CANCELADO', observacao = coalesce(observacao, 'Venceu sem pagamento')
   WHERE loja_id = ctx_loja() AND status = 'ABERTO' AND expira_em < now();
  WITH vencidos AS (
    UPDATE pedidos p SET status = 'CANCELADO'
     WHERE p.loja_id = ctx_loja() AND p.status = 'AGUARDANDO_PAGAMENTO' AND valor_pago(p.id) = 0
       AND NOT EXISTS (SELECT 1 FROM links_pagamento l WHERE l.pedido_id = p.id AND l.status = 'ABERTO')
    RETURNING 1)
  SELECT count(*) INTO n FROM vencidos;
  RETURN n;
END $$;
