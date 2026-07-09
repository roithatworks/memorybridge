# Running MemoryBridge as a service

`mb serve` runs in the foreground, so you can supervise it with whatever your OS
uses. MemoryBridge itself is cross-platform; only the service wrapper differs.

For most people you don't need any of this: Claude Desktop launches the stdio
server for you (see the snippet from `mb init`). You only need a long-running
service if you want the **HTTP bridge** (`mb serve --http`) up for other MCP
clients.

## macOS (launchd)

The repo ships templates under `launchd/` (`install.sh` renders them for your
Python + data dir). Or write your own `~/Library/LaunchAgents/*.plist` whose
`ProgramArguments` are the absolute path to `mb` and `serve` / `serve --http`.

## Linux (systemd)

`~/.config/systemd/user/memorybridge.service`:

```ini
[Unit]
Description=MemoryBridge HTTP bridge

[Service]
ExecStart=%h/.local/bin/mb serve --http
Environment=MEMORYBRIDGE_DATA=%h/memorybridge
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now memorybridge`

## Docker

```dockerfile
FROM python:3.13-slim
RUN pip install memorybridge
ENV MEMORYBRIDGE_DATA=/data
VOLUME /data
EXPOSE 8484
CMD ["mb", "serve", "--http"]
```

Mount a volume at `/data` so your `memory.db`, config, and `.env` persist. Pass
`MEMORYBRIDGE_TOKEN` for the capability-URL auth on the HTTP bridge.

## Windows

Run `mb serve` from a terminal, or register it with Task Scheduler / NSSM as a
background service. The stdio path works the same; Claude Desktop's config uses
the same `{"command": "mb", "args": ["serve"]}` snippet.
