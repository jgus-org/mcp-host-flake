# mcp-host

A Nix flake that provides `mcp-host`, an MCP server that puts any number of stdio MCP servers behind a single streamable-HTTP endpoint, for clients that speak only HTTP.

## Outputs

`packages.mcp-host`, also `packages.default`, on `x86_64-linux`, `aarch64-linux`, `x86_64-darwin`, and `aarch64-darwin`: `bin/mcp-host`, carrying its own Python interpreter and MCP libraries, so the host needs nothing installed.

```sh
mcp-host /etc/mcp-host/config.json
MCP_HOST_CONFIG=/etc/mcp-host/config.json mcp-host   # the variable is used when no argument is given
nix run .#mcp-host -- /etc/mcp-host/config.json
```

## Configuration

A JSON object, read once at startup and never written, so it can be mounted read-only; changing the hosted set needs no image change.

| Field            | Default      | Meaning                                               |
| ---------------- | ------------ | ----------------------------------------------------- |
| `name`           | `"mcp-host"` | Server name reported over MCP                         |
| `host`           | `"::"`       | Bind address                                          |
| `port`           | `8080`       | Bind port                                             |
| `path`           | `"/mcp"`     | Path of the streamable-HTTP endpoint                  |
| `servers`        | `[]`         | Servers to host                                       |
| `health_timeout` | `10`         | Seconds a request waits for a hosted server's listing |

Each `servers` entry takes a required `command`, optional `args`, optional `name` (the command itself if unset), and an `env` layered over the host's own environment.

## Notes

- The endpoint is unauthenticated. Put it behind a reverse proxy or network policy if an unauthenticated caller should not get to choose what the hosted servers do.
- Tool names are surfaced exactly as the backing server declares them, with no added namespace. Two servers declaring the same name collide: the listing carries that name once, and a call goes to the first server in configuration order that declares it.
- Each hosted server gets one live session, opened when the host starts and held between requests. Requests borrow that session and never open one of their own, so a hosted server's startup cost is paid once at boot rather than on every listing and call. When the host stops, those sessions close and each hosted server exits with it.
- No listing is answered from a copy, and the client's response cache is off, so a hosted server that has died contributes nothing to the next listing instead of being reported from a stale one, and one that advertises its own listing as cacheable cannot leave the host answering from a stale copy. A session whose server fails is closed and another is opened for that server; a server that cannot be started is reported as failed on request, not retried in a loop, so a request that arrives while a reconnect is in flight can be answered with the previous attempt's failure. A hosted server declares its tools when it starts, so a server that adds a tool at runtime needs its session restarted for the host to list it.
- Giving up on a listing does not stop the hosted server from finishing it. One listing is in flight per hosted server at a time: a request that arrives while one is running waits for that answer rather than starting a second request, because cancelling a request tells the server to interrupt the handler that was answering, which would keep a slow server from ever answering anybody.
- `GET /health` on the same port sends a real listing request to every hosted server through its session and answers `200` with `{"status": "ok", "servers": [{"name": …, "tools": […]}]}` only when all of them reply, and `503` with the failing server's `error` in place of its `tools`. Nothing is cached. The servers are probed at once, so the request's worst case is one `health_timeout` however many servers are hosted, and `health_timeout` bounds how long the host waits -- for the session as well as the round trip -- because a server that is running but not answering must not hold the probe open. A hosted server that never completes its startup handshake keeps its attempt open, so it is reported on every request until it answers or the host stops. A service that probes this endpoint should give its probe a shorter budget than `health_timeout`, or set `health_timeout` below the probe's.
- One listener serves both address families, which is what a service with both A and AAAA records needs; no second bind is required. The published `bin/mcp-host` carries that behaviour in its own interpreter environment, so running `host.py` through another interpreter loses it.

See [LICENSE.md](LICENSE.md) for licensing and copyright, and [NOTICE.md](NOTICE.md) for the terms of upstream and distributed artifacts.
