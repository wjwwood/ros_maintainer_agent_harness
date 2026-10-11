# Spike notes (issue #47), cloud container, Linux, no OrbStack
PASS baseline: flake8 clean, pytest 107 passed on a5681d3
PASS image: node:22-bookworm-slim + pi 1.1.0 + pi-web 1.202610.1 + pi-mcp-adapter 5.2.0 builds; node-pty has NO linux prebuilds (compiled from source, needs make/g++)
PASS harness `hub start --image` launches Pi image; mounts verified (ws rw, config/audit ro, no docker.sock)
FINDING hub start ignores policies.containers.allowed_images (validate_launch_spec called without policy at hub_container.py:378); needed retag to ros-maintainer-harness-hub:*
FINDING Linux: gateway bridge forwarder needs `ip` binary (detect_linux_docker_bridge_ip); without it containers cannot reach gateway. Spike used manual forwarder.
FINDING Linux: hub runs as uid 1001/1000; bind-mounted ws owned by other uid is not writable (OrbStack hides this). cpus policy 4 > host 2 cpus fails.
PASS Pi -> stub OpenAI-compatible model at host.docker.internal:8000 via models.json, apiKey from $ENV
PASS Pi MCP -> gateway streamable-http with Bearer ${ROS_MAINTAINER_GATEWAY_TOKEN}; 32 tools; exposed via `codemode` tool (names mcp__ros_maintainer_harness__<tool>)
PASS authz: hub role denied respond_approval_request, push_release, stop_session_container (UNAUTHORIZED)
SECURITY default project trust: .pi/mcp.json and .pi/extensions from checkout IGNORED
SECURITY defaultProjectTrust=always: malicious .pi/extensions RAN with gateway token in env; .pi/mcp.json header interpolation leaked the token to attacker host
PASS pi-web in hub container (sessiond + server 0.0.0.0:4096) reachable via harness-published 127.0.0.1:4096
PASS host pi-web gateway + machine registered via POST /api/machines {name, baseUrl, token?}; health online; UI shows container project, files, terminal, sessions
PASS browser UI prompt -> gateway -> container pi-web -> Pi -> stub model (key from container env). Multiple sessions per project; spawn_session/spawn_subsession tools present
SECURITY pi-web has NO inbound auth; token field is only an outbound Bearer; machines.json plaintext 0600
SECURITY host gateway always has Local machine: unauthenticated POST /api/projects {path:/etc} succeeded (host FS exposed to anyone reaching gateway port)
NOT RUN paid provider (OpenRouter reachable but no key); anthropic-messages api type untested (stub is OpenAI only)
PASS session container via harness (osrf/ros:rolling-desktop, Ubuntu 26.04, root, no node): portable bundle (node + pi + pi-web, 644M) from <ws>/tools mounted ro at identical path runs Pi, MCP, pi-web
PASS second fleet machine (session-spike1 on 127.0.0.1:4100) online through the same host gateway
PASS session role authz: hub/admin tools denied; cannot act on another session; release tools denied
SECURITY lateral: session container reached hub pi-web at 172.17.0.2:4096 over default bridge with no creds; read project list and WROTE a project
PASS mitigation: container on its own user-defined docker network still reaches host gateway (8765) and model (8000) via host-gateway but not other containers
