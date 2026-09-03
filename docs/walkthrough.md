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
   - The gateway checks if the branch matches allowed patterns (e.g., `^wjwwood/.*$`).
   - If pushing to the maintainer's own fork/branch, the push proceeds automatically.
   - If pushing directly to a 3rd-party contributor's fork, the policy requires explicit maintainer approval. The tool returns:
     ```json
     {
       "status": "PENDING_APPROVAL",
       "request_id": "req-9a8b7c6d",
       "message": "Push to external fork requires maintainer approval."
     }
     ```
3. **Maintainer Approval**:
   You inspect the diff in the session worktree, then approve the ticket:
   ```bash
   ros-maintainer-harness approval approve req-9a8b7c6d --comment "Verified locally and on CI"
   ```
4. The agent retries `git_push`, which succeeds now that the approval ticket is granted.

---

## 6. Cleanup

Once the PR has been updated or merged, prune the session:

```bash
ros-maintainer-harness session prune pr-rclcpp-160
```

This removes the linked worktree and session overlay directory (`build/`, `install/`, `log/`, etc.) while keeping the audit log in `audit/audit.jsonl` intact for future reference.
