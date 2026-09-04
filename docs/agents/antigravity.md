# Using Antigravity & Gemini with the Harness

Antigravity and Gemini CLI provide agentic workflows that integrate natively with the Model Context Protocol (MCP). The harness can automatically generate MCP server configurations (`.gemini/mcp_config.json` or `mcp.json`) for session workspaces, allowing the agent to discover and invoke tools on the Host Policy Gateway.

## Prerequisites

- Antigravity IDE or Gemini CLI installed.
- Access to the Gemini model or agent environment.
- When running in containers or remote environments: Docker or Podman on the host.

## Quick Start

### 1. Launch Antigravity or Gemini in a Session

Open the session folder using the session launcher:

```bash
# Launch Antigravity editor pointing to the session
ros-maintainer-harness session launch pr-rclcpp-160 --agent antigravity

# Or launch Gemini CLI
ros-maintainer-harness session launch pr-rclcpp-160 --agent gemini
```

This launches the agent with:
- The working directory set to `sessions/pr-rclcpp-160/`.
- Session environment variables configured (`ROS_MAINTAINER_SESSION_ID`, `ROS_DISTRO`, `ROS_MAINTAINER_WS`, `ROS_MAINTAINER_GATEWAY_URL`).
- MCP configuration automatically generated in `.gemini/mcp_config.json`.

### 2. MCP Server Configuration

When a session is created (e.g. `session create` or `session from-pr`), the harness automatically generates `.gemini/mcp_config.json` in the session folder:

#### Stdio Mode (Direct Local Execution)
```json
{
  "mcpServers": {
    "ros-maintainer-harness": {
      "command": "ros-maintainer-harness",
      "args": [
        "serve",
        "--transport",
        "stdio",
        "-w",
        "/path/to/ros_maintainer_ws"
      ],
      "env": {
        "ROS_MAINTAINER_WS": "/path/to/ros_maintainer_ws",
        "PYTHONUNBUFFERED": "1"
      }
    }
  }
}
```

In Antigravity, verify that the server is active under **Settings > MCP Servers**. You should see the registered gateway tools (`git_push`, `launch_jenkins_ci`, `get_ci_summary`, `log_status`, etc.).

### 3. Initial Prompt and Workflow

Direct the agent to inspect the synthesized task instructions and maintainer rules:

```text
Please read TASK.md and MAINTAINER_RULES.md. Review the PR changes in src/, run local tests with colcon test to establish a baseline, and record your findings to timeline.md using the log_status tool.
```

## Running Inside Containers or Remote Environments

If your agent runs inside a container sandbox or remote cloudtop environment, configure the harness gateway to listen over HTTP/SSE:

1. **Start the gateway on the host**:
   ```bash
   ros-maintainer-harness serve --transport sse --port 8765
   ```

2. **Configure remote MCP transport**:
   In `.gemini/mcp_config.json` (or `~/.gemini/config/mcp_config.json`):
   ```json
   {
     "mcpServers": {
       "ros-maintainer-harness": {
         "serverUrl": "http://host.docker.internal:8765/sse"
       }
     }
   }
   ```
   *(For remote cloudtops or custom networks, replace `host.docker.internal` with the appropriate hostname or IP).*

## How the Agent Interacts with the Harness

- **Local Builds & Testing**: The agent builds ROS packages (`colcon build`) and executes tests (`colcon test`) directly inside the session workspace.
- **Recording Milestones**: The agent records progress in `timeline.md` using the `log_status` tool or the `ros-session-status` utility.
- **Remote Actions via Gateway**: Protected operations (`git_push`, `launch_jenkins_ci`, `create_pull_request`) pass through the Host Policy Gateway, enforcing branch invariants and generating approval tickets when required.
- **CI Summaries**: Instruct the agent to use `get_ci_summary` rather than fetching raw Jenkins console logs, keeping context usage focused on relevant build and test errors.
