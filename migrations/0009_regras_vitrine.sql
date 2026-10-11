-- 0009 — Regras da vitrine (ajustadas pela loja no painel) e reserva "até devolver"
--
-- Modo ATE_DEVOLVER (padrão, pedido da loja): a peça sai do estoque/aluguel no dia em que o pedido é
-- feito com pagamento (ou fica presa pelo link) e só volta quando a atendente aperta "Cliente devolveu"
-- (+ dias de limpeza, ou antes se apertar "Voltou da limpeza"). Atrasou: continua presa, sozinha.
-- Modo DATAS: a peça fica presa só da 1ª prova até a devolução prevista + limpeza (como era antes).
-- A troca de modo vale para os pedidos NOVOS; os antigos seguem a regra com que foram feitos.

ALTER TABLE lojas
  ADD COLUMN reserva_modo           TEXT NOT NULL DEFAULT 'ATE_DEVOLVER' CHECK (reserva_modo IN ('ATE_DEVOLVER','DATAS')),
  ADD COLUMN alugada_no_site        TEXT NOT NULL DEFAULT 'OCUPADA' CHECK (alugada_no_site IN ('OCUPADA','ESCONDE')),
  ADD COLUMN venda_ajuste_some      BOOLEAN NOT NULL DEFAULT true,
  ADD COLUMN prova_antecedencia_dias SMALLINT NOT NULL DEFAULT 1 CHECK (prova_antecedencia_dias BETWEEN 0 AND 30),
  ADD COLUMN atraso_aviso_hora      TIME NOT NULL DEFAULT '09:30';

ALTER TABLE pedidos ADD COLUMN reserva_ate_devolver BOOLEAN NOT NULL DEFAULT false;

-- Roda depois dos outros gatilhos de INSERT (ordem alfabética dos nomes)
CREATE FUNCTION reserva_do_pedido() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  modo TEXT;
  limpeza SMALLINT;
  hoje DATE := (now() AT TIME ZONE 'America/Sao_Paulo')::date;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.periodo_bloqueado IS NULL THEN                -- venda simples: já saiu
      RETURN NEW;
    END IF;
    SELECT reserva_modo INTO modo FROM lojas WHERE id = NEW.loja_id;
    IF modo = 'ATE_DEVOLVER' THEN
      NEW.reserva_ate_devolver := true;
      NEW.inicio_bloqueio := LEAST(NEW.inicio_bloqueio, hoje);
      NEW.periodo_bloqueado := daterange(NEW.inicio_bloqueio, NULL, '[)');     -- sem data para acabar
    END IF;
    RETURN NEW;
  END IF;

  -- UPDATE: pedido "até devolver" nunca encurta sozinho; só a devolução confirmada fecha o período
  NEW.reserva_ate_devolver := OLD.reserva_ate_devolver;
  IF NOT OLD.reserva_ate_devolver THEN
    RETURN NEW;
  END IF;
  IF NEW.status IN ('DEVOLVIDO','EM_LIMPEZA') THEN
    SELECT c.dias_limpeza INTO limpeza FROM config_pagamento c WHERE c.id = NEW.config_id;
    NEW.periodo_bloqueado := daterange(lower(OLD.periodo_bloqueado),
                                       GREATEST(hoje, lower(OLD.periodo_bloqueado)) + limpeza, '[]');
  ELSIF NEW.status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO') THEN
    NEW.periodo_bloqueado := OLD.periodo_bloqueado;
    NEW.inicio_bloqueio := OLD.inicio_bloqueio;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_zz_reserva_do_pedido BEFORE INSERT OR UPDATE ON pedidos
  FOR EACH ROW EXECUTE FUNCTION reserva_do_pedido();

-- Situação da peça (tela do catálogo): "Com cliente até devolver" quando for o caso
CREATE OR REPLACE FUNCTION peca_presa_ate_devolver(p_produto UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM pedidos WHERE produto_id = p_produto AND reserva_ate_devolver
                    AND status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO','DEVOLVIDO','EM_LIMPEZA'));
$$;

GRANT UPDATE (reserva_modo, alugada_no_site, venda_ajuste_some, prova_antecedencia_dias, atraso_aviso_hora)
  ON lojas TO app_user;
INSERT INTO permissoes (codigo, descricao) VALUES ('vitrine.regras', 'Ajustar as regras da vitrine e das reservas')
  ON CONFLICT DO NOTHING;
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT p.id, pe.id FROM papeis p, permissoes pe
 WHERE p.codigo IN ('DONO','GERENTE') AND pe.codigo = 'vitrine.regras'
ON CONFLICT DO NOTHING;
