# Interactive Shell & Custom Agent Scripts

If you want to manually test a session, inspect the environment, or run custom headless automation scripts, you can drop into an interactive shell.

## Host-Side Session Shell

To start a shell on the host with session environment variables already configured:

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent shell
```

This starts a bash shell with the working directory set to `sessions/pr-rclcpp-160/` and the following variables defined:

```bash
echo $ROS_MAINTAINER_SESSION_ID    # pr-rclcpp-160
echo $ROS_MAINTAINER_WS            # /path/to/ros_maintainer_ws
echo $ROS_DISTRO                   # e.g. jazzy or rolling
echo $ROS_MAINTAINER_GATEWAY_URL   # http://127.0.0.1:8765
```

## Container Shell (Inside the Sandbox)

To get an interactive shell inside the actual container environment without launching an editor:

### Using the Devcontainer CLI
If you have `@devcontainers/cli` installed (`npm install -g @devcontainers/cli`):

```bash
# Start container (if not already running)
devcontainer up --workspace-folder ~/ros_maintainer_ws/sessions/pr-rclcpp-160

# Exec into an interactive bash shell
devcontainer exec --workspace-folder ~/ros_maintainer_ws/sessions/pr-rclcpp-160 bash
```

### Using Plain Docker
You can also run directly with Docker using the same volume mounts:

```bash
docker run -it --rm \
  --name session-pr-160 \
  --add-host=host.docker.internal:host-gateway \
  -v ~/ros_maintainer_ws/sessions/pr-rclcpp-160:/workspace \
  -v ~/ros_maintainer_ws/tools:/workspace/tools \
  -v ~/ros_maintainer_ws/config/maintainer_rules.md:/workspace/MAINTAINER_RULES.md:ro \
  -e ROS_DISTRO=jazzy \
  -e ROS_MAINTAINER_SESSION_ID=pr-rclcpp-160 \
  -e ROS_MAINTAINER_GATEWAY_URL=http://host.docker.internal:8765 \
  -w /workspace \
  osrf/ros:jazzy-desktop bash
```

## Running Container Helper Scripts

Inside the container, `/workspace/tools/bin` is in `$PATH`. You can run helper scripts directly:

```bash
# Log a milestone to timeline.md
ros-session-status -m "Build Success" "Compiled rclcpp with -DCMAKE_BUILD_TYPE=Debug"

# Check CI build status
ros-ci-status ros2/rclcpp#160

# Launch Jenkins CI
ros-ci-for-pr ros2/rclcpp#160 --distro jazzy --reason "Verify fix"
```

## Writing Custom Automation Scripts

You can write custom Python scripts or subagents that communicate with the Host MCP Gateway over HTTP or stdio using the official MCP Python SDK (`mcp`):

```python
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

params = StdioServerParameters(
    command="ros-maintainer-harness",
    args=["serve", "--transport", "stdio", "--workspace", "/path/to/ws"],
)

async with stdio_client(params) as (read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        print("Available tools:", [t.name for t in tools.tools])
```
