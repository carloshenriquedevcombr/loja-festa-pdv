-- 0013 — Site: banners de promoção, 4 destaques (Como funciona, Entrega, Perguntas, Como provar),
--        avaliações das clientes (pedidas depois da devolução/entrega, aprovadas pela loja) e nota do Google.

-- Banners (promoções) que giram no topo do site. Nada é apagado: ativo = false tira do ar.
CREATE TABLE banners (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id      UUID NOT NULL REFERENCES lojas(id),
  titulo       TEXT NOT NULL CHECK (length(titulo) BETWEEN 2 AND 80),
  texto        TEXT CHECK (length(texto) <= 160),
  botao        TEXT CHECK (length(botao) <= 30),
  link         TEXT CHECK (link IS NULL OR link ~ '^/site(/[A-Za-z0-9/_?=&.%-]*)?$'),     -- só páginas do próprio site
  imagem       TEXT CHECK (imagem IS NULL OR imagem ~ '^[0-9a-f]{32}$'),
  tema         TEXT NOT NULL DEFAULT 'escuro' CHECK (tema IN ('escuro','claro','dourado','rosa')),
  inicio       DATE,
  fim          DATE CHECK (fim IS NULL OR inicio IS NULL OR fim >= inicio),
  ordem        SMALLINT NOT NULL DEFAULT 0,
  ativo        BOOLEAN NOT NULL DEFAULT true,
  criado_em    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Os 4 destaques antes do rodapé (ícone de uma lista fixa; link para página do site)
CREATE TABLE destaques (
  loja_id   UUID NOT NULL REFERENCES lojas(id),
  posicao   SMALLINT NOT NULL CHECK (posicao BETWEEN 1 AND 4),
  icone     TEXT NOT NULL CHECK (icone IN ('cabide','caminhao','pergunta','fita','estrela','calendario','tesoura','presente','coracao','cartao')),
  titulo    TEXT NOT NULL CHECK (length(titulo) BETWEEN 2 AND 40),
  texto     TEXT CHECK (length(texto) <= 120),
  link      TEXT CHECK (link IS NULL OR link ~ '^/site(/[A-Za-z0-9/_?=&.%-]*)?$'),
  PRIMARY KEY (loja_id, posicao)
);

-- Páginas novas para os destaques
ALTER TABLE paginas DROP CONSTRAINT paginas_slug_check;
ALTER TABLE paginas ADD CONSTRAINT paginas_slug_check CHECK (slug IN
  ('quem-somos','trocas-e-devolucoes','termos-de-uso','politica-de-privacidade',
   'como-funciona','entrega-e-devolucao','perguntas-frequentes','como-provar'));

-- Avaliações das clientes: uma por pedido, pelo link secreto; só aparece no site depois que a loja aprova.
-- A nota e o texto da cliente nunca mudam; a loja só aprova/recusa e pode responder.
CREATE TABLE avaliacoes (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  loja_id        UUID NOT NULL REFERENCES lojas(id),
  pedido_id      UUID NOT NULL UNIQUE REFERENCES pedidos(id),
  cliente_id     UUID REFERENCES clientes(id),
  nota           SMALLINT NOT NULL CHECK (nota BETWEEN 1 AND 5),
  texto          TEXT CHECK (length(texto) <= 600),
  nome_exibicao  TEXT NOT NULL CHECK (length(nome_exibicao) BETWEEN 2 AND 40),
  autoriza_site  BOOLEAN NOT NULL DEFAULT true,
  status         TEXT NOT NULL DEFAULT 'PENDENTE' CHECK (status IN ('PENDENTE','APROVADA','RECUSADA')),
  resposta       TEXT CHECK (length(resposta) <= 400),
  moderado_por   UUID REFERENCES usuarios(id),
  moderado_em    TIMESTAMPTZ,
  criado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status <> 'APROVADA' OR autoriza_site)
);
CREATE INDEX idx_avaliacoes_site ON avaliacoes (loja_id, criado_em DESC) WHERE status = 'APROVADA';

