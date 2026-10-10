# Host Development and Redeployment Workflow

This guide describes how to develop and modify `ros_maintainer_agent_harness` on the host machine while live maintainer sessions are running.

---

## 1. Separation of Concerns: Dev Conversation vs. Maintainer Hub

The system separates harness development from ROS 2 maintenance operations:

1. **Dev Conversation (Host Repository)**:
   - Runs in the `ros_maintainer_agent_harness` source checkout (for example `~/ros2_ws/tools/ros-maintainer-agent-harness`).
   - Edits harness Python modules (`src/`), unit tests (`tests/`), and documentation (`docs/`).
   - Runs linters and unit tests locally.
2. **Gateway Launch Service (Host Daemon)**:
   - Runs the installed `ros-maintainer-harness` snapshot on `127.0.0.1:8765`, enforcing role-scoped bearer tokens (`admin`, `hub`, `session:<id>`), container launch policies, and maintainer approval tickets.
3. **Maintainer Hub Container (`ros-harness-hub`)**:
   - Runs the unprivileged coordinator agent inside `ros-maintainer-harness-hub:latest` with no Docker socket, mounting `<workspace_root>` (`rw`) and `<workspace_root>/config` + `<workspace_root>/audit` (`ro`).
4. **Session Containers (`ros-harness-<session_id>`)**:
   - Run isolated per-PR ROS 2 build/test environments and per-session OpenCode agents.

---

## 2. Why Editable Installs (`pip install -e .`) Are Prohibited

Never install `ros_maintainer_agent_harness` in editable mode (`pip install -e .`) when live maintainer sessions exist:
- Host `PreToolUse` hooks (`~/.gemini/config/hooks.json`, `.claude/settings.json`) and the Gateway Launch Service invoke `~/.local/bin/ros-maintainer-harness` on tool calls.
- An editable install exposes live sessions to half-written edits or transient syntax errors in the working tree.
- Instead, test edits against the working tree (`pyproject.toml` configures `pythonpath = ["src"]` for `pytest`) and ship a verified wheel snapshot using `ros-maintainer-harness redeploy`.

---

## 3. Running Linting and Unit Tests

Before committing or redeploying any change, run `flake8` and `pytest` from the repository root:

```bash
export PATH="$HOME/.local/bin:$PATH"

# 1. Lint all source and test files (120-character line limit)
flake8 src/ tests/ --max-line-length=120 --statistics

# 2. Run the unit test suite (docker integration tests are skipped by default)
pytest -v

# 3. Run the standard library unittest discovery runner (used in GitHub Actions CI)
PYTHONPATH=src python3 -m unittest discover -s tests -p "test_*.py" -v
```

To run the opt-in live Docker/OrbStack end-to-end integration tests:

```bash
pytest -v -m docker
```

---

## 4. Shipping a Tested Snapshot with `redeploy`

Once tests pass and changes are committed, use `ros-maintainer-harness redeploy` to build a clean wheel and update the running deployment without disturbing active session containers:

```bash
ros-maintainer-harness -w ~/ros_maintenance_ws redeploy --also-host
```

### What `redeploy` Does
1. **Verifies Clean Git State**: Refuses to deploy from a dirty working tree unless `--allow-dirty` is explicitly passed.
2. **Builds an Isolated Wheel**: Builds `dist/ros_maintainer_agent_harness-<version>-py3-none-any.whl` into a temporary directory.
3. **Builds the Hub Image**: Builds `ros-maintainer-harness-hub:latest` and `ros-maintainer-harness-hub:<version>-<git_sha>` from the packaged `Dockerfile.hub` and the built wheel (never copying the raw working tree).
4. **Updates the Host Snapshot (`--also-host`)**: Installs the wheel into `~/.local` using `pip install --user --no-build-isolation --no-deps --force-reinstall`.
5. **Restarts Gateway and Hub Only**: Restarts the Host Gateway Launch Service and recreates `ros-harness-hub`, leaving all running `ros-harness-<session_id>` containers untouched.
6. **Reports Version Alignment**: Prints the previous and new versions and verifies that the host CLI, gateway service, and Hub image versions match.
