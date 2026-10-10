# Containerized Hub-and-Spoke Deployment Issues

This table tracks the GitHub issues for the containerized Hub-and-Spoke architecture on macOS (OrbStack) and Linux Docker.

| Issue | Title | Area | Status |
| :--- | :--- | :--- | :--- |
| [#26](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/26) | Epic: Containerized Hub-and-Spoke Agent Deployment on macOS (OrbStack) | Architecture / Epic | In Progress |
| [#27](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/27) | Fix `policy.yaml` schema drift and tighten default branch patterns | Policy & Config | Closed in [#42](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/42) |
| [#28](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/28) | Accept `-w` / `--workspace` both before and after subcommands | CLI | Closed in [#42](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/42) |
| [#29](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/29) | Add `gateway` commands to run the host launch service on localhost | Gateway Service | Closed in [#43](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/43) |
| [#30](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/30) | Per-role tokens and tool authorization on the launch service | Security & Auth | Closed in [#43](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/43) |
| [#31](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/31) | Validate container launches against a policy spec | Container Security | Closed in [#44](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/44) |
| [#32](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/32) | Hub image and hub lifecycle commands (no Docker socket in the hub) | Hub Container | Implemented |
| [#33](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/33) | Investigate running OpenCode in containers behind a single OpenCode instance | OpenCode Architecture | Closed in [#42](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/42) |
| [#34](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/34) | Session containers run an OpenCode agent, and the launch service returns how to attach | Session Runtime | Planned |
| [#35](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/35) | Provide LLM credentials to in-container agents without exposing host auth files | Credentials & Security | Planned |
| [#36](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/36) | Generate OpenCode config and instructions for Hub and Session agents | Agent Config | Planned |
| [#37](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/37) | Hub starts session conversations through the launch service | Hub Coordinator | Planned |
| [#38](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/38) | Add `redeploy` and document the dev-conversation-on-host workflow | Deployment & Dev Workflow | Implemented |
| [#39](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/39) | `doctor`: check OrbStack/Docker, the hub, the launch service, architecture, and OpenCode | Diagnostics | Implemented |
| [#40](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/40) | Add an injectable container runner and an opt-in Docker end-to-end test | Testing | In Progress |
| [#41](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/41) | Update the documentation for the hub-and-spoke deployment | Documentation | In Progress |
