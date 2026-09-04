# End-to-End Walkthrough: Triaging a ROS 2 PR

This walkthrough demonstrates a complete lifecycle of investigating, fixing, and testing a ROS 2 pull request using the harness and an AI coding agent.

In this scenario, we are triaging an issue reported on `ros2/rclcpp#160` (a hypothetical deadlock in timer callbacks).

---

## 1. Set Up the Session

Run `session from-pr` with the PR reference:

```bash
ros-maintainer-harness session from-pr ros2/rclcpp#160
```

Behind the scenes, the harness:
1. **Queries GitHub**: Retrieves metadata (title, body, author, base branch, changed files).
2. **Infers Target Distro**: Notes that the base branch is `jazzy`, so the target ROS distribution is Jazzy.
3. **Prepares Shared Clones**: Checks if `shared_repos/ros2/rclcpp` exists; clones it if missing.
4. **Creates Git Worktree**: Fetches the PR ref (`pull/160/head`) and creates a linked worktree at `sessions/pr-rclcpp-160/src/rclcpp` on branch `pr-160`.
5. **Configures Devcontainer**: Writes `.devcontainer/devcontainer.json` referencing `osrf/ros:jazzy-desktop`.
6. **Writes MCP Configs**: Generates `.mcp.json`, `.cursor/mcp.json`, and `.vscode/mcp.json`.
7. **Initializes Context**:
   - Creates `timeline.md` with an initial entry linking to the PR.
   - Creates `TASK.md` detailing the problem, files to inspect, and verification goals.

---

## 2. Launch Your AI Agent

Launch your preferred agent in the session. Here we use Claude Code:

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent claude
```

Claude starts in `sessions/pr-rclcpp-160/` with the MCP server active and environment variables set.

Give Claude its initial prompt:

```text
Please read TASK.md and MAINTAINER_RULES.md. Inspect the changes in src/rclcpp, build the package with colcon, and run the test suite to reproduce the reported issue.
```

---

## 3. Local Reproduction and Fix

Inside the container sandbox, the agent works autonomously:

1. **Builds the package**:
   ```bash
   colcon build --symlink-install --packages-select rclcpp
   ```
2. **Runs the test suite**:
   ```bash
   colcon test --packages-select rclcpp --pytest-args -k test_timer
   colcon test-result --verbose
   ```
3. **Logs progress**:
   The agent records status notes to `timeline.md` (e.g. using the `log_status` tool or `ros-session-status` script):
   ```
   - **[14:22:10]** **Status**: Reproduced deadlock in test_timer_callback_after_shutdown
   ```
4. **Applies the fix**:
   The agent edits the source files in `src/rclcpp/` and reruns tests until they pass.
5. **Commits locally**:
   The agent commits the change to the local `pr-160` branch, ensuring maintainer DCO sign-off conventions are respected:
   ```bash
   git commit -s -m "Fix deadlock in timer callback during executor shutdown"
   ```

---

## 4. Trigger and Monitor CI

Once local tests pass, the fix needs to be verified on the ROS 2 build farm (`ci.ros2.org`).

The agent calls the `launch_jenkins_ci` tool:
```json
{
  "session_id": "pr-rclcpp-160",
  "pr_url": "ros2/rclcpp#160",
  "target_distro": "jazzy",
  "reason": "Verify timer deadlock fix on multi-platform build farm"
}
```

The host gateway evaluates the policy (checking CI concurrency and cooldown timers), launches the Jenkins build, and starts tracking it in the background.

### Checking CI Without Burning Context Tokens
Instead of having the agent poll Jenkins continuously and ingest megabytes of logs, you (or the agent) can check status efficiently:

```bash
# Block until the build completes
ros-maintainer-harness ci status ros2/rclcpp#160 --wait

# If the build failed, get a compact summary of test failures and compiler errors
ros-maintainer-harness ci summary ros2/rclcpp#160
```

The gateway extracts only the failed JUnit tests and compiler error messages, keeping the context token footprint minimal.

---

## 5. Pushing Changes & Maintainer Approval

When the agent is ready to push its branch to remote:

1. The agent calls the `git_push` MCP tool:
   ```json
   {
     "session_id": "pr-rclcpp-160",
     "repo_path": "/path/to/shared_repos/ros2/rclcpp",
     "branch": "wjwwood/fix-timer-deadlock",
     "reason": "Push tested fix for PR 160"
   }
   ```
2. **Policy Evaluation**:
   - The gateway evaluates the push against `config/policy.yaml`:
     - The target branch must match `allowed_branch_patterns` (e.g., `^wjwwood/.*$`).
     - The target repository must match `allowed_repositories` (e.g., `ros2/*`).
     - Direct pushes to base branches (`main`, `rolling`, `jazzy`, etc.) are blocked.
   - If the push satisfies all policy rules and targets the maintainer's own fork or branch, the push proceeds without requiring an approval ticket.
   - If the push targets an external third-party contributor's fork (and `require_fork_approval: true`), the gateway creates a pending approval ticket and returns:
     ```json
     {
       "status": "PENDING_APPROVAL",
       "request_id": "req-9a8b7c6d",
       "message": "Push to external fork requires maintainer approval. Request ID: req-9a8b7c6d"
     }
     ```
3. **Maintainer Approval**:
   The agent reports the ticket ID in the chat session and pauses. You inspect the diff in the session worktree, then approve the ticket on the host:
   ```bash
   ros-maintainer-harness approval approve req-9a8b7c6d --comment "Verified locally and on CI"
   ```
4. **Resuming the Push**:
   Once you notify the agent that the request has been approved, the agent retries `git_push`. The gateway finds the active approved ticket in `audit/approvals.json` and executes the push.

   *(Note: Currently, the agent relies on user confirmation in chat to know when an approval ticket is granted. A potential future enhancement is an event-driven notification or a blocking `wait_for_approval` tool so the agent can resume automatically).*

---

## 6. Cleanup

Once the PR has been updated or merged, prune the session:

```bash
ros-maintainer-harness session prune pr-rclcpp-160
```

This removes the linked worktree and session overlay directory (`build/`, `install/`, `log/`, etc.) while keeping the audit log in `audit/audit.jsonl` intact for future reference.
