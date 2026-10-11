# T3 Code spike (issue #47)

Throwaway scaffolding. Image: `Dockerfile.t3` (t3 0.0.45 + opencode-ai, non-root uid 1001).

Setup used:
- Two containers on a user-defined Docker network, each running
  `t3 serve --host 0.0.0.0 --port 3773 --base-dir ~/t3home ~/proj`.
- `~/t3home/userdata/settings.json`: `{"providers":{"opencode":{"enabled":true}}}`.
- Register the project with `t3 project add --base-dir ~/t3home /home/maintainer/proj`. Do not type
  an absolute path into the UI folder picker: it is relative to the home directory and "Create & Add"
  makes a new empty folder (`/home/maintainer/home/maintainer/proj`), which broke the first run.
- With the project registered correctly, both global (`~/.config/opencode/opencode.json`) and
  project-local `opencode.json` work. Project-local config is a token-exfiltration path for PR
  checkouts; `OPENCODE_DISABLE_PROJECT_CONFIG=1` blocks it.
- Start the server with `T3CODE_TELEMETRY_ENABLED=false`.
- Stub OpenAI-compatible server and header-logging MCP stand-in from `spike/pi/`.

Probes: `POST /api/auth/browser-session {"credential": ...}`, `GET /api/auth/session`,
`t3 auth pairing create|list|revoke`, `t3 auth session issue|list|revoke --base-dir ...`.
