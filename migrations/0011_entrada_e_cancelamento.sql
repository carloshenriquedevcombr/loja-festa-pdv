-- 0011 — Entrada e multa de cancelamento por PORCENTAGEM ou VALOR FIXO, separadas para aluguel e venda.
--
-- Entrada (sinal) da reserva:  PCT = % do total  ·  FIXO = valor em centavos (nunca mais que o total)
-- Cancelamento pela cliente:   PCT = % do total  ·  FIXO = valor em centavos
--   A loja fica com a multa, limitada ao que já foi pago; o resto é devolvido à cliente.
--   Direito de arrependimento (CDC art. 49): pedido feito fora da loja (link/WhatsApp/site) cancelado em até
--   7 dias devolve tudo, se a opção estiver ligada.
--   "Sem multa" (ex.: a loja errou) só a Dona ou a Gerente, com motivo.
-- As regras são versionadas junto com o resto do config_pagamento: pedido antigo segue a regra do dia em que foi feito.

ALTER TABLE config_pagamento
  ADD COLUMN entrada_aluguel_tipo  TEXT NOT NULL DEFAULT 'PCT' CHECK (entrada_aluguel_tipo IN ('PCT','FIXO')),
  ADD COLUMN entrada_aluguel_valor NUMERIC(12,2) NOT NULL DEFAULT 25 CHECK (entrada_aluguel_valor >= 0),
  ADD COLUMN entrada_venda_tipo    TEXT NOT NULL DEFAULT 'PCT' CHECK (entrada_venda_tipo IN ('PCT','FIXO')),
  ADD COLUMN entrada_venda_valor   NUMERIC(12,2) NOT NULL DEFAULT 25 CHECK (entrada_venda_valor >= 0),
  ADD COLUMN multa_aluguel_tipo    TEXT NOT NULL DEFAULT 'PCT' CHECK (multa_aluguel_tipo IN ('PCT','FIXO')),
  ADD COLUMN multa_aluguel_valor   NUMERIC(12,2) NOT NULL DEFAULT 25 CHECK (multa_aluguel_valor >= 0),
  ADD COLUMN multa_venda_tipo      TEXT NOT NULL DEFAULT 'PCT' CHECK (multa_venda_tipo IN ('PCT','FIXO')),
  ADD COLUMN multa_venda_valor     NUMERIC(12,2) NOT NULL DEFAULT 25 CHECK (multa_venda_valor >= 0),
  ADD COLUMN arrependimento_7d     BOOLEAN NOT NULL DEFAULT true,
  ADD CONSTRAINT pct_ate_100 CHECK (
        (entrada_aluguel_tipo = 'FIXO' OR entrada_aluguel_valor <= 100)
    AND (entrada_venda_tipo   = 'FIXO' OR entrada_venda_valor   <= 100)
    AND (multa_aluguel_tipo   = 'FIXO' OR multa_aluguel_valor   <= 100)
    AND (multa_venda_tipo     = 'FIXO' OR multa_venda_valor     <= 100));

-- As regras que já existiam continuam iguais: entrada = % antiga; multa = a entrada (o contrato já dizia isso)
-- (config_pagamento é imutável para o app; aqui é a própria migração, então libera só durante este ajuste)
ALTER TABLE config_pagamento DISABLE TRIGGER trg_config_pagamento_imutavel;
UPDATE config_pagamento SET entrada_aluguel_valor = entrada_minima_pct, entrada_venda_valor = entrada_minima_pct,
                            multa_aluguel_valor = entrada_minima_pct, multa_venda_valor = entrada_minima_pct;
ALTER TABLE config_pagamento ENABLE TRIGGER trg_config_pagamento_imutavel;

-- Valor pela regra (PCT ou FIXO). FIXO nunca passa do total.
CREATE FUNCTION valor_pela_regra(p_tipo TEXT, p_valor NUMERIC, p_total BIGINT) RETURNS BIGINT
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_tipo = 'FIXO' THEN LEAST(round(p_valor)::bigint, p_total)
              ELSE CEIL(p_total * p_valor / 100)::bigint END;
$$;

ALTER TABLE pedidos
  ADD COLUMN cancel_multa_centavos    BIGINT CHECK (cancel_multa_centavos >= 0),
  ADD COLUMN cancel_devolver_centavos BIGINT CHECK (cancel_devolver_centavos >= 0),
  ADD COLUMN cancel_regra             TEXT,
  ADD COLUMN cancel_sem_multa         BOOLEAN NOT NULL DEFAULT false;

-- Entrada mínima pela regra do tipo (roda depois dos outros gatilhos de INSERT/UPDATE)
CREATE FUNCTION entrada_pela_regra() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cfg config_pagamento%ROWTYPE;
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.valor_total_centavos = OLD.valor_total_centavos THEN
    RETURN NEW;
  END IF;
  SELECT * INTO cfg FROM config_pagamento WHERE id = NEW.config_id;
  NEW.entrada_minima_centavos := CASE WHEN NEW.tipo = 'ALUGUEL'
      THEN valor_pela_regra(cfg.entrada_aluguel_tipo, cfg.entrada_aluguel_valor, NEW.valor_total_centavos)
      ELSE valor_pela_regra(cfg.entrada_venda_tipo, cfg.entrada_venda_valor, NEW.valor_total_centavos) END;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_zy_entrada_pela_regra BEFORE INSERT OR UPDATE ON pedidos
  FOR EACH ROW EXECUTE FUNCTION entrada_pela_regra();