CREATE FUNCTION proteger_avaliacao() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.id, NEW.loja_id, NEW.pedido_id, NEW.cliente_id, NEW.nota, NEW.texto, NEW.nome_exibicao, NEW.autoriza_site, NEW.criado_em)
     IS DISTINCT FROM (OLD.id, OLD.loja_id, OLD.pedido_id, OLD.cliente_id, OLD.nota, OLD.texto, OLD.nome_exibicao, OLD.autoriza_site, OLD.criado_em) THEN
    RAISE EXCEPTION 'A avaliação da cliente não pode ser alterada.';
  END IF;
  IF NEW.status <> OLD.status OR NEW.resposta IS DISTINCT FROM OLD.resposta THEN
    IF NOT tem_papel(ctx_usuario(), ARRAY['DONO','GERENTE']) THEN
      RAISE EXCEPTION 'Só a Dona ou a Gerente aprovam avaliações.';
    END IF;
    NEW.moderado_por := ctx_usuario();
    NEW.moderado_em := now();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_proteger_avaliacao BEFORE UPDATE ON avaliacoes FOR EACH ROW EXECUTE FUNCTION proteger_avaliacao();
CREATE TRIGGER trg_avaliacoes_sem_apagar BEFORE DELETE ON avaliacoes FOR EACH ROW EXECUTE FUNCTION bloquear_alteracao();

-- Pedido só é avaliado depois de terminado (devolvido/entregue)
CREATE FUNCTION validar_avaliacao() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p pedidos%ROWTYPE;
BEGIN
  SELECT * INTO p FROM pedidos WHERE id = NEW.pedido_id;
  IF p.status NOT IN ('DEVOLVIDO','EM_LIMPEZA','CONCLUIDO','RETIRADO','VENDIDO') THEN
    RAISE EXCEPTION 'A avaliação abre depois da devolução ou da entrega.';
  END IF;
  NEW.cliente_id := p.cliente_id;
  NEW.loja_id := p.loja_id;
  NEW.status := 'PENDENTE';
  NEW.resposta := NULL;
  NEW.moderado_por := NULL;
  NEW.moderado_em := NULL;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_validar_avaliacao BEFORE INSERT ON avaliacoes FOR EACH ROW EXECUTE FUNCTION validar_avaliacao();

-- Nota do Google Meu Negócio: digitada pela loja ou buscada todo dia (Google Places) se houver chave
ALTER TABLE lojas
  ADD COLUMN google_nota        NUMERIC(2,1) CHECK (google_nota IS NULL OR google_nota BETWEEN 1 AND 5),
  ADD COLUMN google_total       INTEGER CHECK (google_total IS NULL OR google_total >= 0),
  ADD COLUMN google_link        TEXT CHECK (google_link IS NULL OR google_link ~ '^https://(www\.)?(google\.[a-z.]+/maps|maps\.google\.[a-z.]+|maps\.app\.goo\.gl|g\.page|search\.google\.com)/'),
  ADD COLUMN google_place_id    TEXT CHECK (google_place_id IS NULL OR google_place_id ~ '^[A-Za-z0-9_-]{10,200}$'),
  ADD COLUMN google_atualizado  TIMESTAMPTZ,
  ADD COLUMN avaliacoes_no_site BOOLEAN NOT NULL DEFAULT true;

-- Comentários do Google (até 5, trocados a cada busca; o Google exige mostrar o autor e o link)
CREATE TABLE google_avaliacoes (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  loja_id     UUID NOT NULL REFERENCES lojas(id),
  autor       TEXT NOT NULL CHECK (length(autor) <= 80),
  autor_link  TEXT CHECK (autor_link IS NULL OR autor_link ~ '^https://(www\.)?google\.[a-z.]+/'),
  nota        SMALLINT CHECK (nota BETWEEN 1 AND 5),
  texto       TEXT CHECK (length(texto) <= 1000),
  quando      TEXT CHECK (length(quando) <= 40),
  buscado_em  TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON banners TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON destaques TO app_user;
GRANT SELECT, INSERT ON avaliacoes TO app_user;
GRANT UPDATE (status, resposta, moderado_por, moderado_em) ON avaliacoes TO app_user;
GRANT SELECT, INSERT, DELETE ON google_avaliacoes TO app_user;
GRANT UPDATE (google_nota, google_total, google_link, google_place_id, google_atualizado, avaliacoes_no_site)
  ON lojas TO app_user;
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['banners','destaques','avaliacoes','google_avaliacoes'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY isolamento_loja ON %I TO app_user USING (loja_id = ctx_loja()) WITH CHECK (loja_id = ctx_loja())', t);
  END LOOP;
END $$;

INSERT INTO permissoes (codigo, descricao) VALUES ('avaliacoes.moderar', 'Aprovar avaliações das clientes') ON CONFLICT DO NOTHING;
INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id FROM papeis pa, permissoes pe
 WHERE pa.codigo IN ('DONO','GERENTE') AND pe.codigo = 'avaliacoes.moderar'
ON CONFLICT DO NOTHING;
