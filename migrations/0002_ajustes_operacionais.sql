-- Migração 0002 — ajustes operacionais
--   * Números no padrão brasileiro nas mensagens do banco (R$ 1.200,00)
--   * Tabelas criadas por migrações futuras já nascem legíveis pelo app
--   * O app não enxerga o controle de migrações

-- lc_numeric define o separador de milhar e decimal de to_char().
-- Só funciona se o locale pt_BR existir no servidor (a imagem ./db o instala).
DO $$
BEGIN
  EXECUTE format('ALTER DATABASE %I SET lc_numeric = %L', current_database(), 'pt_BR.UTF-8');
  SET LOCAL lc_numeric = 'pt_BR.UTF-8';
EXCEPTION WHEN OTHERS THEN
  RAISE WARNING 'Locale pt_BR.UTF-8 indisponível neste servidor; valores sairão como 1,200.00. Use a imagem ./db.';
END $$;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE ON SEQUENCES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO app_user;

REVOKE ALL ON schema_migrations FROM app_user;
