-- Migração 0004 — clientes, equipe e agenda do processo
--   * Clientes: e-mail e observações
--   * Funcionários: telefone, data de admissão e observações
--   * Loja: endereço, telefone e texto do rodapé dos comprovantes
--   * Agenda: cada pedido com ajuste ganha compromissos (1ª prova, 2ª prova, retirada, devolução)
--     que ficam "feitos" sozinhos quando o pedido avança de etapa

ALTER TABLE clientes
  ADD COLUMN email TEXT CHECK (email IS NULL OR email ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  ADD COLUMN observacoes TEXT,
  ADD COLUMN atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE lojas
  ADD COLUMN endereco TEXT,
  ADD COLUMN telefone TEXT,
  ADD COLUMN rodape_comprovante TEXT;

ALTER TABLE usuarios
  ADD COLUMN telefone TEXT,
  ADD COLUMN data_admissao DATE,
  ADD COLUMN observacoes TEXT;

ALTER TABLE agendamentos DROP CONSTRAINT agendamentos_status_check;
ALTER TABLE agendamentos
  ADD CONSTRAINT agendamentos_status_check
    CHECK (status IN ('AGENDADO','FEITO','FALTOU','REMARCADO','CANCELADO')),
  ADD COLUMN observacao TEXT,
  ADD COLUMN criado_em TIMESTAMPTZ NOT NULL DEFAULT now();

-- Quando o pedido muda de etapa, o compromisso correspondente vira FEITO
CREATE FUNCTION sincronizar_agenda() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE compromisso TEXT;
BEGIN
  IF NEW.status = 'CANCELADO' THEN
    UPDATE agendamentos SET status = 'CANCELADO'
     WHERE pedido_id = NEW.id AND status IN ('AGENDADO','REMARCADO','FALTOU');
    RETURN NULL;
  END IF;
  compromisso := CASE
    WHEN NEW.status = 'PROVA_1'                                      THEN 'PROVA_1'
    WHEN OLD.status = 'PROVA_2' AND NEW.status IN ('PRONTO','EM_AJUSTE') THEN 'PROVA_2'
    WHEN NEW.status IN ('ALUGADO','RETIRADO')                        THEN 'RETIRADA'
    WHEN NEW.status = 'DEVOLVIDO'                                    THEN 'DEVOLUCAO'
  END;
  IF compromisso IS NOT NULL THEN
    UPDATE agendamentos SET status = 'FEITO'
     WHERE pedido_id = NEW.id AND tipo = compromisso AND status IN ('AGENDADO','REMARCADO','FALTOU');
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER trg_sincronizar_agenda
  AFTER UPDATE OF status ON pedidos
  FOR EACH ROW WHEN (OLD.status IS DISTINCT FROM NEW.status)
  EXECUTE FUNCTION sincronizar_agenda();

-- Pedidos que já existiam ganham retirada e devolução na agenda (às 10h)
INSERT INTO agendamentos (loja_id, pedido_id, tipo, inicio, status)
SELECT loja_id, id, 'RETIRADA', (data_retirada + time '10:00') AT TIME ZONE 'America/Sao_Paulo',
       CASE WHEN status IN ('ALUGADO','DEVOLVIDO','EM_LIMPEZA','CONCLUIDO','RETIRADO') THEN 'FEITO' ELSE 'AGENDADO' END
  FROM pedidos
 WHERE data_retirada IS NOT NULL AND status <> 'CANCELADO'
ON CONFLICT (pedido_id, tipo) DO NOTHING;

INSERT INTO agendamentos (loja_id, pedido_id, tipo, inicio, status)
SELECT loja_id, id, 'DEVOLUCAO', (data_devolucao + time '10:00') AT TIME ZONE 'America/Sao_Paulo',
       CASE WHEN status IN ('DEVOLVIDO','EM_LIMPEZA','CONCLUIDO') THEN 'FEITO' ELSE 'AGENDADO' END
  FROM pedidos
 WHERE data_devolucao IS NOT NULL AND status <> 'CANCELADO'
ON CONFLICT (pedido_id, tipo) DO NOTHING;
