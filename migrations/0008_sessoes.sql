-- 0008 — Sessões registradas no servidor
-- O cookie da sessão é cifrado (ninguém lê no F12) e, além disso, cada entrada no sistema vira uma linha aqui.
-- "Sair", "desconectar aparelhos", troca de senha ou roubo de cookie (outro navegador) encerram a linha,
-- e o cookie antigo para de valer na hora, mesmo que alguém tenha copiado.
-- Nada é apagado: sessão encerrada fica com a data e o motivo (auditoria).

CREATE TABLE sessoes (
  id            TEXT PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),      -- hash do identificador (o valor fica só no cookie cifrado)
  loja_id       UUID NOT NULL REFERENCES lojas(id),
  usuario_id    UUID NOT NULL REFERENCES usuarios(id),
  aparelho      TEXT CHECK (aparelho IS NULL OR aparelho ~ '^[0-9a-f]{64}$'),
  navegador     TEXT NOT NULL CHECK (navegador ~ '^[0-9a-f]{64}$'),  -- hash do User-Agent
  descricao     TEXT,                                                -- "Chrome no Android"
  ip_inicio     INET,
  ip_ultimo     INET,
  criada_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
  visto_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expira_em     TIMESTAMPTZ NOT NULL,
  encerrada_em  TIMESTAMPTZ,
  motivo        TEXT CHECK (motivo IS NULL OR motivo IN
                  ('SAIU','DESCONECTADO','SENHA_TROCADA','NAVEGADOR_DIFERENTE','APARELHO_DIFERENTE',
                   'EXPIRADA','PARADA','DESATIVADO'))
);
CREATE INDEX idx_sessoes_usuario_abertas ON sessoes (usuario_id) WHERE encerrada_em IS NULL;

-- Sessão encerrada não reabre
CREATE FUNCTION sessoes_nao_reabre() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.encerrada_em IS NOT NULL AND NEW.encerrada_em IS DISTINCT FROM OLD.encerrada_em THEN
    RAISE EXCEPTION 'Sessão encerrada não pode ser reaberta.';
  END IF;
  IF NEW.id <> OLD.id OR NEW.usuario_id <> OLD.usuario_id OR NEW.criada_em <> OLD.criada_em
     OR NEW.navegador <> OLD.navegador OR NEW.expira_em > OLD.expira_em THEN
    RAISE EXCEPTION 'Sessão não pode ser alterada.';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_sessoes_nao_reabre BEFORE UPDATE ON sessoes FOR EACH ROW EXECUTE FUNCTION sessoes_nao_reabre();
CREATE TRIGGER trg_sessoes_sem_apagar BEFORE DELETE ON sessoes FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

GRANT SELECT, INSERT ON sessoes TO app_user;
GRANT UPDATE (visto_em, ip_ultimo, encerrada_em, motivo) ON sessoes TO app_user;
ALTER TABLE sessoes ENABLE ROW LEVEL SECURITY;
CREATE POLICY isolamento_loja ON sessoes TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja());

-- Desconectar aparelhos, trocar/redefinir senha e desativar (sobem sessao_versao) encerram as sessões no banco
CREATE FUNCTION encerrar_sessoes_do_usuario() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE sessoes SET encerrada_em = now(),
         motivo = CASE WHEN NOT NEW.ativo THEN 'DESATIVADO'
                       WHEN NEW.senha_hash IS DISTINCT FROM OLD.senha_hash THEN 'SENHA_TROCADA'
                       ELSE 'DESCONECTADO' END
   WHERE usuario_id = NEW.id AND encerrada_em IS NULL;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_usuarios_encerra_sessoes AFTER UPDATE OF sessao_versao ON usuarios
  FOR EACH ROW WHEN (NEW.sessao_versao > OLD.sessao_versao) EXECUTE FUNCTION encerrar_sessoes_do_usuario();
