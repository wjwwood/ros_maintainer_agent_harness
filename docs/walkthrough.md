# End-to-End Walkthrough: Triaging a ROS 2 PR

This walkthrough demonstrates a complete lifecycle of investigating, fixing, and testing a ROS 2 pull request using the harness and an AI coding agent.

In this scenario, we are triaging an issue reported on `ros2/rclcpp#160` (a hypothetical deadlock in timer callbacks).

---

## 1. Starting from the Maintainer Hub Conversation (OpenCode Hub-and-Spoke)

When running the three-tier Hub-and-Spoke deployment (`gateway start` + `hub start`), start by attaching OpenCode to the Maintainer Hub container:

```bash
ros-maintainer-harness -w ~/ros_maintenance_ws gateway start
ros-maintainer-harness -w ~/ros_maintenance_ws hub start
ros-maintainer-harness -w ~/ros_maintenance_ws hub attach
```

Inside the Hub conversation:
1. Ask the Hub what needs attention:
   - `"What is the status of things we're working on?"` -> calls `get_workspace_status()`.
   - `"What should I work on next?"` -> calls `get_next_actions(repos=["ros2/rclcpp"])`.
2. Ask the Hub to start a session for `ros2/rclcpp#160`:
   - `"I want to work on ros2/rclcpp#160"`.
   - The Hub calls `start_session_conversation(pr_ref="ros2/rclcpp#160", mode="auto")` on the Host Launch Service.
   - The Launch Service scaffolds `sessions/pr-rclcpp-160`, starts `ros-harness-pr-rclcpp-160`, starts the in-container `opencode serve` agent, seeds the initial prompt from `TASK.md`, and returns the session attach command.
3. Attach to the dedicated Session conversation from your host terminal (or switch to its endpoint in the OpenCode Desktop app):
   ```bash
   ros-maintainer-harness -w ~/ros_maintenance_ws session attach pr-rclcpp-160
   ```

---

## 2. Direct CLI Session Setup & Agent Launch

You can also scaffold a session and launch an agent directly from the host CLI:

```bash
ros-maintainer-harness session from-pr ros2/rclcpp#160
```

Behind the scenes, the harness:
1. **Queries GitHub**: Retrieves metadata (title, body, author, base branch, changed files).
2. **Infers Target Distro**: Notes that the base branch is `jazzy`, so the target ROS distribution is Jazzy.
3. **Prepares Shared Clones**: Checks if `shared_repos/ros2/rclcpp` exists; clones it if missing.
4. **Creates Git Worktree**: Fetches the PR ref (`pull/160/head`) and creates a reference-clone checkout at `sessions/pr-rclcpp-160/src/rclcpp` on branch `pr-160`.
5. **Configures Devcontainer**: Writes `.devcontainer/devcontainer.json` referencing `osrf/ros:jazzy-desktop`.
6. **Writes MCP Configs**: Generates `opencode.json`, `.mcp.json`, `.cursor/mcp.json`, and `.vscode/mcp.json`.
7. **Initializes Context**:
   - Creates `timeline.md` with an initial entry linking to the PR.
   - Creates `TASK.md` detailing the problem, files to inspect, and verification goals.

Launch your preferred agent in the session (for example, OpenCode or Claude Code):

```bash
ros-maintainer-harness session launch pr-rclcpp-160 --agent claude
```

The agent starts in `sessions/pr-rclcpp-160/` with the MCP server active and environment variables set.

Give the agent its initial prompt:

```text
Please read TASK.md and MAINTAINER_RULES.md. First inspect the diff in src/rclcpp against origin/rolling for any suspicious modifications to build scripts, test code, or CI workflows, and check for committed credentials. Once verified safe, build the package with colcon and run the test suite to reproduce the reported issue.
```

---

## 3. Local Reproduction and Fix

Inside the container sandbox, the agent works autonomously:

1. **Inspects the diff before building**:
   Checks `git diff origin/rolling` to understand the PR changes and ensure there are no suspicious build script alterations (`CMakeLists.txt`, `package.xml`), unexpected network calls in test code, or committed secrets before running local builds.
2. **Builds the package**:
   ```bash
   colcon build --symlink-install --packages-select rclcpp
   ```
3. **Runs the test suite**:
   ```bash
   colcon test --packages-select rclcpp --pytest-args -k test_timer
   colcon test-result --verbose
   ```
4. **Logs progress**:
   The agent records status notes to `timeline.md` (e.g. using the `log_status` tool or `ros-session-status` script):
   ```
   - **[14:22:10]** **Status**: Reproduced deadlock in test_timer_callback_after_shutdown
   ```
5. **Applies the fix**:
   The agent edits the source files in `src/rclcpp/` and reruns tests until they pass.
6. **Commits locally**:
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

### Opening a New Pull Request (Pre-Filled GitHub URL by Default)
When a task involves opening a new Pull Request (after pushing a topic branch to `origin` or your fork):
- By default, calling `create_pull_request` (or `ros-maintainer-harness create-pr`) generates a **pre-filled GitHub `/compare/<base>...<head>?quick_pull=1&title=...&body=...` URL** (`status: "WEB_URL_READY"`, no approval ticket required):
  ```bash
  ros-maintainer-harness create-pr -s pr-rclcpp-160 --repo ros2/rclcpp --title "Fix timer callback deadlock" -F pr_body.md --head wjwwood/fix_timer_deadlock --base rolling -m "Prepare PR for timer fix"
  ```
- Click the returned link to review the commit history, file diff, title, and pre-filled PR template description in your browser before clicking **Create pull request** yourself.
- If you instead want the agent to open the PR directly via the GitHub REST API, pass `--api` (or `web_url=False` on the MCP tool), which gates creation on a maintainer approval ticket.

### Releasing a ROS Package (`release push` & `release bloom`)
When preparing a ROS package release:
1. Inside the session container, the agent runs `catkin_generate_changelog`, cleans up `CHANGELOG.rst`, commits the changelogs, and runs `catkin_prepare_release --no-push` to create the local version bump commit and tag.
2. **Push Release Commit & Tag (Ticket-Gated)**:
   The agent requests a release push via `push_release` (or `ros-maintainer-harness release push -s <session_id> --repo <repo> --branch rolling --tag <version> -m "..."`). After you approve the `release_push` ticket, retrying pushes `<tag_commit>:refs/heads/rolling` and `refs/tags/<version>` to `origin`.
3. **Run Bloom Release (Ticket-Gated)**:
   The agent requests a Bloom release via `run_bloom_release` (or `ros-maintainer-harness release bloom -s <session_id> --repo <repository> --rosdistro rolling -m "..."`). After you approve the `bloom_release` ticket, the host gateway executes `bloom-release --rosdistro rolling --track rolling --non-interactive --no-web <repository>` using your host `~/.config/bloom` credentials and records the resulting `ros/rosdistro` PR URL.

---

## 6. Cleanup

Once the PR has been updated or merged, prune the session:

```bash
ros-maintainer-harness session prune pr-rclcpp-160
```

This removes the linked worktree and session overlay directory (`build/`, `install/`, `log/`, etc.) while keeping the audit log in `audit/audit.jsonl` intact for future reference.
