"""Serve any number of stdio MCP servers behind one streamable-HTTP endpoint."""

import asyncio
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, Tool
from starlette.requests import Request
from starlette.responses import JSONResponse

DEFAULT_BIND = "::"
DEFAULT_PORT = 8080
DEFAULT_PATH = "/mcp"
DEFAULT_NAME = "mcp-host"
HEALTH_PATH = "/health"
DEFAULT_HEALTH_TIMEOUT = 10.0

logger = logging.getLogger("mcp-host")


class Server(TypedDict, total=False):
    name: str
    command: str
    args: list[str]
    env: dict[str, str]


class HostConfig(TypedDict, total=False):
    name: str
    host: str
    port: int
    path: str
    servers: list[Server]
    health_timeout: float


def load_config(path: Path) -> HostConfig:
    try:
        text = path.read_text()
    except OSError as err:
        raise SystemExit(f"cannot read config {path}: {err.strerror}") from err
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as err:
        raise SystemExit(f"config {path} is not valid JSON: {err}") from err
    if not isinstance(parsed, dict):
        raise SystemExit(f"config {path} must be a JSON object")
    return HostConfig(**parsed)


def child_env(entry: Server) -> dict[str, str]:
    # The SDK spawns with only its "safe to inherit" subset unless env is given,
    # which silently strips everything a backing server was configured to need.
    env = dict(os.environ)
    env.update(entry.get("env", {}))
    return env


def describe(err: BaseException) -> str:
    # The SDK's stdio transport reports a child's failure inside an anyio task
    # group, so the group's own text names nothing the operator can act on.
    while isinstance(err, BaseExceptionGroup) and len(err.exceptions) == 1:
        err = err.exceptions[0]
    detail = str(err)
    if not detail:
        return type(err).__name__
    return f"{type(err).__name__}: {detail}"


class SessionUnavailable(Exception):
    """A hosted server has no live session and no failure to report yet."""


@dataclass(frozen=True, slots=True)
class Child:
    name: str
    params: StdioServerParameters

    def connect(self) -> Client:
        # The client's response cache is off: a listing advertised as cacheable
        # must never leave the host answering from a copy of a dead server's.
        return Client(self.params, cache=None)


class ChildSession:
    """One hosted server's live session, owned by the task running `keep`.

    A stdio handshake costs about as much as running a tool, so a session is held
    between requests rather than spawned by them. The SDK closes a session only
    from the task that opened it, so `keep` opens and closes it while requests
    borrow it; a request that finds it broken hands it back, and `keep` starts
    another one as soon as anything asks for that server again.
    """

    def __init__(self, child: Child) -> None:
        self._child = child
        self._state = asyncio.Condition()
        self._client: Client | None = None
        self._failure: Exception | None = None
        self._listing: asyncio.Task[list[Tool]] | None = None
        self._wanted = True
        self._attempting = False
        self._broken = False

    async def keep(self) -> None:
        while True:
            async with self._state:
                await self._state.wait_for(
                    lambda: self._wanted and self._client is None
                )
                self._wanted, self._attempting = False, True
            opened = False
            try:
                async with self._child.connect() as client:
                    opened = True
                    await self._settle(client=client)
                    async with self._state:
                        await self._state.wait_for(lambda: self._broken)
            except Exception as err:
                # Only a session that never opened says something about the
                # server. One that opened and was closed says nothing: the next
                # request starts another attempt and reports whatever that does.
                if not opened:
                    await self._settle(failure=err)
            finally:
                await self._settle()

    def listing(
        self, ask: Callable[[], Awaitable[list[Tool]]]
    ) -> asyncio.Task[list[Tool]]:
        # One exchange with a server at a time. A request that finds one running
        # joins it rather than starting its own, because abandoning a request
        # tells the server to cancel it and the server interrupts the handler
        # that was answering, so every abandoner would keep a slow server from
        # ever finishing.
        if self._listing is None or self._listing.done():
            self._listing = asyncio.create_task(ask())
            self._listing.add_done_callback(_reaped)
        return self._listing

    async def borrow(self) -> Client:
        async with self._state:
            self._wanted = True
            self._state.notify_all()
            await self._state.wait_for(
                lambda: (
                    self._client is not None
                    or (self._failure is not None and not self._attempting)
                )
            )
            if self._client is not None:
                return self._client
            if self._failure is None:
                raise SessionUnavailable(
                    f"no session with {self._child.name} yet"
                )
            raise self._failure

    async def hand_back(self, client: Client) -> None:
        async with self._state:
            if self._client is client:
                self._broken = True
                self._state.notify_all()

    async def _settle(
        self, client: Client | None = None, failure: Exception | None = None
    ) -> None:
        async with self._state:
            self._client = client
            self._attempting = False
            self._broken = False
            if client is not None:
                self._failure = None
            elif failure is not None:
                self._failure = failure
            self._state.notify_all()


def hosted_children(config: HostConfig) -> list[Child]:
    children = []
    for entry in config.get("servers", []):
        if "command" not in entry:
            raise SystemExit(
                f"server entry {entry.get('name', '?')} has no command"
            )
        children.append(
            Child(
                name=entry.get("name", entry["command"]),
                params=StdioServerParameters(
                    command=entry["command"],
                    args=list(entry.get("args", [])),
                    env=child_env(entry),
                ),
            )
        )
    return children


