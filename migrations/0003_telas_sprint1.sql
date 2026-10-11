-- Migração 0003 — apoio às telas da Sprint 1
--   * Clique duplo no "Cadastrar roupa" não cria duas peças
--   * Índices para as listas de pedidos, filas de costura/limpeza e caixa do dia

ALTER TABLE produtos ADD COLUMN chave_idempotencia UUID;
CREATE UNIQUE INDEX uq_produtos_chave ON produtos (loja_id, chave_idempotencia)
  WHERE chave_idempotencia IS NOT NULL;

CREATE INDEX idx_pedidos_ativos ON pedidos (loja_id, status, data_retirada)
  WHERE status NOT IN ('CANCELADO','CONCLUIDO','RETIRADO','VENDIDO');

CREATE INDEX idx_clientes_telefone ON clientes (loja_id, (regexp_replace(telefone, '\D', '', 'g')));
