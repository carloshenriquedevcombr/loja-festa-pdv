"""Acesso ao banco.

Toda operação passa por `transacao()`, que define a loja e o usuário da sessão
DENTRO da transação (set_config(..., true)). É isso que faz o Row-Level Security
e os triggers de autorização funcionarem: sem esse contexto, o app não vê nada.
"""
from contextlib import contextmanager
from typing import Iterator, Optional

from psycopg import Cursor
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

pool = ConnectionPool(
    conninfo=config.DATABASE_URL,
    min_size=1,
    max_size=5,
    open=False,
    check=ConnectionPool.check_connection,   # descarta conexões mortas (ex.: banco reiniciou)
    # Nenhuma consulta presa trava o sistema (ataque de lentidão ou erro): o banco corta sozinho
    kwargs={"application_name": "loja-app",
            "options": "-c statement_timeout=15000 -c idle_in_transaction_session_timeout=30000 -c lock_timeout=10000"},
)


@contextmanager
def transacao(loja_id: Optional[str] = None, usuario_id: Optional[str] = None) -> Iterator[Cursor]:
    loja = loja_id or config.LOJA_ID
    if not loja:
        raise RuntimeError("LOJA_ID não configurado.")
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT set_config('app.loja_id', %s, true), set_config('app.usuario_id', %s, true)",
                    (loja, usuario_id or ""),
                )
                yield cur


def banco_ok() -> bool:
    try:
        with pool.connection(timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False
