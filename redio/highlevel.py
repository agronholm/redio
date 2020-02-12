from __future__ import annotations
from contextlib import contextmanager
from functools import partial

from redio import conv
from redio.commands import CommandBase
from redio.conn import ConnectInfo
from redio.exc import ProtocolError, RedisError
from redio.protocol import Protocol
from redio.pubsub import PubSub

class Redis:
    """Redis connection pool."""
    def __init__(self, url="redis://localhost/", *, ssl_context=None, pool_max=100):
        self.conninfo = ConnectInfo.from_url(
            url,
            ssl_context=ssl_context,
        )
        self.pool_max = pool_max
        self.pool = []

    def __call__(self) -> DB:
        """Get a Redis database connection."""
        return DB(self)

    def pubsub(self, *channels) -> PubSub:
        """Create a publish/subscribe receiver."""
        return PubSub(self, *channels)

    def _borrow_connection(self) -> Protocol:
        return self.pool.pop() if self.pool else Protocol(self.conninfo)

    def _restore_connection(self, connection: Protocol):
        if len(self.pool) < self.pool_max:
            self.pool += connection,


class DB(CommandBase, conv.ByteDecoder):
    """Redis database connection (high level API)."""
    def __init__(self, redis: Redis):
        super().__init__()
        self.redis = redis
        self.protocol = redis._borrow_connection()
        self.commands = []

    def __del__(self):
        """Restore still usable connection to pool on garbage collect. We rely
        partially on CPython's reference counting but also note that it is not
        crucial for connections to be returned immediately."""
        if self.redis and not self.protocol.closed:
            self.redis._restore_connection(self.protocol)

    @property
    def prevent_pooling(self):
        """Prevent this connection being returned to connection pool."""
        self.redis = None
        return self

    def __await__(self):
        """Execute any pending commands and return their results.

        Generally there is one response per each command but some commands may
        not return anything.

        RedisError objects may be returned instead of being raised because the
        database does not abort or rollback anything, and thus all responses
        should be returned.

        Two or more responses are returned as a list."""
        return self._run().__await__()

    async def _run(self):
        """Execute queued commands, equivalent to await self."""
        if self.protocol.closed:
            await self.protocol.connect()
        try:
            if self.commands:
                return await self._execute()
        except:
            # Any error and we assume that the connection is in invalid state.
            self.prevent_pooling
            await self.protocol.aclose()
            self.protocol = None
            raise

    async def _execute(self):
        """Execute queued commands without error handling."""
        commands = []
        handlers = []
        for handler, cmd in self.commands:
            handlers.append(handler)
            commands.append([conv.encode(a) for a in cmd])
        self.commands = []
        res = await self.protocol.run(commands)
        ret = []
        for h, r in zip(handlers, res):
            if h is None:
                ret += r,
                continue
            if isinstance(h, str):
                if r != h:
                    raise RedisError(f"Expected {h}, got {r}")
                continue
            ret += h(r),
        ret = self._decode(ret)
        self.bytedecoder(None)
        return ret if len(ret) != 1 else ret[0]

    def _command(self, *cmd, handler=None):
        self.commands.append((handler, cmd))
        return self
