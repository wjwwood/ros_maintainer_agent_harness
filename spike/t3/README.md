# T3 Code spike (issue #47)

Throwaway scaffolding. Image: `Dockerfile.t3` (t3 0.0.45 + opencode-ai, non-root uid 1001).

Setup used:
- Two containers on a user-defined Docker network, each running
  `t3 serve --host 0.0.0.0 --port 3773 --base-dir ~/t3home ~/proj`.
- `~/t3home/userdata/settings.json`: `{"providers":{"opencode":{"enabled":true}}}`.
- Provider and MCP config in `~/.config/opencode/opencode.json` (global). A project-local
  `opencode.json` appears in T3's model picker but turns fail with "Model not found".
- Stub OpenAI-compatible server and header-logging MCP stand-in from `spike/pi/`.

Probes: `POST /api/auth/browser-session {"credential": ...}`, `GET /api/auth/session`,
`t3 auth pairing create|list|revoke`, `t3 auth session issue|list|revoke --base-dir ...`.
