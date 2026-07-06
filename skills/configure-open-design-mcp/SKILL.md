---
name: configure-open-design-mcp
description: Use when registering Open Design as a Codex MCP server, fixing or reapplying the open-design MCP entry in Codex config.toml, or working around codex mcp add failures such as spawn EPERM on Windows.
---

# Configure Open Design MCP

## Overview

Register the local Open Design daemon as a Codex MCP server. Prefer the bundled script because it avoids the known Windows failure where `od mcp install codex` tries to spawn `codex.exe` and receives `EPERM`.

## Quick Start

1. Confirm Open Design has been installed and built in the target root. The default search order includes the current directory, parent directories, `D:\open-design\open-design-run`, and `D:\open-design\open-design`.
2. Run the script:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\admin\.codex\skills\configure-open-design-mcp\scripts\register-open-design-mcp.ps1
```

If Open Design is in a nonstandard location:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\admin\.codex\skills\configure-open-design-mcp\scripts\register-open-design-mcp.ps1 -OpenDesignRoot D:\path\to\open-design
```

For a dry run:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\admin\.codex\skills\configure-open-design-mcp\scripts\register-open-design-mcp.ps1 -DryRun
```

## What The Script Does

- Finds `apps\daemon\dist\cli.js` under the Open Design root.
- Finds `node.exe` on PATH unless `-NodePath` is supplied.
- Backs up `C:\Users\admin\.codex\config.toml` before writing.
- Removes any existing `open-design` MCP block and writes a fresh one.
- Sets `OD_DATA_DIR` and `OD_SIDECAR_IPC_PATH` so the MCP server can discover the live daemon across port changes.
- Verifies the Open Design MCP CLI entry with `node ...\cli.js mcp --help`.

## Options

| Option | Use |
| --- | --- |
| `-OpenDesignRoot <path>` | Use a specific Open Design checkout/run copy. |
| `-CodexConfig <path>` | Write a config other than `$CODEX_HOME\config.toml` or `%USERPROFILE%\.codex\config.toml`. |
| `-ServerName <name>` | Register under a different MCP server name. Default: `open-design`. |
| `-NodePath <path>` | Use a specific Node executable. |
| `-Namespace <name>` | Match a non-default `tools-dev --namespace`. Default: `default`. |
| `-DaemonUrl <url>` | Also set `OD_DAEMON_URL` and health-check `/api/health`. |
| `-NoHealthCheck` | Skip daemon URL health check. |
| `-DryRun` | Print the generated config block without writing. |

## Common Mistakes

- Do not rely on `od mcp install codex` on Windows when `codex.exe` returns `Access is denied` or `spawn EPERM`; use the script.
- Restart Codex after changing `config.toml`; existing sessions may not load newly added MCP servers.
- Keep Open Design daemon running when using the MCP tools. Configuration can be written offline, but tool calls need the daemon.
- If the Open Design port changes, prefer the sidecar pipe path in the generated config instead of hardcoding a port.

## Expected Config Shape

```toml
[mcp_servers."open-design"]
type = "stdio"
command = 'D:\nodejs\node.exe'
args = ['D:\open-design\open-design-run\apps\daemon\dist\cli.js', 'mcp']
startup_timeout_sec = 120

[mcp_servers."open-design".env]
OD_DATA_DIR = 'D:\open-design\open-design-run\.od'
OD_SIDECAR_IPC_PATH = '\\.\pipe\open-design-default-daemon'
```
