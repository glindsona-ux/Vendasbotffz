"""
Mini-substituto do aiosqlite SÓ pra rodar os testes num ambiente sem
internet (sandbox). No seu PC/Discloud, com `pip install -r requirements.txt`,
o aiosqlite de verdade é usado e este arquivo é ignorado.
"""
import asyncio
import sqlite3
import types


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    async def fetchone(self):
        return self._cur.fetchone()

    async def fetchall(self):
        return self._cur.fetchall()


class _Result:
    def __init__(self, coro_fn):
        self._fn = coro_fn

    def __await__(self):
        async def run():
            return await self._fn()
        return run().__await__()

    async def __aenter__(self):
        return await self._fn()

    async def __aexit__(self, *a):
        return False


class _Conn:
    def __init__(self, path):
        self._c = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._in_tx = False
        self.row_factory = None

    def _sync_rf(self):
        self._c.row_factory = self.row_factory

    def execute(self, sql, params=()):
        async def run():
            await asyncio.sleep(0)  # cede o controle como o aiosqlite real (thread): expõe corridas
            self._sync_rf()
            head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
            if head in ("INSERT", "UPDATE", "DELETE", "REPLACE") and not self._in_tx:
                self._c.execute("BEGIN")
                self._in_tx = True
            cur = self._c.execute(sql, params)
            return _Cursor(cur)
        return _Result(run)

    async def executemany(self, sql, seq):
        await asyncio.sleep(0)
        self._sync_rf()
        if not self._in_tx:
            self._c.execute("BEGIN")
            self._in_tx = True
        return _Cursor(self._c.executemany(sql, seq))

    async def commit(self):
        if self._in_tx:
            self._c.execute("COMMIT")
            self._in_tx = False

    async def close(self):
        self._c.close()


async def connect(path, timeout=30):
    return _Conn(path)


shim = types.ModuleType("aiosqlite")
shim.connect = connect
shim.Connection = _Conn
shim.Row = sqlite3.Row
shim.__version__ = "shim"
