import asyncio

from sqlalchemy.connectors.asyncio import AsyncAdapt_dbapi_connection
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.engine.default import DefaultDialect
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util.concurrency import await_only


class Control:
    """Controls where the fake driver blocks."""

    def __init__(self):
        self.events = []
        self.block_on = set()
        self.release = asyncio.Event()

    async def gate(self, name):
        self.events.append(name)
        if name in self.block_on:
            await self.release.wait()


class FakeCursor:
    server_side = False

    def __init__(self, connection, control, server_side=False):
        self._connection = connection
        self.control = control
        self.server_side = server_side
        self.description = None
        self.rowcount = -1
        self.arraysize = 1
        self.lastrowid = None
        self._closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def execute(self, operation, parameters=None):
        await self.control.gate("execute")
        if operation.startswith("SELECT"):
            self.description = (("x", None, None, None, None, None, None),)
        else:
            self.description = None
        self.rowcount = 1

    async def executemany(self, operation, parameters):
        await self.control.gate("executemany")
        self.description = None
        self.rowcount = len(parameters)

    async def fetchone(self):
        await self.control.gate("fetchone")
        return (1,)

    async def fetchmany(self, size=None):
        await self.control.gate("fetchmany")
        return [(1,)]

    async def fetchall(self):
        await self.control.gate("fetchall")
        return [(1,)]

    async def close(self):
        if not self._closed:
            self._closed = True
            await self.control.gate("cursor.close")

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeDriverConnection:
    def __init__(self, control):
        self.control = control
        self.closed = False

    def cursor(self):
        return FakeCursor(self, self.control)

    def ss_cursor(self):
        return FakeCursor(self, self.control, server_side=True)

    async def commit(self):
        await self.control.gate("commit")

    async def rollback(self):
        await self.control.gate("rollback")

    async def close(self):
        if not self.closed:
            self.closed = True
            await self.control.gate("close")


class FakeError(Exception):
    pass


class FakeDBAPI:
    apilevel = "2.0"
    threadsafety = 1
    paramstyle = "qmark"

    Error = FakeError
    DatabaseError = FakeError
    OperationalError = FakeError
    InterfaceError = FakeError
    IntegrityError = FakeError
    ProgrammingError = FakeError
    NotSupportedError = FakeError

    @classmethod
    async def connect(cls, *args, **kw):
        control = kw["control"]
        await control.gate("connect")
        return FakeDriverConnection(control)


class FakeAsyncConnection(AsyncAdapt_dbapi_connection):
    def cursor(self, server_side=False):
        if server_side:
            return FakeAsyncSSCursor(self)
        return super().cursor()


class FakeAsyncSSCursor:
    """server-side adapted cursor"""

    def __init__(self, adapt_connection):
        self._adapt_connection = adapt_connection
        self.await_ = adapt_connection.await_
        self._cursor = adapt_connection._connection.ss_cursor()
        self.description = None
        self.rowcount = -1
        self.arraysize = 1
        self.lastrowid = None

    def execute(self, operation, parameters=None):
        self.await_(self._cursor.execute(operation, parameters))
        self.description = self._cursor.description

    def close(self):
        if self._cursor is not None:
            self.await_(self._cursor.close())
            self._cursor = None

    def fetchone(self):
        return self.await_(self._cursor.fetchone())

    def fetchmany(self, size=None):
        return self.await_(self._cursor.fetchmany(size=size))

    def fetchall(self):
        return self.await_(self._cursor.fetchall())

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class FakeDialect(DefaultDialect):
    is_async = True
    driver = "fake"
    name = "fake"
    poolclass = AsyncAdaptedQueuePool
    supports_server_side_cursors = True
    supports_statement_cache = False
    @classmethod
    def import_dbapi(cls):
        return FakeDBAPI

    def connect(self, *cargs, **cparams):
        return FakeAsyncConnection(
            self.loaded_dbapi,
            await_only(self.loaded_dbapi.connect(*cargs, **cparams)),
        )

    def create_connect_args(self, url):
        return (), {}

    def do_begin(self, dbapi_connection):
        pass

    def get_driver_connection(self, connection):
        return connection._connection


class FakeExecutionContext(DefaultExecutionContext):
    def create_server_side_cursor(self):
        return self._dbapi_connection.cursor(server_side=True)


FakeDialect.execution_ctx_cls = FakeExecutionContext
