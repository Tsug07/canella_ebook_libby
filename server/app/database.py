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

_pool = ConnectionPool(
    DSN, min_size=1, max_size=10, open=False,
    kwargs={"autocommit": True, "row_factory": dict_row},
)

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
"""


def init_db() -> None:
    _pool.open()
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
