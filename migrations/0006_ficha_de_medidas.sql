-- Migração 0006 — ficha de medidas completa para a costureira
--   * Cada medição guarda a ficha usada (feminina ou masculina) e todos os pontos numerados
--     do desenho (JSON: {"cintura": 70.5, ...}). As colunas antigas continuam para o histórico.
--   * Salto usado na prova (muda o comprimento do vestido longo)
--   * A Dona também pode anotar medidas

ALTER TABLE medidas
  ADD COLUMN ficha   TEXT NOT NULL DEFAULT 'FEMININO' CHECK (ficha IN ('FEMININO','MASCULINO')),
  ADD COLUMN valores JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(valores) = 'object'),
  ADD COLUMN salto_cm NUMERIC(4,1) CHECK (salto_cm IS NULL OR salto_cm BETWEEN 0 AND 25);

-- Medições antigas passam a aparecer na ficha nova (única vez em que a tabela é alterada)
ALTER TABLE medidas DISABLE TRIGGER trg_medidas_imutaveis;
UPDATE medidas SET valores = jsonb_strip_nulls(jsonb_build_object(
    'busto', busto_cm, 'cintura', cintura_cm, 'quadril', quadril_cm,
    'altura', altura_cm, 'comprimento_vestido', comprimento_cm))
 WHERE valores = '{}';
ALTER TABLE medidas ENABLE TRIGGER trg_medidas_imutaveis;

INSERT INTO papel_permissoes (papel_id, permissao_id)
SELECT pa.id, pe.id FROM papeis pa, permissoes pe
 WHERE pa.codigo = 'DONO' AND pe.codigo = 'medidas.editar'
ON CONFLICT DO NOTHING;
