# Using VS Code & AI Extensions with the Harness

Visual Studio Code provides first-class support for Dev Containers and works with popular AI extensions such as Cline, Roo Code, Continue, and GitHub Copilot.

## Prerequisites

- Visual Studio Code installed.
- [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) installed (`ms-vscode-remote.remote-containers`).
- Docker or Podman installed on the host.

## Quick Start

### 1. Launch VS Code in the Session

Open the session folder in VS Code:

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent code
```

### 2. Reopen in Container

When VS Code opens the folder, it will detect `.devcontainer/devcontainer.json`.
1. Click **Reopen in Container** in the pop-up notification, or open the Command Palette (`Ctrl+Shift+P` / `Cmd+Shift+P`) and select **Dev Containers: Reopen in Container**.
2. VS Code starts the container based on the target ROS 2 distribution image (e.g. `osrf/ros:jazzy-desktop`).
3. Inside the container:
   - Your session folder is mounted at `/workspace`.
   - The shared maintainer tools are mounted at `/workspace/tools/bin` and added to `$PATH`.
   - Maintainer preferences are mounted read-only at `/workspace/MAINTAINER_RULES.md`.
   - Standard ROS 2 environment setup (`/opt/ros/$ROS_DISTRO/setup.bash`) is sourced automatically in new terminals.

## Connecting AI Extensions (Cline, Roo Code, etc.)

The harness writes `.vscode/mcp.json` into each session directory. How you connect your AI extension depends on where the extension runs:

### Option A: Extension Runs on the Host (Before Reopening in Container)
If your agent extension runs on the host machine, it can use the `stdio` transport directly:

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

### Option B: Extension Runs Inside the Container
If your agent extension runs inside the devcontainer, it communicates with the host gateway over HTTP/SSE:

1. On your host machine, start the gateway daemon:
   ```bash
   ros-maintainer-harness serve --transport sse --port 8765
   ```
2. In the container's `.vscode/mcp.json` or extension settings, configure the SSE endpoint:
   ```json
   {
     "mcpServers": {
       "ros-maintainer-harness": {
         "url": "http://host.docker.internal:8765/sse"
       }
     }
   }
   ```
   *(The devcontainer configuration automatically maps `host.docker.internal` to the host machine via `--add-host=host.docker.internal:host-gateway`)*.

## Pre-Installed VS Code Extensions

The generated `.devcontainer/devcontainer.json` automatically installs standard ROS 2 and C++ development extensions:
- `ms-vscode.cpptools` (C/C++ Intellisense)
- `ms-python.python` (Python language support)
- `ms-iot.vscode-ros` (ROS / ROS 2 tools)
- `twxs.cmake` (CMake syntax highlighting)
- `eamodio.gitlens` (Git visual history)
