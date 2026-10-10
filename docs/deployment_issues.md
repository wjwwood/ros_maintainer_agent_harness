# Containerized Hub-and-Spoke Deployment Issues

This table tracks the GitHub issues for the containerized Hub-and-Spoke architecture on macOS (OrbStack) and Linux Docker.

| Issue | Title | Area | Status |
| :--- | :--- | :--- | :--- |
| [#26](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/26) | Tracking: hub-and-spoke deployment with containerized OpenCode agents | Architecture / Epic | Implemented |
| [#27](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/27) | Document the hub-and-spoke architecture, decisions, and threat model | Documentation | Closed in [#42](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/42) |
| [#28](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/28) | Accept `-w` / `--workspace` both before and after subcommands | CLI | Closed in [#42](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/42) |
| [#29](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/29) | Add `gateway` commands to run the host launch service on localhost | Gateway Service | Closed in [#43](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/43) |
| [#30](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/30) | Per-role tokens and tool authorization on the launch service | Security & Auth | Closed in [#43](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/43) |
| [#31](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/31) | Validate container launches against a policy spec | Container Security | Closed in [#44](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/44) |
| [#32](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/32) | Hub image and hub lifecycle commands (no Docker socket in the hub) | Hub Container | Closed in [#45](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/45) |
| [#33](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/33) | Investigate running OpenCode in containers behind a single OpenCode instance | OpenCode Architecture | Closed in [#42](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/42) |
| [#34](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/34) | Session containers run an OpenCode agent, and the launch service returns how to attach | Session Runtime | Implemented |
| [#35](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/35) | Provide LLM credentials to in-container agents without exposing host auth files | Credentials & Security | Implemented |
| [#36](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/36) | Generate OpenCode config and instructions for Hub and Session agents | Agent Config | Implemented |
| [#37](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/37) | Hub starts session conversations through the launch service | Hub Coordinator | Implemented |
| [#38](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/38) | Add `redeploy` and document the dev-conversation-on-host workflow | Deployment & Dev Workflow | Closed in [#45](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/45) |
| [#39](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/39) | `doctor`: check OrbStack/Docker, the hub, the launch service, architecture, and OpenCode | Diagnostics | Closed in [#45](https://github.com/wjwwood/ros_maintainer_agent_harness/pull/45) |
| [#40](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/40) | Add an injectable container runner and an opt-in Docker end-to-end test | Testing | Implemented |
| [#41](https://github.com/wjwwood/ros_maintainer_agent_harness/issues/41) | Update the documentation for the hub-and-spoke deployment | Documentation | Implemented |
