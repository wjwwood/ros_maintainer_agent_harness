# Using Claude Code with the Harness

[Claude Code](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) is a command-line AI coding agent that runs directly in your terminal. It natively supports the Model Context Protocol (MCP) by reading configuration from `.mcp.json` in the current working directory.

## Prerequisites

- Claude Code CLI installed:
  ```bash
  npm install -g @anthropic-ai/claude-code
  ```
- Authenticated with Anthropic (run `claude` once to complete login).

## Quick Start

### 1. Launch Claude Code in a Session

Once a session has been created (for example via `session from-pr ros2/rclcpp#160`), start Claude Code with:

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent claude
```

This launches Claude with:
- The working directory set to the session folder (`sessions/pr-rclcpp-160/`).
- Session environment variables set (`ROS_MAINTAINER_SESSION_ID`, `ROS_DISTRO`, etc.).
- MCP server configured via `.mcp.json`, exposing all gateway tools (`git_push`, `launch_jenkins_ci`, `log_status`, etc.).

### 2. Giving Claude the Initial Task

When Claude Code starts, give it an initial prompt pointing to the session instructions:

```text
Please read TASK.md and MAINTAINER_RULES.md. Review the PR changes in src/, check if there are test failures, and log your initial findings to timeline.md.
```

Because `TASK.md` was generated during session creation, it already contains the PR title, author, description, changed files list, and verification objectives.

## How Claude Interacts with the Harness

### Local Autonomy
Claude performs local tasks using its built-in shell execution tools:
- Inspecting files: `git status`, `git diff` inside `src/`.
- Building packages: `colcon build --symlink-install --packages-select <pkg>`.
- Running tests: `colcon test --packages-select <pkg> && colcon test-result --verbose`.

### Logging Milestones
Claude can record milestones and status notes to `timeline.md` in two ways:
1. By calling the `log_status` MCP tool:
   ```json
   {
     "session_id": "pr-rclcpp-160",
     "message": "All unit tests in test_timer passing locally",
     "milestone": "Local Tests Green"
   }
   ```
2. Or by running the helper script from the container's `$PATH`:
   ```bash
   ros-session-status -m "Local Tests Green" "All unit tests in test_timer passing locally"
   ```

### Interacting with Remote Systems
Claude cannot push to remotes or trigger Jenkins CI directly through raw git or curl commands (it lacks write tokens and SSH keys). Instead, it calls the host gateway's MCP tools:
- **`launch_jenkins_ci`**: Submits a Jenkins CI build on `ci.ros2.org`.
- **`get_ci_status`**: Polls build status, with optional host-side blocking (`wait_for_completion=True`).
- **`get_ci_summary`**: Retrieves concise test failure reports and compiler error log excerpts without burning context tokens on full build logs.
- **`git_push`**: Requests a git push. If pushing to an external contributor fork, the gateway returns a `PENDING_APPROVAL` ticket.

## Tips and Best Practices

- **Conserving Context Tokens on CI Logs**: Remind Claude not to fetch full raw Jenkins console logs with `curl`. Instruct it to use `get_ci_summary` or `ros-maintainer-harness ci summary`, which extracts only the relevant compiler errors and failed test traces.
- **Handling Approvals**: When pushing to external forks, the gateway will return an approval ticket (e.g. `req-1234abcd`). Claude will report this ticket to you in the terminal, and you can approve it in another window with:
  ```bash
  ros-maintainer-harness approval approve req-1234abcd --comment "LGTM"
  ```
  Once approved, Claude can re-try the `git_push` tool call.
