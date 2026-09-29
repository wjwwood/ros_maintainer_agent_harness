# Using Antigravity & Gemini with the Harness

Antigravity and Gemini CLI provide agentic workflows that integrate natively with the Model Context Protocol (MCP) and automatically discover workspace rule files (`AGENTS.md`).

## Prerequisites

- Antigravity IDE or Gemini CLI installed.
- Docker or Podman installed on the host.
- A read-only fine-grained GitHub Personal Access Token for container sandboxes (or explicit opt-in to unauthenticated container mode; see [GitHub Token Setup](../github_tokens.md)).

## One-Time Workspace Setup

Before starting a conversation in Antigravity or Gemini, initialize your maintainer workspace, configure your container token, and register the MCP server globally:

```bash
# 1. Initialize workspace (writes config/, tools/, AGENTS.md, CLAUDE.md)
ros-maintainer-harness -w ~/ros_maintenance_ws init

# 2. Configure container GitHub token (or pass --no-token for unauthenticated mode)
ros-maintainer-harness -w ~/ros_maintenance_ws token-setup --container-token <READONLY_PAT>

# 3. Register the MCP server in ~/.gemini/config/mcp_config.json
ros-maintainer-harness -w ~/ros_maintenance_ws mcp-install --target gemini

# 4. Verify readiness
ros-maintainer-harness -w ~/ros_maintenance_ws doctor
```

> [!IMPORTANT]
> Antigravity reads global MCP server registrations from `~/.gemini/config/mcp_config.json`. Running `ros-maintainer-harness mcp-install` updates this file automatically so the `ros-maintainer-harness` MCP tools are available in every conversation.

## Starting a Conversation

You can start an Antigravity or Gemini conversation in either of two ways:

### Option A: Open a Specific Session Directory (Single-PR Focus)

1. Scaffold the session from a Pull Request:
   ```bash
   ros-maintainer-harness -w ~/ros_maintenance_ws session from-pr ros2/rclcpp#160
   ```
2. Launch Antigravity or Gemini in the session directory:
   ```bash
   ros-maintainer-harness -w ~/ros_maintenance_ws session launch pr-rclcpp-160 --agent antigravity
   ```
   Because `sessions/pr-rclcpp-160/` contains auto-generated `AGENTS.md` and `TASK.md` files, Antigravity automatically loads the session rules on startup.

### Option B: Open the Maintainer Workspace Root as a "Maintainer Hub" (Recommended)

1. Open `~/ros_maintenance_ws` (or this repository) as your workspace in Antigravity.
2. Antigravity automatically discovers `AGENTS.md` at the workspace root.
3. Use a single long-lived **Maintainer Hub** conversation to coordinate your work across multiple PRs:
   - **Check Status Across All Tasks**:
     ```text
     What is the status of things we're still working on?
     ```
     Calls `get_workspace_status` (or `ros-maintainer-harness status`) to display all active sessions, clickable `[<session_id>](conversation://<id>)` and `[<session_id>](file://<session_dir>)` links, latest milestones, container state, CI runs, and pending approvals.
   - **Triage What to Work on Next**:
     ```text
     What should I work on next?
     ```
     Calls `get_next_actions` (or `ros-maintainer-harness next`) to prioritize pending approval tickets, blocked sessions, failed/completed CI jobs, and open GitHub PRs not yet in an active session.
   - **Start a Dedicated Task Conversation for a PR**:
     ```text
     I want to work on ros2/rclcpp#160
     ```
     Calls `start_session_conversation(pr_ref="ros2/rclcpp#160")`, which scaffolds `sessions/pr-rclcpp-160/`, pre-starts the session container (`ros-harness-pr-rclcpp-160`), launches a dedicated top-level conversation via `agentapi new-conversation` (or spawns a background subagent via `invoke_subagent`), links the `conversation_id` in `session.json`, and returns clickable `conversation://<id>` and `file://<session_dir>` links in the Hub chat.

## How Containerized Execution Works (`PreToolUse` Hooks)

When Antigravity, Gemini CLI, or Claude Code runs on the host OS, its standard shell tool (`run_command` or `Bash`) normally executes on the host. To ensure untrusted PR code is never compiled or executed on the host machine and that the agent never accidentally uses host credentials:
- Running `ros-maintainer-harness init` and `ros-maintainer-harness mcp-install` installs `PreToolUse` hooks in `.agents/hooks.json`, `_agents/hooks.json`, `.claude/settings.json`, and `~/.gemini/config/hooks.json`.
- Whenever an agent runs a command with `Cwd` inside `~/ros_maintenance_ws/sessions/<session_id>` (or inside a conversation linked to `<session_id>` in `session.json`), `ros-maintainer-harness hook pre-tool-use` intercepts the tool call and rewrites `CommandLine` to execute inside the session container (`ros-harness-<session_id>`).
- The session agent can run `colcon build`, `colcon test`, `git`, `gh`, and `pytest` directly without prefixing every command with `ros-maintainer-harness session exec`, and cannot bypass the container or leak host `gh` credentials by omitting the prefix.
- Direct `colcon` or `rosdep` invocations on the host outside any session are automatically denied by the hook.
