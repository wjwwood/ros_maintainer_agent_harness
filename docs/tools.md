# Shared Tools & Extensibility

The harness provides a shared tool catalog in tools/ that is mounted into every container session. This allows maintainers and agents to share scripts, automation utilities, and diagnostics across multiple workspaces without re-installing them per session.

---

## 1. Why a Shared Tool Catalog?

When maintaining packages across different ROS 2 distributions (e.g. Jazzy on Ubuntu 24.04 vs. Humble on Ubuntu 22.04), sharing compiled virtual environments (venv/) across containers is brittle:
- **ABI & Binary Incompatibilities**: Python patch versions, glibc versions, and compiled C-extensions differ between container base images.
- **Concurrent Modification Races**: Multiple containers attempting to install packages into a shared directory lead to file corruption.

### The Solution: Shared Scripts + Container-Local Dependencies
The harness decouples script definitions from the Python runtime:
1. **Shared Executables (tools/bin/)**: Scripts are placed in tools/bin/ with standard #!/usr/bin/env python3 shebangs.
2. **Mounted in Container PATH**: Every session devcontainer mounts tools/bin/ to /workspace/tools/bin and prepends it to PATH.
3. **Declarative Dependencies (tools/requirements.txt)**: Dependencies (such as PyGithub, requests) are listed in tools/requirements.txt. Each devcontainer installs them into its local environment during container initialization via postCreateCommand.

---

## 2. Built-in Utilities & Design Rationale

Rather than acting as general-purpose wrappers, the built-in scripts address specific pain points in ROS 2 maintainer workflows:

### ros-session-status — Structured Narrative & Milestones
- **The Problem**: AI agents tend to write unstructured or verbose notes, making it hard for a human maintainer to skim the current state of a task after several hours.
- **How it Works**: Appends timestamped status updates or milestone badges (🏆 Milestone: ...) to sessions/<id>/timeline.md. Maintainers can open timeline.md in an editor or preview panel to instantly see what the agent has attempted, what failed, and what passed.

### ros-find-restarted-ci — Discovering Rescheduled Jenkins Builds
- **The Problem**: On ROS 2 repositories, Jenkins CI builds on ci.ros2.org are frequently triggered or rescheduled by maintainers through comment phrases (@ros-pull-request-builder retest this please). Tracking down which build corresponds to the latest commit across long GitHub comment threads is error-prone.
- **How it Works**: Inspects the Jenkins build farm for queued or running jobs associated with a PR, parses their parameters, and optionally updates the GitHub PR status comment with active build links in-place.

### ros-ci-for-pr & ros-ci-status — Sandboxed CI Interaction
- **The Problem**: The container sandbox has no Jenkins API tokens or credentials, preventing the agent from triggering builds directly via curl.
- **How it Works**: These scripts check for the ROS_MAINTAINER_GATEWAY_URL environment variable. When present, they submit CI requests and query build status through the Host MCP Gateway, allowing the agent to launch and check CI while keeping credentials secure on the host.

---

## 3. Adding Custom Maintainer Tools

You can easily extend the tool catalog with your own project-specific utilities:

### Step 1: Create the Executable Script
Add your script to tools/bin/:

```python
#!/usr/bin/env python3
import sys

print("Checking ABI compatibility against upstream...")
# Your ABI inspection logic here
sys.exit(0)
```

Make sure to mark the script executable (`chmod +x tools/bin/<name>`).

### Step 2: Add Python Dependencies (if needed)
If your script requires third-party Python packages, add them to `tools/requirements.txt`:

```text
requests>=2.28.0
tabulate>=0.9.0
```

Any new session container created after this will automatically install these packages on startup.

### Step 3: Document the Tool in tools/README.md
When an AI agent starts up, it reads `tools/README.md` to discover what utilities are available:

```markdown
### check-abi-breakage
Runs abi-compliance-checker on the compiled libraries in build/ against reference headers in /opt/ros/$ROS_DISTRO/include.
```

---

## 4. Best Practices for Custom Tools

- **Stateless Operation**: Avoid saving state inside the container root filesystem, as devcontainers are ephemeral. Persist task state into `/workspace/scratch/` or `/workspace/timeline.md`.
- **Use the Gateway for Privileged Actions**: If your custom tool needs to push git tags, access internal build farm credentials, or create GitHub releases, design it to call the Host MCP Gateway rather than baking private keys into the container.
- **UTF-8 Output**: Ensure scripts reconfigure their output streams for UTF-8 (`sys.stdout.reconfigure(encoding='utf-8')`) to avoid encoding crashes on multi-platform runners.
