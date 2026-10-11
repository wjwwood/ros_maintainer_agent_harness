# Pi spike scaffolding (issue #47)

Throwaway scripts used to evaluate Pi, pi-web and pi-mcp-adapter as the in-container agent and UI.
Not part of the package. Results are posted on the issue; raw notes are in `RESULTS_RAW.md`.

| File | Purpose |
|---|---|
| `Dockerfile.spike` | Pi 1.1.0, pi-web 1.202610.1, pi-mcp-adapter 5.2.0 on `node:22-bookworm-slim` |
| `stub_llm.py` | OpenAI-compatible stub model. Logs requests and returns a scripted `codemode` tool call when the prompt contains `CALLTOOL` (code read from `$SPIKE_DIR/code.js`) |
| `fwd.py` | TCP forwarder from the Docker bridge address to the gateway on 127.0.0.1 (stand-in for the harness bridge forwarder when `ip` is missing) |
| `attacker.py` | Fake MCP endpoint that logs Authorization headers, to test token leakage from untrusted `.pi/` config |

Rough procedure (Linux, Docker): build the image, tag it `ros-maintainer-harness-hub:spike-pi`, `init`, `token-setup --no-token`,
`gateway start`, `hub start --image ros-maintainer-harness-hub:spike-pi`, run `fwd.py <docker0 ip> 8765`, run `stub_llm.py <docker0 ip> 8000`,
then write `~/.pi/agent/models.json` and `~/.pi/agent/mcp.json` inside the container as shown in the issue comment.
