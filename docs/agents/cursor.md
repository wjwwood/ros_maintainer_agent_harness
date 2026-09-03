# Using Cursor with the Harness

[Cursor](https://www.cursor.com/) is an AI-powered code editor based on VS Code. It supports MCP servers through `.cursor/mcp.json`, allowing Cursor Composer (in Agent mode) to discover and call tools on the Host Policy Gateway.

## Prerequisites

- Cursor IDE installed.
- (Optional) Dev Containers extension installed in Cursor if you want to run the editor inside the session container.

## Quick Start

### 1. Launch Cursor in a Session

Open the session directory in Cursor using the launcher:

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent cursor
```

This launches Cursor pointing at `sessions/pr-rclcpp-160/` with environment variables pre-populated.

### 2. Verify MCP Configuration

When the session was created, the harness automatically created `.cursor/mcp.json` in the session directory:

```json
{
  "mcpServers": {
    "ros-maintainer-harness": {
      "command": "ros-maintainer-harness",
      "args": [
        "serve",
        "--transport",
        "stdio",
        "--workspace",
        "/path/to/ros_maintainer_ws"
      ]
    }
  }
}
```

In Cursor, verify that the tools are active:
1. Open **Cursor Settings** (`Cmd+,` or `Ctrl+,`).
2. Navigate to **Features** > **MCP**.
3. Confirm that `ros-maintainer-harness` is connected and shows available tools (`git_push`, `log_status`, `launch_jenkins_ci`, etc.).

### 3. Using Cursor Composer in Agent Mode

1. Open Cursor Composer (`Cmd+I` or `Ctrl+I`).
2. Ensure Composer is set to **Agent** mode (not Normal mode), so it has permission to run terminal commands and call MCP tools.
3. Provide your prompt:
   ```text
   Review @TASK.md and @MAINTAINER_RULES.md. Inspect the diff in @src/ and run colcon test to verify the current state. Record your progress to @timeline.md using the log_status tool.
   ```

## Running Inside the Devcontainer

If you want the full isolated container experience in Cursor:
1. Press `Cmd+Shift+P` (or `Ctrl+Shift+P`) to open the Command Palette.
2. Select **Dev Containers: Reopen in Container**.
3. Cursor will build and start the devcontainer using `.devcontainer/devcontainer.json`.
4. Inside the container, `/workspace` contains your session files, and `/workspace/tools/bin` is automatically mounted in `$PATH`.
5. The container reaches the host MCP gateway via `http://host.docker.internal:8765`.

## Tips

- **Reference Context Files**: Use Cursor's `@` symbol to directly attach `@TASK.md`, `@timeline.md`, or specific source files to the prompt.
- **Review Tool Calls**: In Agent mode, Cursor displays an approval prompt before invoking external tools like `git_push`. You can inspect the arguments (branch, reason) directly in the UI before confirming.