-- Cancelamento: quanto a loja fica e quanto devolve (a tela mostra ANTES de confirmar; o gatilho grava)
CREATE FUNCTION prever_cancelamento(p_pedido UUID, p_sem_multa BOOLEAN DEFAULT false)
RETURNS TABLE (pago BIGINT, multa BIGINT, devolver BIGINT, regra TEXT) LANGUAGE plpgsql STABLE AS $$
DECLARE
  p    pedidos%ROWTYPE;
  cfg  config_pagamento%ROWTYPE;
  tipo TEXT;
  val  NUMERIC;
BEGIN
  SELECT * INTO p FROM pedidos WHERE id = p_pedido;
  SELECT * INTO cfg FROM config_pagamento WHERE id = p.config_id;
  pago := valor_pago(p.id);
  IF p.tipo = 'ALUGUEL' THEN tipo := cfg.multa_aluguel_tipo; val := cfg.multa_aluguel_valor;
  ELSE tipo := cfg.multa_venda_tipo; val := cfg.multa_venda_valor; END IF;
  IF pago <= 0 THEN
    multa := 0; regra := 'Nada pago: nada a devolver';
  ELSIF p_sem_multa THEN
    multa := 0; regra := 'Sem multa (decisão da loja)';
  ELSIF cfg.arrependimento_7d AND p.pagar_pelo_link AND now() - p.criado_em <= interval '7 days' THEN
    multa := 0; regra := 'Arrependimento em até 7 dias (compra fora da loja, CDC art. 49)';
  ELSE
    multa := LEAST(valor_pela_regra(tipo, val, p.valor_total_centavos), pago);
    regra := 'Multa de ' || CASE WHEN tipo = 'PCT' THEN rtrim(rtrim(to_char(val, 'FM990D00'), '0'), ',.') || '% do total'
                                 ELSE 'R$ ' || to_char(val / 100.0, 'FM999G999G990D00') END
             || ' (limitada ao valor pago)';
  END IF;
  devolver := GREATEST(pago, 0) - multa;
  RETURN NEXT;
END $$;

CREATE FUNCTION calcular_cancelamento() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE c RECORD;
BEGIN
  IF NEW.status <> 'CANCELADO' OR OLD.status = 'CANCELADO' THEN
    NEW.cancel_multa_centavos := OLD.cancel_multa_centavos;
    NEW.cancel_devolver_centavos := OLD.cancel_devolver_centavos;
    NEW.cancel_regra := OLD.cancel_regra;
    NEW.cancel_sem_multa := OLD.cancel_sem_multa;
    RETURN NEW;
  END IF;
  IF NEW.cancel_sem_multa AND NOT tem_papel(ctx_usuario(), ARRAY['DONO','GERENTE']) THEN
    RAISE EXCEPTION 'Cancelar sem multa só a Dona ou a Gerente.';
  END IF;
  SELECT * INTO c FROM prever_cancelamento(NEW.id, NEW.cancel_sem_multa);
  NEW.cancel_multa_centavos := c.multa;
  NEW.cancel_devolver_centavos := c.devolver;
  NEW.cancel_regra := c.regra;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_zx_calcular_cancelamento BEFORE UPDATE ON pedidos
  FOR EACH ROW EXECUTE FUNCTION calcular_cancelamento();

-- Devolução do dinheiro à cliente (saída no caixa, ligada ao pedido cancelado)
ALTER TABLE transacoes DROP CONSTRAINT transacoes_natureza_check;
ALTER TABLE transacoes ADD CONSTRAINT transacoes_natureza_check CHECK (natureza IN
  ('ENTRADA_RESERVA','SALDO','PAGAMENTO_INTEGRAL','SERVICO_AJUSTE','DESPESA','ESTORNO','AVULSO','DEVOLUCAO_CLIENTE'));

CREATE FUNCTION validar_devolucao_cliente() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  p pedidos%ROWTYPE;
  ja BIGINT;
BEGIN
  IF NEW.natureza <> 'DEVOLUCAO_CLIENTE' THEN
    RETURN NEW;
  END IF;
  IF NEW.tipo <> 'SAIDA' OR NEW.pedido_id IS NULL THEN
    RAISE EXCEPTION 'Devolução à cliente é uma saída ligada a um pedido.';
  END IF;
  SELECT * INTO p FROM pedidos WHERE id = NEW.pedido_id FOR UPDATE;
  IF p.status <> 'CANCELADO' THEN
    RAISE EXCEPTION 'Só se devolve dinheiro de pedido cancelado.';
  END IF;
  SELECT COALESCE(sum(valor_centavos), 0) INTO ja FROM transacoes
   WHERE pedido_id = p.id AND natureza = 'DEVOLUCAO_CLIENTE';
  IF ja + NEW.valor_centavos > COALESCE(p.cancel_devolver_centavos, 0) THEN
    RAISE EXCEPTION 'Devolução maior do que a regra permite (R$ %).',
      to_char((COALESCE(p.cancel_devolver_centavos, 0) - ja) / 100.0, 'FM999G999G990D00');
  END IF;
  IF NOT tem_papel(NEW.usuario_id, ARRAY['DONO','GERENTE','FINANCEIRO']) THEN
    RAISE EXCEPTION 'Devolução de dinheiro só a Dona, a Gerente ou o Financeiro.';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_zz_validar_devolucao_cliente BEFORE INSERT ON transacoes
  FOR EACH ROW EXECUTE FUNCTION validar_devolucao_cliente();
