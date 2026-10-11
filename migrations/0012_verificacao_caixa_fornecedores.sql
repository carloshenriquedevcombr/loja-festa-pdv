-- 0012 — Verificação pública do pedido/contrato (QR), formas de pagamento novas, crediário da loja,
--        fornecedores e detalhes das despesas, botões escondidos por cargo.

-- --------------------------------------------------------------------------- --
-- Verificação: aceita o código do contrato (XXXX-XXXX-XXXX) OU o número do pedido (8 letras/números),
-- inclusive de pedidos antigos. Devolve só o que não identifica a cliente: peça, situação e histórico.
-- --------------------------------------------------------------------------- --
CREATE FUNCTION verificar_codigo(p_codigo TEXT) RETURNS JSONB
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE
  cod TEXT := upper(regexp_replace(coalesce(p_codigo, ''), '[^A-Za-z0-9-]', '', 'g'));
  pid UUID;
  n INT;
BEGIN
  IF cod ~ '^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$' THEN
    SELECT c.pedido_id INTO pid FROM contratos c WHERE c.codigo = cod AND c.loja_id = ctx_loja();
  ELSIF cod ~ '^[0-9A-F]{8}$' THEN
    SELECT count(*), min(p.id::text)::uuid INTO n, pid FROM pedidos p
     WHERE p.loja_id = ctx_loja() AND replace(p.id::text, '-', '') LIKE lower(cod) || '%';
    IF n <> 1 THEN pid := NULL; END IF;
  END IF;
  IF pid IS NULL THEN
    RETURN NULL;
  END IF;
  RETURN (
    SELECT jsonb_build_object(
      'pedido_id', p.id, 'numero', upper(left(replace(p.id::text, '-', ''), 8)), 'loja', l.nome,
      'sku', pr.sku, 'peca', pr.nome, 'tipo', p.tipo, 'com_ajuste', p.com_ajuste, 'status', p.status,
      'criado', to_char(p.criado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY'),
      'retirada', to_char(p.data_retirada, 'DD/MM/YYYY'), 'devolucao', to_char(p.data_devolucao, 'DD/MM/YYYY'),
      'quitado', valor_pago(p.id) >= p.valor_total_centavos,
      'contrato', (SELECT jsonb_build_object('codigo', c.codigo, 'hash', c.hash,
                                             'assinado', to_char(c.assinado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'))
                     FROM contratos c WHERE c.pedido_id = p.id),
      'historico', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                              'para', h.para, 'quando', to_char(h.criado_em AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI'))
                              ORDER BY h.id), '[]'::jsonb)
                      FROM historico_status h WHERE h.pedido_id = p.id))
      FROM pedidos p JOIN produtos pr ON pr.id = p.produto_id JOIN lojas l ON l.id = p.loja_id
     WHERE p.id = pid);
END $$;

-- --------------------------------------------------------------------------- --
-- Formas novas: boleto e carnê (de financeira/banco: o dinheiro entra quando é pago) e crediário da loja.
-- Na SAÍDA (Paguei algo) o cartão é o cartão da loja: sem maquininha (e o estorno dela também).
-- --------------------------------------------------------------------------- --
ALTER TABLE transacoes DROP CONSTRAINT transacoes_forma_pagamento_check;
ALTER TABLE transacoes ADD CONSTRAINT transacoes_forma_pagamento_check
  CHECK (forma_pagamento IN ('DINHEIRO','PIX','DEBITO','CREDITO','BOLETO','CARNE'));
ALTER TABLE transacoes DROP CONSTRAINT cartao_exige_maquininha;
ALTER TABLE transacoes ADD CONSTRAINT cartao_exige_maquininha CHECK (
  tipo = 'SAIDA' OR natureza = 'ESTORNO' OR forma_pagamento NOT IN ('DEBITO','CREDITO') OR maquininha_id IS NOT NULL);
ALTER TABLE transacoes DROP CONSTRAINT credito_tem_modalidade;
ALTER TABLE transacoes ADD CONSTRAINT credito_tem_modalidade CHECK (
  tipo = 'SAIDA' OR (natureza = 'ESTORNO' AND maquininha_id IS NULL)
  OR (forma_pagamento = 'CREDITO') = (modalidade_cartao IS NOT NULL));
ALTER TABLE transacoes DROP CONSTRAINT parcelado_so_no_credito;
ALTER TABLE transacoes ADD CONSTRAINT parcelado_so_no_credito CHECK (
  parcelas = 1 OR forma_pagamento IN ('CREDITO','CARNE','BOLETO'));
ALTER TABLE transacoes DROP CONSTRAINT transacoes_natureza_check;
ALTER TABLE transacoes ADD CONSTRAINT transacoes_natureza_check CHECK (natureza IN
  ('ENTRADA_RESERVA','SALDO','PAGAMENTO_INTEGRAL','SERVICO_AJUSTE','DESPESA','ESTORNO','AVULSO','DEVOLUCAO_CLIENTE',
   'PARCELA_CREDIARIO'));

-- --------------------------------------------------------------------------- --
-- Fornecedores e detalhes das despesas (Paguei algo)
-- --------------------------------------------------------------------------- --
CREATE TABLE fornecedores (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id     UUID NOT NULL REFERENCES lojas(id),
  nome        TEXT NOT NULL CHECK (length(nome) BETWEEN 2 AND 120),
  documento   TEXT CHECK (documento IS NULL OR documento ~ '^(\d{11}|\d{14})$'),     -- CPF ou CNPJ, só números
  telefone    TEXT CHECK (telefone IS NULL OR telefone ~ '^\d{10,11}$'),
  email       TEXT CHECK (email IS NULL OR email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  pix         TEXT CHECK (length(pix) <= 120),
  categoria   TEXT CHECK (length(categoria) <= 60),
  observacoes TEXT CHECK (length(observacoes) <= 500),
  ativo       BOOLEAN NOT NULL DEFAULT true,
  criado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_fornecedor_documento ON fornecedores (loja_id, documento) WHERE documento IS NOT NULL;

ALTER TABLE transacoes
  ADD COLUMN fornecedor_id  UUID REFERENCES fornecedores(id),
  ADD COLUMN categoria      TEXT CHECK (length(categoria) <= 60),
  ADD COLUMN documento      TEXT CHECK (length(documento) <= 60),              -- nº da nota, boleto, recibo
  ADD COLUMN anexo          TEXT CHECK (anexo IS NULL OR anexo ~ '^[0-9a-f]{32}$'),   -- foto da nota
  ADD COLUMN crediario_parcela_id BIGINT;

-- --------------------------------------------------------------------------- --
-- Crediário da loja (carnê próprio): a cliente leva e paga em parcelas com vencimento
-- --------------------------------------------------------------------------- --
CREATE TABLE crediarios (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id        UUID NOT NULL REFERENCES lojas(id),
  cliente_id     UUID NOT NULL REFERENCES clientes(id),
  pedido_id      UUID REFERENCES pedidos(id),
  valor_centavos BIGINT NOT NULL CHECK (valor_centavos > 0),
  parcelas       SMALLINT NOT NULL CHECK (parcelas BETWEEN 1 AND 24),
  status         TEXT NOT NULL DEFAULT 'ATIVO' CHECK (status IN ('ATIVO','QUITADO','CANCELADO')),
  criado_por     UUID NOT NULL REFERENCES usuarios(id),
  criado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  cancelado_por  UUID REFERENCES usuarios(id),
  motivo_cancel  TEXT,
  chave_idempotencia TEXT UNIQUE                   -- clique duplo não cria dois crediários
);
CREATE TABLE crediario_parcelas (
  id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id        UUID NOT NULL REFERENCES lojas(id),
  crediario_id   UUID NOT NULL REFERENCES crediarios(id),
  numero         SMALLINT NOT NULL,
  valor_centavos BIGINT NOT NULL CHECK (valor_centavos > 0),
  vencimento     DATE NOT NULL,
  paga_em        TIMESTAMPTZ,
  transacao_id   BIGINT REFERENCES transacoes(id),
  UNIQUE (crediario_id, numero)
);
CREATE INDEX idx_parcelas_abertas ON crediario_parcelas (loja_id, vencimento) WHERE paga_em IS NULL;
ALTER TABLE transacoes ADD CONSTRAINT fk_crediario_parcela FOREIGN KEY (crediario_parcela_id) REFERENCES crediario_parcelas(id);

-- Parcela paga: só uma vez, pelo valor certo, e o crediário vira QUITADO na última
CREATE FUNCTION baixar_parcela() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE par crediario_parcelas%ROWTYPE;
BEGIN
  IF NEW.crediario_parcela_id IS NULL THEN
    IF NEW.natureza = 'PARCELA_CREDIARIO' THEN RAISE EXCEPTION 'Parcela de crediário sem parcela.'; END IF;
    RETURN NEW;
  END IF;
  SELECT * INTO par FROM crediario_parcelas WHERE id = NEW.crediario_parcela_id FOR UPDATE;
  IF par.paga_em IS NOT NULL THEN
    RAISE EXCEPTION 'Esta parcela já foi paga.';
  END IF;
  IF (SELECT status FROM crediarios WHERE id = par.crediario_id) <> 'ATIVO' THEN
    RAISE EXCEPTION 'Crediário cancelado ou quitado.';
  END IF;
  IF NEW.tipo <> 'ENTRADA' OR NEW.natureza <> 'PARCELA_CREDIARIO' OR NEW.valor_centavos <> par.valor_centavos
     OR NEW.pedido_id IS NOT NULL THEN
    RAISE EXCEPTION 'Parcela de crediário: entrada com o valor exato da parcela (R$ %).',
      to_char(par.valor_centavos / 100.0, 'FM999G999G990D00');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_zz_baixar_parcela BEFORE INSERT ON transacoes FOR EACH ROW EXECUTE FUNCTION baixar_parcela();

CREATE FUNCTION parcela_paga() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.crediario_parcela_id IS NOT NULL THEN
    UPDATE crediario_parcelas SET paga_em = now(), transacao_id = NEW.id WHERE id = NEW.crediario_parcela_id;
    UPDATE crediarios c SET status = 'QUITADO'
     WHERE c.id = (SELECT crediario_id FROM crediario_parcelas WHERE id = NEW.crediario_parcela_id)
       AND NOT EXISTS (SELECT 1 FROM crediario_parcelas p WHERE p.crediario_id = c.id AND p.paga_em IS NULL);
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_parcela_paga AFTER INSERT ON transacoes FOR EACH ROW EXECUTE FUNCTION parcela_paga();

-- Crediário conta como pago no pedido (a cliente pode retirar); cancelado conta só o que já pagou
CREATE OR REPLACE FUNCTION valor_pago(p_pedido UUID) RETURNS BIGINT LANGUAGE sql STABLE AS $$
  SELECT (SELECT COALESCE(SUM(CASE WHEN tipo = 'ENTRADA' THEN valor_centavos ELSE -valor_centavos END), 0)
            FROM transacoes WHERE pedido_id = p_pedido)
       + (SELECT COALESCE(SUM(CASE WHEN c.status = 'CANCELADO'
                                   THEN (SELECT COALESCE(SUM(p.valor_centavos), 0) FROM crediario_parcelas p
                                          WHERE p.crediario_id = c.id AND p.paga_em IS NOT NULL)
                                   ELSE c.valor_centavos END), 0)
            FROM crediarios c WHERE c.pedido_id = p_pedido)::bigint;
$$;

-- Crediário do pedido: nunca maior que o que falta; só Dona/Gerente/Financeiro liberam; cliente precisa de CPF
CREATE FUNCTION validar_crediario() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p pedidos%ROWTYPE;
BEGIN
  IF NEW.criado_por IS DISTINCT FROM ctx_usuario() OR NOT tem_papel(NEW.criado_por, ARRAY['DONO','GERENTE','FINANCEIRO']) THEN
    RAISE EXCEPTION 'Crediário só a Dona, a Gerente ou o Financeiro liberam.';
  END IF;
  IF NEW.pedido_id IS NOT NULL THEN
    SELECT * INTO p FROM pedidos WHERE id = NEW.pedido_id FOR UPDATE;
    IF p.status = 'CANCELADO' THEN RAISE EXCEPTION 'Pedido cancelado.'; END IF;
    IF NEW.valor_centavos > p.valor_total_centavos - valor_pago(p.id) THEN
      RAISE EXCEPTION 'Crediário maior que o que falta pagar (R$ %).',
        to_char((p.valor_total_centavos - valor_pago(p.id)) / 100.0, 'FM999G999G990D00');
    END IF;
    IF p.cliente_id IS DISTINCT FROM NEW.cliente_id THEN RAISE EXCEPTION 'Cliente diferente do pedido.'; END IF;
  END IF;
  IF (SELECT cpf_c FROM clientes WHERE id = NEW.cliente_id) IS NULL THEN
    RAISE EXCEPTION 'Para o crediário, cadastre o CPF da cliente.';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_validar_crediario BEFORE INSERT ON crediarios FOR EACH ROW EXECUTE FUNCTION validar_crediario();

-- Parcelas: numeradas de 1 a N, soma nunca passa do valor do crediário, nascem em aberto
CREATE FUNCTION validar_parcela() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c crediarios%ROWTYPE;
BEGIN
  SELECT * INTO c FROM crediarios WHERE id = NEW.crediario_id FOR UPDATE;
  IF c.status <> 'ATIVO' OR NEW.numero NOT BETWEEN 1 AND c.parcelas OR NEW.paga_em IS NOT NULL
     OR NEW.transacao_id IS NOT NULL OR NEW.loja_id <> c.loja_id
     OR (SELECT COALESCE(SUM(valor_centavos), 0) FROM crediario_parcelas WHERE crediario_id = c.id) + NEW.valor_centavos
        > c.valor_centavos THEN
    RAISE EXCEPTION 'Parcela fora do crediário.';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_validar_parcela BEFORE INSERT ON crediario_parcelas FOR EACH ROW EXECUTE FUNCTION validar_parcela();

-- Desfazer (estorno) o recebimento de uma parcela: ela volta a ficar em aberto
CREATE FUNCTION reabrir_parcela() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE par BIGINT;
BEGIN
  IF NEW.estorno_de IS NOT NULL THEN
    SELECT crediario_parcela_id INTO par FROM transacoes WHERE id = NEW.estorno_de;
    IF par IS NOT NULL THEN
      UPDATE crediario_parcelas SET paga_em = NULL, transacao_id = NULL WHERE id = par;
      UPDATE crediarios SET status = 'ATIVO'
       WHERE id = (SELECT crediario_id FROM crediario_parcelas WHERE id = par) AND status = 'QUITADO';
    END IF;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER trg_reabrir_parcela AFTER INSERT ON transacoes FOR EACH ROW EXECUTE FUNCTION reabrir_parcela();

-- Crediário: só o status muda (ATIVO → QUITADO/CANCELADO, QUITADO → ATIVO no estorno); cancelar é da Dona/Gerente/Financeiro
CREATE FUNCTION proteger_crediario() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.id, NEW.loja_id, NEW.cliente_id, NEW.pedido_id, NEW.valor_centavos, NEW.parcelas, NEW.criado_por, NEW.criado_em, NEW.chave_idempotencia)
     IS DISTINCT FROM (OLD.id, OLD.loja_id, OLD.cliente_id, OLD.pedido_id, OLD.valor_centavos, OLD.parcelas, OLD.criado_por, OLD.criado_em, OLD.chave_idempotencia) THEN
    RAISE EXCEPTION 'Crediário não pode ser alterado; cancele e faça outro.';
  END IF;
  IF OLD.status = 'CANCELADO' THEN
    RAISE EXCEPTION 'Crediário já cancelado.';
  END IF;
  IF NEW.status = 'CANCELADO' AND OLD.status <> 'CANCELADO' THEN
    IF NEW.cancelado_por IS NULL OR NEW.cancelado_por <> ctx_usuario()
       OR NOT tem_papel(NEW.cancelado_por, ARRAY['DONO','GERENTE','FINANCEIRO']) THEN
      RAISE EXCEPTION 'Cancelar crediário: só a Dona, a Gerente ou o Financeiro.';
    END IF;
    IF length(coalesce(NEW.motivo_cancel, '')) < 3 THEN RAISE EXCEPTION 'Escreva o motivo do cancelamento.'; END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_proteger_crediario BEFORE UPDATE ON crediarios FOR EACH ROW EXECUTE FUNCTION proteger_crediario();
CREATE TRIGGER trg_crediarios_sem_apagar BEFORE DELETE ON crediarios FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();
CREATE TRIGGER trg_parcelas_sem_apagar BEFORE DELETE ON crediario_parcelas FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- Chave Pix da loja (vai no carnê para a cliente pagar a parcela)
ALTER TABLE lojas ADD COLUMN pix_chave TEXT CHECK (length(pix_chave) <= 77),
                  ADD COLUMN pix_nome TEXT CHECK (length(pix_nome) <= 25),
                  ADD COLUMN pix_cidade TEXT CHECK (length(pix_cidade) <= 15);

-- Botões escondidos por cargo (a Dona/Dev escolhem na tela)
CREATE TABLE botoes_ocultos (
  loja_id  UUID NOT NULL REFERENCES lojas(id),
  papel    TEXT NOT NULL,
  endereco TEXT NOT NULL CHECK (endereco ~ '^/[a-z0-9/_-]*$'),
  PRIMARY KEY (loja_id, papel, endereco)
);

CREATE FUNCTION so_quem_escolhe_botoes() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT tem_papel(ctx_usuario(), ARRAY['DONO','DESENVOLVEDOR']) THEN
    RAISE EXCEPTION 'Só a Dona ou o Desenvolvedor escolhem os botões.';
  END IF;
  RETURN COALESCE(NEW, OLD);
END $$;
CREATE TRIGGER trg_botoes_quem_escolhe BEFORE INSERT OR DELETE ON botoes_ocultos
  FOR EACH ROW EXECUTE FUNCTION so_quem_escolhe_botoes();

GRANT SELECT, INSERT, UPDATE ON fornecedores, crediarios TO app_user;
GRANT SELECT, INSERT ON crediario_parcelas TO app_user;
GRANT UPDATE (paga_em, transacao_id) ON crediario_parcelas TO app_user;
GRANT SELECT, INSERT, DELETE ON botoes_ocultos TO app_user;
GRANT UPDATE (pix_chave, pix_nome, pix_cidade) ON lojas TO app_user;
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['fornecedores','crediarios','crediario_parcelas','botoes_ocultos'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY isolamento_loja ON %I TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja())', t);
  END LOOP;
END $$;

INSERT INTO permissoes (codigo, descricao) VALUES
  ('fornecedores.editar', 'Cadastrar fornecedores'),
  ('crediario.liberar',   'Liberar crediário (carnê da loja)'),
  ('botoes.escolher',     'Escolher os botões que cada cargo vê')
ON CONFLICT DO NOTHING;
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id
  FROM (VALUES ('DONO','fornecedores.editar'), ('GERENTE','fornecedores.editar'), ('FINANCEIRO','fornecedores.editar'),
               ('DONO','crediario.liberar'), ('GERENTE','crediario.liberar'), ('FINANCEIRO','crediario.liberar'),
               ('DONO','botoes.escolher'), ('DESENVOLVEDOR','botoes.escolher')) AS m(papel, perm)
  JOIN papeis pa ON pa.codigo = m.papel JOIN permissoes pe ON pe.codigo = m.perm
ON CONFLICT DO NOTHING;
