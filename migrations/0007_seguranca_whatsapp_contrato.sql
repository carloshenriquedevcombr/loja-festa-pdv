-- Migração 0007 — blindagem, WhatsApp, chat do site, contrato assinado e e-mail
--
-- Dados pessoais passam a ficar CIFRADOS (AES-256-GCM) pelo app, com uma chave que fica fora do
-- banco (DADOS_CHAVE no .env). Colunas *_c guardam o texto cifrado; *_idx guardam um "índice cego"
-- (HMAC) que permite achar pelo telefone/CPF sem guardar o número. As colunas antigas em texto são
-- esvaziadas pelo app na primeira vez que ele sobe (python -m app.cli cifrar-dados).

-- --------------------------------------------------------------------------- --
-- Cifra
-- --------------------------------------------------------------------------- --
ALTER TABLE clientes
  ADD COLUMN telefone_c    BYTEA,
  ADD COLUMN cpf_c         BYTEA,
  ADD COLUMN email_c       BYTEA,
  ADD COLUMN observacoes_c BYTEA,
  ADD COLUMN endereco_c    BYTEA,
  ADD COLUMN telefone_idx  TEXT,
  ADD COLUMN telefone_fim  TEXT CHECK (telefone_fim IS NULL OR telefone_fim ~ '^\d{4}$'),
  ADD COLUMN cpf_idx       TEXT,
  ADD COLUMN whatsapp_sair BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX idx_clientes_tel_idx ON clientes (loja_id, telefone_idx);
CREATE INDEX idx_clientes_tel_fim ON clientes (loja_id, telefone_fim);
CREATE INDEX idx_clientes_cpf_idx ON clientes (loja_id, cpf_idx);
DROP INDEX IF EXISTS idx_clientes_telefone;

ALTER TABLE usuarios
  ADD COLUMN telefone_c    BYTEA,
  ADD COLUMN observacoes_c BYTEA,
  ADD COLUMN telefone_idx  TEXT,
  ADD COLUMN avisos        TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN sessao_versao INT NOT NULL DEFAULT 1,
  ADD COLUMN trocar_senha  BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX idx_usuarios_tel_idx ON usuarios (loja_id, telefone_idx);

ALTER TABLE solicitacoes_prova ALTER COLUMN telefone DROP NOT NULL;
ALTER TABLE solicitacoes_prova
  ADD COLUMN telefone_c   BYTEA,
  ADD COLUMN telefone_idx TEXT,
  ADD COLUMN mensagem_c   BYTEA;

ALTER TABLE medidas
  ADD COLUMN valores_c     BYTEA,
  ADD COLUMN observacoes_c BYTEA;

-- Medidas continuam imutáveis; a ÚNICA alteração permitida é trocar o texto aberto pelo cifrado
CREATE FUNCTION medidas_so_cifrar() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND OLD.valores_c IS NULL AND NEW.valores_c IS NOT NULL
     AND NEW.valores = '{}' AND NEW.observacoes IS NULL
     AND NEW.busto_cm IS NULL AND NEW.cintura_cm IS NULL AND NEW.quadril_cm IS NULL
     AND NEW.altura_cm IS NULL AND NEW.comprimento_cm IS NULL
     AND NEW.cliente_id = OLD.cliente_id AND NEW.tomada_em = OLD.tomada_em
     AND NEW.tomada_por = OLD.tomada_por AND NEW.ficha = OLD.ficha THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'Registro imutável: medida nova é uma linha nova.';
END $$;
DROP TRIGGER trg_medidas_imutaveis ON medidas;
CREATE TRIGGER trg_medidas_imutaveis BEFORE UPDATE OR DELETE ON medidas
  FOR EACH ROW EXECUTE FUNCTION medidas_so_cifrar();
GRANT UPDATE (valores, valores_c, observacoes, observacoes_c, busto_cm, cintura_cm, quadril_cm, altura_cm,
              comprimento_cm) ON medidas TO app_user;

-- --------------------------------------------------------------------------- --
-- Loja: redes sociais, horário de funcionamento, contrato, relatório
-- --------------------------------------------------------------------------- --
ALTER TABLE lojas
  ADD COLUMN facebook       TEXT CHECK (facebook IS NULL OR facebook ~ '^https://'),
  ADD COLUMN tiktok         TEXT CHECK (tiktok IS NULL OR tiktok ~ '^https://'),
  ADD COLUMN youtube        TEXT CHECK (youtube IS NULL OR youtube ~ '^https://'),
  ADD COLUMN pinterest      TEXT CHECK (pinterest IS NULL OR pinterest ~ '^https://'),
  ADD COLUMN site_externo   TEXT CHECK (site_externo IS NULL OR site_externo ~ '^https://'),
  ADD COLUMN abre_as        TIME NOT NULL DEFAULT '08:00',
  ADD COLUMN fecha_as       TIME NOT NULL DEFAULT '19:00',
  ADD COLUMN dias_abertos   SMALLINT[] NOT NULL DEFAULT '{1,2,3,4,5,6}',    -- 1 = segunda ... 7 = domingo
  ADD COLUMN contrato_texto TEXT,
  ADD COLUMN relatorio_as   TIME NOT NULL DEFAULT '19:30';

