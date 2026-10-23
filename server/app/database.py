"""Camada de dados da Biblioteca de Ebooks (PostgreSQL).

Mesma ideia da Libby: helpers pequenos (q / one / ex / init_db) sobre um pool
psycopg, com placeholders ``%s`` nativos. O schema é criado/atualizado sozinho
no startup (CREATE ... IF NOT EXISTS), então o 1º deploy só precisa do banco
vazio.

Conexão: ``DATABASE_URL`` (postgresql://user:senha@host:5432/ebooks).
"""
from __future__ import annotations
import os

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DSN = os.environ.get("DATABASE_URL")
if not DSN:
    raise RuntimeError(
        "DATABASE_URL nao definido. Ex.: "
        "postgresql://usuario:senha@postgres:5432/ebooks"
    )

def _novo_pool() -> ConnectionPool:
    return ConnectionPool(DSN, min_size=1, max_size=10, open=False,
                          kwargs={"autocommit": True, "row_factory": dict_row})


_pool = _novo_pool()

SCHEMA = """
CREATE TABLE IF NOT EXISTS ebook(
  id BIGSERIAL PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  titulo TEXT NOT NULL,
  subtitulo TEXT,
  descricao TEXT,
  categoria TEXT,
  arquivo TEXT NOT NULL,          -- nome do PDF dentro de DATA_DIR/pdfs
  capa TEXT,                      -- nome da imagem dentro de DATA_DIR/capas
  paginas INTEGER,
  tamanho BIGINT,
  visibilidade TEXT DEFAULT 'publicado',  -- 'publicado' | 'evento' | 'oculto' (ver main.VISIBILIDADES)
  ordem INTEGER DEFAULT 0,
  criado_em TIMESTAMP DEFAULT now(),
  atualizado_em TIMESTAMP DEFAULT now());

-- Quem pediu acesso. Uma linha por e-mail: o mesmo contato pode chegar por
-- um evento (acesso_completo = false, só o ebook do evento) e depois passar
-- pelo formulário completo (vira true e a biblioteca inteira abre).
CREATE TABLE IF NOT EXISTS pessoa(
  id BIGSERIAL PRIMARY KEY,
  nome TEXT NOT NULL,
  email TEXT NOT NULL,
  telefone TEXT,
  empresa TEXT,
  cargo TEXT,
  cnpj TEXT,
  como_conheceu TEXT,
  aceita_novidades BOOLEAN DEFAULT FALSE,
  consentimento_em TIMESTAMP,
  acesso_completo BOOLEAN DEFAULT FALSE,
  origem TEXT,                    -- 'formulario' ou 'evento: <nome>' (1º contato)
  criado_em TIMESTAMP DEFAULT now(),
  liberado_em TIMESTAMP,          -- quando ganhou a biblioteca completa
  ultimo_acesso TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pessoa_email ON pessoa(LOWER(email));

CREATE TABLE IF NOT EXISTS evento(
  id BIGSERIAL PRIMARY KEY,
  nome TEXT NOT NULL,
  token TEXT UNIQUE NOT NULL,
  ebook_id BIGINT REFERENCES ebook(id) ON DELETE CASCADE,
  data_evento DATE,
  valido_ate DATE,                -- depois disso o QR para de liberar
  ativo BOOLEAN DEFAULT TRUE,
  criado_em TIMESTAMP DEFAULT now());

-- Ebooks liberados individualmente (hoje: pelo QR de evento).
CREATE TABLE IF NOT EXISTS pessoa_ebook(
  pessoa_id BIGINT REFERENCES pessoa(id) ON DELETE CASCADE,
  ebook_id BIGINT REFERENCES ebook(id) ON DELETE CASCADE,
  evento_id BIGINT REFERENCES evento(id) ON DELETE SET NULL,
  criado_em TIMESTAMP DEFAULT now(),
  PRIMARY KEY (pessoa_id, ebook_id));

-- Leituras e downloads, para o painel saber o que interessa a quem.
CREATE TABLE IF NOT EXISTS acesso_log(
  id BIGSERIAL PRIMARY KEY,
  pessoa_id BIGINT REFERENCES pessoa(id) ON DELETE CASCADE,
  ebook_id BIGINT REFERENCES ebook(id) ON DELETE CASCADE,
  acao TEXT,                      -- 'leitura' | 'download'
  ts TIMESTAMP DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_acesso_log_ebook ON acesso_log(ebook_id, acao);

-- Migração: a 1ª versão tinha só o booleano 'publicado' (sim/não). Virou
-- 'visibilidade' para existir o meio-termo "só eventos".
ALTER TABLE ebook ADD COLUMN IF NOT EXISTS visibilidade TEXT DEFAULT 'publicado';
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'ebook' AND column_name = 'publicado') THEN
    UPDATE ebook SET visibilidade = CASE WHEN publicado THEN 'publicado' ELSE 'oculto' END;
    ALTER TABLE ebook DROP COLUMN publicado;
  END IF;
END $$;

-- Destaque na vitrine e arquivamento de material desatualizado. Arquivar
-- guarda a visibilidade anterior (para restaurar igual) e, se houver, a nova
-- edição que substitui este ebook (QR/links antigos passam a apontar para ela).
ALTER TABLE ebook ADD COLUMN IF NOT EXISTS destaque BOOLEAN DEFAULT FALSE;
ALTER TABLE ebook ADD COLUMN IF NOT EXISTS arquivado_em TIMESTAMP;
ALTER TABLE ebook ADD COLUMN IF NOT EXISTS motivo_arquivo TEXT;
ALTER TABLE ebook ADD COLUMN IF NOT EXISTS vis_antes_arquivo TEXT;
ALTER TABLE ebook ADD COLUMN IF NOT EXISTS substituto_id BIGINT REFERENCES ebook(id) ON DELETE SET NULL;

-- Lead: de qual campanha veio (?origem=instagram&campanha=...), qual ebook
-- pediu ao se cadastrar e se confirmou o e-mail (clicou no link de boas-vindas).
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS campanha_origem TEXT;
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS campanha_nome TEXT;
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS ebook_pedido_id BIGINT REFERENCES ebook(id) ON DELETE SET NULL;
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS email_confirmado_em TIMESTAMP;

-- Quantas vezes o QR foi aberto (topo do funil do evento).
ALTER TABLE evento ADD COLUMN IF NOT EXISTS visitas INTEGER DEFAULT 0;

-- Estudantes: o cadastro tem dois perfis. Estudante não tem empresa/cargo/CNPJ;
-- informa instituição, curso e período. Evento "para estudantes" já marca os
-- participantes com esse perfil (e o cadastro completo abre no formulário certo).
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS perfil TEXT DEFAULT 'profissional';  -- 'profissional' | 'estudante'
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS instituicao TEXT;
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS curso TEXT;
ALTER TABLE pessoa ADD COLUMN IF NOT EXISTS periodo TEXT;
ALTER TABLE evento ADD COLUMN IF NOT EXISTS para_estudantes BOOLEAN DEFAULT FALSE;

-- Controle de idade do material: criado_em é quando entrou na biblioteca;
-- pdf_atualizado_em é a última troca do PDF (edição nova do mesmo assunto).
ALTER TABLE ebook ADD COLUMN IF NOT EXISTS pdf_atualizado_em TIMESTAMP;

-- Equipe do painel: um login por pessoa (antes era uma senha única).
-- sessao_versao: trocar a senha/desativar sobe o número e derruba as sessões
-- abertas em outros aparelhos (o cookie guarda a versão com que foi emitido).
CREATE TABLE IF NOT EXISTS admin_usuario(
  id BIGSERIAL PRIMARY KEY,
  nome TEXT NOT NULL,
  email TEXT NOT NULL,
  senha_hash TEXT NOT NULL,
  papel TEXT DEFAULT 'equipe',          -- 'admin' | 'equipe' (ver equipe.PAPEIS)
  ativo BOOLEAN DEFAULT TRUE,
  trocar_senha BOOLEAN DEFAULT TRUE,     -- senha temporária: troca no 1º acesso
  totp_segredo TEXT,                     -- verificação em duas etapas (app autenticador)
  sessao_versao INTEGER DEFAULT 1,
  criado_em TIMESTAMP DEFAULT now(),
  ultimo_login TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_email ON admin_usuario(LOWER(email));
-- Login por NOME DE USUÁRIO (ex.: "hugo"); o e-mail virou opcional (contato).
-- Quem já existia ganha como usuário a parte do e-mail antes do @.
ALTER TABLE admin_usuario ADD COLUMN IF NOT EXISTS usuario TEXT;
UPDATE admin_usuario SET usuario = LOWER(split_part(email, '@', 1)) || CASE WHEN n > 1 THEN n::text ELSE '' END
  FROM (SELECT id AS uid, ROW_NUMBER() OVER (PARTITION BY LOWER(split_part(email, '@', 1)) ORDER BY id) AS n
          FROM admin_usuario WHERE usuario IS NULL) x
 WHERE id = x.uid;
ALTER TABLE admin_usuario ALTER COLUMN email DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_admin_usuario ON admin_usuario(LOWER(usuario));

-- Quem fez o quê no painel. usuario_nome fica copiado: o registro continua
-- legível mesmo se a pessoa for removida depois.
CREATE TABLE IF NOT EXISTS admin_log(
  id BIGSERIAL PRIMARY KEY,
  usuario_id BIGINT REFERENCES admin_usuario(id) ON DELETE SET NULL,
  usuario_nome TEXT,
  acao TEXT,
  detalhe TEXT,
  ip TEXT,
  ts TIMESTAMP DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_admin_log_ts ON admin_log(ts DESC);

-- Limite de tentativas (login do painel e formulários públicos). No banco,
-- e não em memória, para valer igual nos 2 workers do uvicorn.
CREATE TABLE IF NOT EXISTS tentativa(
  id BIGSERIAL PRIMARY KEY,
  chave TEXT NOT NULL,
  ts TIMESTAMP DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_tentativa ON tentativa(chave, ts);
"""


def init_db() -> None:
    global _pool
    if _pool.closed and getattr(_pool, "_aberto_antes", False):
        _pool = _novo_pool()  # um pool fechado não reabre (testes sobem o app mais de uma vez)
    _pool.open()
    _pool._aberto_antes = True
    with _pool.connection() as c:
        # Os 2 workers do uvicorn sobem juntos: CREATE ... IF NOT EXISTS em
        # paralelo pode colidir no catálogo do Postgres. O lock serializa.
        c.execute("SELECT pg_advisory_lock(7741)")
        try:
            c.execute(SCHEMA)
        finally:
            c.execute("SELECT pg_advisory_unlock(7741)")


def close_db() -> None:
    _pool.close()


def q(sql: str, params: tuple | list = ()) -> list[dict]:
    with _pool.connection() as c:
        return c.execute(sql, params).fetchall()


def one(sql: str, params: tuple | list = ()) -> dict | None:
    with _pool.connection() as c:
        return c.execute(sql, params).fetchone()


def ex(sql: str, params: tuple | list = ()):
    """Executa um comando. Com RETURNING, devolve a 1ª linha (ex.: o id)."""
    with _pool.connection() as c:
        cur = c.execute(sql, params)
        return cur.fetchone() if cur.description else None