class MCPHost(MCPServer):
    """The union of its hosted servers' tools, served over one endpoint."""

    def __init__(
        self,
        name: str,
        children: Sequence[Child],
        health_timeout: float,
    ) -> None:
        super().__init__(name, lifespan=live_sessions)
        self._health_timeout = health_timeout
        self._hosted = tuple(
            (child, ChildSession(child)) for child in children
        )
        self.custom_route(HEALTH_PATH, methods=["GET"])(self._health)

    @property
    def sessions(self) -> tuple[ChildSession, ...]:
        return tuple(session for _, session in self._hosted)

    async def list_tools(self) -> list[Tool]:
        listed = await asyncio.gather(
            *(self._listed(session) for _, session in self._hosted),
            return_exceptions=True,
        )
        tools: list[Tool] = []
        for (child, _), outcome in zip(self._hosted, listed, strict=True):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "hosted server %s answered no listing: %s",
                    child.name,
                    describe(outcome),
                )
                continue
            tools.extend(outcome)
        return _first_declaration(tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> CallToolResult:
        answered = await asyncio.gather(
            *(
                self._forward(session, name, arguments)
                for _, session in self._hosted
            ),
            return_exceptions=True,
        )
        for outcome in answered:
            if isinstance(outcome, CallToolResult):
                return outcome
        for (child, _), outcome in zip(self._hosted, answered, strict=True):
            if isinstance(outcome, BaseException):
                raise ToolError(
                    f"hosted server {child.name} could not call {name}: {describe(outcome)}"
                ) from outcome
        raise ToolError(f"unknown tool: {name}")

    async def _listed(self, session: ChildSession) -> list[Tool]:
        # health_timeout is the most any request waits for one server, including
        # the time its session takes to arrive. It bounds the wait, not the
        # exchange: cancelling a request tells the server to abandon the handler
        # answering it, so a server slower than the bound would never finish an
        # answer for anybody, the probe included.
        asking = session.listing(lambda: self._asking(session))
        done, _ = await asyncio.wait({asking}, timeout=self._health_timeout)
        if asking not in done:
            raise TimeoutError(f"no listing within {self._health_timeout}s")
        return asking.result()

    async def _asking(self, session: ChildSession) -> list[Tool]:
        # Every listing is a live request to that server. Nothing is answered from
        # a copy, so a server that cannot answer contributes nothing to the list.
        async with self._using(session) as client:
            return (await client.list_tools()).tools

    async def _forward(
        self,
        session: ChildSession,
        name: str,
        arguments: dict[str, Any],
    ) -> CallToolResult | None:
        tools = await self._listed(session)
        if not any(tool.name == name for tool in tools):
            return None
        # The listing decides which server gets the call; the call itself is not
        # bounded, because a long-running tool is the server's business.
        async with self._using(session) as client:
            return await client.call_tool(name, arguments)

    @contextlib.asynccontextmanager
    async def _using(self, session: ChildSession) -> AsyncIterator[Client]:
        client = await session.borrow()
        try:
            yield client
        except Exception:
            # A session that reports its own failure is the server's problem. A
            # cancelled request is the caller's, and other requests are still
            # using this session, so it is handed back untouched.
            await session.hand_back(client)
            raise

    async def _health(self, request: Request) -> JSONResponse:
        # Probed at once, so the probe's worst case is one health_timeout and
        # not one per hosted server: the container healthcheck gives this
        # request a couple of seconds, and it grows no larger as servers are
        # added. The report stays in configuration order regardless.
        probed = await asyncio.gather(
            *(self._listed(session) for _, session in self._hosted),
            return_exceptions=True,
        )
        report: list[dict[str, Any]] = []
        for (child, _), outcome in zip(self._hosted, probed, strict=True):
            if isinstance(outcome, TimeoutError):
                report.append(
                    {
                        "name": child.name,
                        "error": f"no listing within {self._health_timeout}s",
                    }
                )
            elif isinstance(outcome, BaseException):
                report.append({"name": child.name, "error": describe(outcome)})
            else:
                report.append(
                    {
                        "name": child.name,
                        "tools": [tool.name for tool in outcome],
                    }
                )
        healthy = all("tools" in entry for entry in report)
        return JSONResponse(
            {"status": "ok" if healthy else "unhealthy", "servers": report},
            status_code=200 if healthy else 503,
        )


@contextlib.asynccontextmanager
async def live_sessions(host: MCPHost) -> AsyncIterator[None]:
    # A TaskGroup waits for its tasks rather than cancelling them, and a keeper
    # parked on its condition has no wake left at shutdown, so the keepers are
    # cancelled on the way out. Each closes the session it opened, in the task
    # that opened it, so its server exits with the host.
    async with asyncio.TaskGroup() as group:
        keepers = [
            group.create_task(session.keep()) for session in host.sessions
        ]
        try:
            yield
        finally:
            for keeper in keepers:
                keeper.cancel()


def _reaped(task: asyncio.Task[list[Tool]]) -> None:
    # Every waiter may have given up before this finished, and an answer nobody
    # asks for is reported by the loop as a failure that went unhandled.
    if not task.cancelled():
        task.exception()


def _first_declaration(tools: list[Tool]) -> list[Tool]:
    # Names pass through unnamespaced. A name two servers declare is listed once
    # and served by the first of them in configuration order, which is the only
    # reading of the listing that matches what a client can call.
    declared: dict[str, Tool] = {}
    for tool in tools:
        declared.setdefault(tool.name, tool)
    return list(declared.values())


def build(config: HostConfig) -> MCPHost:
    return MCPHost(
        config.get("name", DEFAULT_NAME),
        hosted_children(config),
        config.get("health_timeout", DEFAULT_HEALTH_TIMEOUT),
    )


def main() -> None:
    config_path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(os.environ["MCP_HOST_CONFIG"])
    )
    config = load_config(config_path)
    build(config).run(
        transport="streamable-http",
        host=config.get("host", DEFAULT_BIND),
        port=config.get("port", DEFAULT_PORT),
        streamable_http_path=config.get("path", DEFAULT_PATH),
    )


if __name__ == "__main__":
    main()
