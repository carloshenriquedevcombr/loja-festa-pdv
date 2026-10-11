-- 0015 — Quem está online agora: funcionárias (sistema) e clientes com conta (site).
-- O navegador avisa "ainda estou aqui" a cada 45 s enquanto a página está aberta; online = visto nos últimos 3 min.
-- Guardamos só a área aberta (ex.: "/pedidos"), nunca o endereço com números ou dados.

ALTER TABLE sessoes ADD COLUMN pagina TEXT CHECK (pagina IS NULL OR pagina ~ '^/[a-z0-9/_-]{0,79}$');
ALTER TABLE sessoes_clientes ADD COLUMN pagina TEXT CHECK (pagina IS NULL OR pagina ~ '^/[a-z0-9/_-]{0,79}$');
GRANT UPDATE (pagina) ON sessoes TO app_user;
CREATE INDEX idx_sessoes_online ON sessoes (loja_id, visto_em DESC) WHERE encerrada_em IS NULL;
CREATE INDEX idx_sessoes_clientes_online ON sessoes_clientes (loja_id, visto_em DESC) WHERE encerrada_em IS NULL;

INSERT INTO permissoes (codigo, descricao) VALUES ('presenca.ver', 'Ver quem está online (equipe e clientes)')
ON CONFLICT DO NOTHING;
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id FROM papeis pa, permissoes pe
 WHERE pa.codigo IN ('DONO','GERENTE','DESENVOLVEDOR') AND pe.codigo = 'presenca.ver'
ON CONFLICT DO NOTHING;
