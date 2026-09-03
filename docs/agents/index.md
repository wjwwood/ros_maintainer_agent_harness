# AI Agent Integration Guide

The harness is designed to work with any AI coding agent that can either speak the Model Context Protocol (MCP) or run in an interactive shell.

## How Agent Integration Works

When a session is created (via `session create` or `session from-pr`), the harness prepares everything an agent needs to work inside the isolated workspace:

1. **MCP Client Configurations**: Automatically writes configuration files tailored for different editors and tools:
   - `mcp.json` — Generic MCP client configuration
   - `.mcp.json` — Claude Code / Claude Desktop configuration
   - `.cursor/mcp.json` — Cursor IDE MCP configuration
   - `.vscode/mcp.json` — VS Code (Cline, Roo Code, etc.) configuration
2. **Environment Variables**: Injects required session context:
   - `ROS_MAINTAINER_SESSION_ID` — Current session identifier (e.g. `pr-rclcpp-160`)
   - `ROS_MAINTAINER_WS` — Root maintainer workspace directory
   - `ROS_DISTRO` — Target ROS 2 distribution (e.g. `jazzy`, `rolling`)
   - `ROS_MAINTAINER_GATEWAY_URL` — Endpoint for the Host MCP Gateway
3. **Session Prompts & Context**:
   - `TASK.md` — Synthesized goal prompt containing PR metadata, objectives, and maintainer conventions.
   - `timeline.md` — Running log of session progress, status notes, and milestone events.
   - `MAINTAINER_RULES.md` — Read-only copy of maintainer preferences mounted from `config/maintainer_rules.md`.

## The `session launch` Command

The fastest way to start an agent inside a session is using `session launch`:

```bash
ros-maintainer-harness session launch <session_id> --agent <agent_name>
```

Supported agent options:
- `claude` — Launches Claude Code in the session directory with `.mcp.json` active.
- `cursor` — Opens Cursor in the session directory.
- `code` / `vscode` — Opens VS Code in the session directory.
- `shell` — Drops into an interactive bash shell with all environment variables set.

To inspect the generated command and environment without actually starting the process, pass `--dry-run`:

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent claude --dry-run
```

## Agent Guides

For detailed setup instructions, tips, and recommended workflows for specific tools, see:

- [Claude Code](claude_code.md) — Command-line agent workflow using Claude Code.
- [Cursor](cursor.md) — Editor-based workflow using Cursor Composer and MCP tools.
- [VS Code & Extensions](vscode.md) — Using Dev Containers and VS Code extensions (Cline, Roo Code, GitHub Copilot).
- [Interactive Shell](shell.md) — Manual debugging and headless script execution.