-- --------------------------------------------------------------------------- --
-- Aparelhos conhecidos e bloqueio de IP
-- --------------------------------------------------------------------------- --
CREATE TABLE usuario_aparelhos (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  usuario_id   UUID NOT NULL REFERENCES usuarios(id),
  aparelho     TEXT NOT NULL CHECK (aparelho ~ '^[0-9a-f]{64}$'),   -- hash do cookie do aparelho
  navegador    TEXT,
  ip_ultimo    INET,
  primeiro_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultimo_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (usuario_id, aparelho)
);

CREATE TABLE bloqueios_ip (
  id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id  UUID NOT NULL REFERENCES lojas(id),
  ip       INET NOT NULL,
  ate      TIMESTAMPTZ NOT NULL,
  motivo   TEXT NOT NULL,
  criado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_bloqueios_ip ON bloqueios_ip (loja_id, ip, ate);
CREATE INDEX idx_auditoria_ip_falhas ON auditoria (ip, criado_em) WHERE acao = 'login_falhou';

-- --------------------------------------------------------------------------- --
-- WhatsApp (WPPConnect) e e-mail (Gmail)
-- --------------------------------------------------------------------------- --
CREATE TABLE whatsapp_config (
  loja_id          UUID PRIMARY KEY REFERENCES lojas(id),
  url              TEXT NOT NULL CHECK (url ~ '^https?://'),
  sessao           TEXT NOT NULL CHECK (sessao ~ '^[A-Za-z0-9_-]{2,40}$'),
  segredo_c        BYTEA NOT NULL,
  token_c          BYTEA,
  numero           TEXT NOT NULL CHECK (numero ~ '^\d{12,13}$'),
  webhook_segredo  TEXT NOT NULL CHECK (webhook_segredo ~ '^[0-9a-f]{40}$'),
  endereco_sistema TEXT,
  status           TEXT NOT NULL DEFAULT 'DESCONECTADO',
  codigo           TEXT,
  qrcode           TEXT,
  ativo            BOOLEAN NOT NULL DEFAULT true,
  atualizado_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE email_config (
  loja_id       UUID PRIMARY KEY REFERENCES lojas(id),
  remetente     TEXT NOT NULL CHECK (remetente ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$'),
  senha_c       BYTEA NOT NULL,
  ativo         BOOLEAN NOT NULL DEFAULT true,
  definido_por  UUID NOT NULL REFERENCES usuarios(id),
  definido_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fila de mensagens (WhatsApp e e-mail). Destino e texto cifrados.
CREATE TABLE mensagens (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  canal        TEXT NOT NULL CHECK (canal IN ('WHATSAPP','EMAIL')),
  destino_c    BYTEA NOT NULL,
  texto_c      BYTEA NOT NULL,
  assunto      TEXT,
  anexo_nome   TEXT,
  anexo_c      BYTEA,
  motivo       TEXT NOT NULL,
  chave        TEXT,
  status       TEXT NOT NULL DEFAULT 'PENDENTE' CHECK (status IN ('PENDENTE','ENVIADA','FALHOU','CANCELADA')),
  tentativas   SMALLINT NOT NULL DEFAULT 0,
  enviar_apos  TIMESTAMPTZ NOT NULL DEFAULT now(),
  enviada_em   TIMESTAMPTZ,
  id_externo   TEXT,
  erro         TEXT,
  criado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (loja_id, chave)
);
CREATE INDEX idx_mensagens_fila ON mensagens (loja_id, status, enviar_apos) WHERE status = 'PENDENTE';

-- --------------------------------------------------------------------------- --
-- Chat do site (a conversa acontece no WhatsApp da equipe)
-- --------------------------------------------------------------------------- --
CREATE TABLE chat_conversas (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id       UUID NOT NULL REFERENCES lojas(id),
  codigo        TEXT NOT NULL CHECK (codigo ~ '^[A-Z0-9]{4}$'),
  segredo_hash  TEXT NOT NULL CHECK (segredo_hash ~ '^[0-9a-f]{64}$'),
  nome_c        BYTEA NOT NULL,
  telefone_c    BYTEA,
  origem_hash   TEXT NOT NULL,
  criado_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultima_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
  aberta        BOOLEAN NOT NULL DEFAULT true,
  UNIQUE (loja_id, codigo)
);
CREATE TABLE chat_mensagens (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  conversa_id  UUID NOT NULL REFERENCES chat_conversas(id),
  de           TEXT NOT NULL CHECK (de IN ('CLIENTE','LOJA')),
  texto_c      BYTEA NOT NULL,
  autor        TEXT,
  wpp_id       TEXT,
  criado_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chat_mensagens ON chat_mensagens (conversa_id, id);
CREATE INDEX idx_chat_wpp ON chat_mensagens (loja_id, wpp_id) WHERE wpp_id IS NOT NULL;

-- --------------------------------------------------------------------------- --
-- Contrato assinado (imutável). O código de verificação é impresso em todo comprovante.
-- --------------------------------------------------------------------------- --
ALTER TABLE pedidos ADD COLUMN token_publico TEXT NOT NULL DEFAULT encode(gen_random_bytes(18), 'hex');
CREATE UNIQUE INDEX uq_pedidos_token ON pedidos (token_publico);

CREATE TABLE contratos (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id       UUID NOT NULL REFERENCES lojas(id),
  pedido_id     UUID NOT NULL UNIQUE REFERENCES pedidos(id),
  texto_c       BYTEA NOT NULL,
  assinatura_c  BYTEA NOT NULL,
  assinante     TEXT NOT NULL,
  codigo        TEXT NOT NULL UNIQUE CHECK (codigo ~ '^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$'),
  hash          TEXT NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
  assinado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
  ip            INET,
  navegador     TEXT,
  atendente_id  UUID REFERENCES usuarios(id)
);
CREATE TRIGGER trg_contratos_imutaveis BEFORE UPDATE OR DELETE ON contratos
  FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- Verificação pública de um contrato pelo código (sem dados pessoais)
CREATE FUNCTION verificar_contrato(p_codigo TEXT)
RETURNS TABLE (loja TEXT, sku TEXT, peca TEXT, assinado_em TIMESTAMPTZ, hash TEXT)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT l.nome, pr.sku, pr.nome, c.assinado_em, c.hash
    FROM contratos c JOIN pedidos p ON p.id = c.pedido_id JOIN produtos pr ON pr.id = p.produto_id
    JOIN lojas l ON l.id = c.loja_id
   WHERE c.codigo = upper(p_codigo) AND c.loja_id = ctx_loja()
$$;

-- --------------------------------------------------------------------------- --
-- Permissões e segurança das tabelas novas
-- --------------------------------------------------------------------------- --
INSERT INTO permissoes (codigo, descricao) VALUES
  ('whatsapp.config', 'Conectar o WhatsApp e o e-mail da loja'),
  ('seguranca.ver',   'Ver acessos, aparelhos e alertas de segurança');
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id
  FROM (VALUES ('DONO','whatsapp.config'), ('DESENVOLVEDOR','whatsapp.config'),
               ('DONO','seguranca.ver'), ('DESENVOLVEDOR','seguranca.ver')) AS m(papel, perm)
  JOIN papeis pa ON pa.codigo = m.papel JOIN permissoes pe ON pe.codigo = m.perm;

-- Avisos padrão por cargo (a Dona muda na tela da Equipe)
UPDATE usuarios u SET avisos = x.avisos
  FROM (SELECT up.usuario_id, array_agg(DISTINCT a) AS avisos
          FROM usuario_papeis up JOIN papeis p ON p.id = up.papel_id,
               LATERAL unnest(CASE p.codigo
                 WHEN 'DONO' THEN ARRAY['pedidos','site','pagamentos','relatorio','seguranca']
                 WHEN 'DESENVOLVEDOR' THEN ARRAY['seguranca']
                 WHEN 'GERENTE' THEN ARRAY['pedidos','site','pagamentos','agenda','chat']
                 WHEN 'ATENDENTE' THEN ARRAY['site','agenda','chat']
                 WHEN 'COSTUREIRA' THEN ARRAY['costura']
                 WHEN 'AJUDANTE_COSTURA' THEN ARRAY['costura']
                 WHEN 'LIMPEZA' THEN ARRAY['limpeza']
                 WHEN 'FINANCEIRO' THEN ARRAY['pagamentos','relatorio']
                 ELSE ARRAY[]::text[] END) AS a
         GROUP BY up.usuario_id) x
 WHERE x.usuario_id = u.id;

GRANT INSERT, UPDATE ON usuario_aparelhos, bloqueios_ip, whatsapp_config, email_config, mensagens,
                        chat_conversas, chat_mensagens TO app_user;
GRANT INSERT ON contratos TO app_user;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['usuario_aparelhos','bloqueios_ip','whatsapp_config','email_config','mensagens',
                           'chat_conversas','chat_mensagens','contratos']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY isolamento_loja ON %I TO app_user
         USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja())', t);
  END LOOP;
END $$;
