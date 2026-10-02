# Copyright 2026 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional

DEFAULT_DISTRO_IMAGES = {
    'rolling': 'docker.io/osrf/ros:rolling-desktop',
    'jazzy': 'docker.io/osrf/ros:jazzy-desktop',
    'iron': 'docker.io/osrf/ros:iron-desktop',
    'humble': 'docker.io/osrf/ros:humble-desktop',
    'kilted': 'docker.io/osrf/ros:kilted-desktop',
    'lyrical': 'docker.io/osrf/ros:lyrical-desktop',
    'noetic': 'docker.io/osrf/ros:noetic-desktop',
}


def get_image_for_distro(distro: str, custom_image: Optional[str] = None) -> str:
    """Resolve the container image name for a target ROS distro."""
    if custom_image:
        return custom_image
    return DEFAULT_DISTRO_IMAGES.get(distro.lower(), f"docker.io/osrf/ros:{distro.lower()}-desktop")


def load_workspace_env(workspace_root: Path) -> Dict[str, str]:
    """Load key=value environment variables from <workspace_root>/.env if present."""
    env_file = workspace_root.resolve() / '.env'
    env_vars: Dict[str, str] = {}
    if not env_file.exists():
        return env_vars
    try:
        for raw_line in env_file.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[len('export '):].strip()
            if '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                env_vars[k] = v
    except Exception:
        pass
    return env_vars


def save_workspace_env_var(workspace_root: Path, key: str, value: str) -> Path:
    """Save or update a key=value entry in <workspace_root>/.env with 0600 permissions."""
    env_file = workspace_root.resolve() / '.env'
    existing = load_workspace_env(workspace_root)
    existing[key] = value
    lines = [
        '# ROS Maintainer Agent Harness Workspace Environment',
        '# Do not commit this file to version control.',
    ]
    for k, v in existing.items():
        lines.append(f'{k}="{v}"')
    env_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    try:
        env_file.chmod(0o600)
    except OSError:
        pass
    return env_file


def get_container_github_token(workspace_root: Path) -> Optional[str]:
    """Retrieve the configured ROS_CONTAINER_GITHUB_TOKEN from environment or workspace .env."""
    ws_env = load_workspace_env(workspace_root)
    token = os.environ.get('ROS_CONTAINER_GITHUB_TOKEN') or ws_env.get('ROS_CONTAINER_GITHUB_TOKEN')
    return token


def detect_container_runtime() -> Optional[str]:
    """Detect available container runtime ('docker' or 'podman')."""
    for candidate in ('docker', 'podman'):
        if shutil.which(candidate):
            return candidate
    return None


def get_container_name(session_id: str) -> str:
    """Return the standardized container name for a session."""
    return f"ros-harness-{session_id}"


def check_token_and_environment(workspace_root: Path) -> Dict[str, Any]:
    """
    Check workspace readiness: initialization, container runtime, GitHub tokens, and global MCP config.
    """
    ws_root = workspace_root.resolve()
    ws_initialized = (ws_root / 'config' / 'policy.yaml').exists()
    runtime = detect_container_runtime()

    container_token = get_container_github_token(ws_root)
    if container_token is None or container_token == '':
        container_token_configured = False
        container_token_mode = 'unconfigured'
    elif container_token.lower() == 'none':
        container_token_configured = True
        container_token_mode = 'none'
    else:
        container_token_configured = True
        container_token_mode = 'token'

    # Check host token / gh auth
    ws_env = load_workspace_env(ws_root)
    host_token = (
        os.environ.get('ROS_HOST_GITHUB_TOKEN')
        or ws_env.get('ROS_HOST_GITHUB_TOKEN')
        or os.environ.get('GITHUB_TOKEN')
        or os.environ.get('GH_TOKEN')
    )
    gh_cli_token = None
    if shutil.which('gh'):
        try:
            res = subprocess.run(
                ['gh', 'auth', 'token'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                gh_cli_token = res.stdout.strip()
        except Exception:
            pass

    host_token_configured = bool(host_token or gh_cli_token)
    token_matches_host_gh = bool(
        container_token_mode == 'token'
        and gh_cli_token
        and container_token == gh_cli_token
    )

    # Check global MCP config for Gemini/Antigravity or Claude
    global_mcp_configured = False
    try:
        home_dir = Path.home()
        gemini_mcp_path = home_dir / '.gemini' / 'config' / 'mcp_config.json'
        claude_mcp_path = home_dir / '.claude.json'
        for mcp_path in (gemini_mcp_path, claude_mcp_path):
            if mcp_path.exists():
                content = mcp_path.read_text(encoding='utf-8').strip()
                if content:
                    data = json.loads(content)
                    if 'ros-maintainer-harness' in data.get('mcpServers', {}):
                        global_mcp_configured = True
    except Exception:
        pass

    warnings: List[str] = []
    recommendations: List[str] = []

    if not ws_initialized:
        warnings.append(f"Workspace at {ws_root} is not initialized.")
        recommendations.append(f"Run `ros-maintainer-harness init -w {ws_root}`.")

    if not runtime:
        warnings.append("No container runtime (docker or podman) found on PATH.")
        recommendations.append("Install Docker or Podman so session builds and tests run in isolated containers.")

    if not container_token_configured:
        warnings.append(
            "ROS_CONTAINER_GITHUB_TOKEN is not configured. Agents must not silently pass your host "
            "`gh auth token` into containers."
        )
        recommendations.append(
            "Configure a read-only fine-grained GitHub PAT with "
            "`ros-maintainer-harness token-setup --container-token <TOKEN>` "
            "or explicitly opt into unauthenticated mode with `ros-maintainer-harness token-setup --no-token`."
        )

    if token_matches_host_gh:
        warnings.append(
            "ROS_CONTAINER_GITHUB_TOKEN is identical to your active `gh auth token` on the host. "
            "If your `gh` CLI token has write permissions, untrusted code in the container could use it."
        )
        recommendations.append(
            "Create a separate fine-grained Personal Access Token with zero repository write permissions "
            "for ROS_CONTAINER_GITHUB_TOKEN."
        )

    if not global_mcp_configured:
        warnings.append("Global MCP server registration not found in ~/.gemini/config/mcp_config.json.")
        recommendations.append(
            f"Run `ros-maintainer-harness mcp-install -w {ws_root}` so host agents discover the MCP gateway."
        )

    ready = ws_initialized and (runtime is not None) and container_token_configured

    return {
        'ready': ready,
        'workspace_root': str(ws_root),
        'workspace_initialized': ws_initialized,
        'container_runtime': runtime,
        'container_token_configured': container_token_configured,
        'container_token_mode': container_token_mode,
        'host_token_configured': host_token_configured,
        'token_matches_host_gh_warning': token_matches_host_gh,
        'global_mcp_configured': global_mcp_configured,
        'warnings': warnings,
        'recommendations': recommendations,
    }


def generate_devcontainer_config(
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate a complete devcontainer.json configuration dictionary for a session.
    """
    session_dir = session_dir.resolve()
    workspace_root = workspace_root.resolve()
    tools_dir = workspace_root / 'tools'
    shared_repos_dir = workspace_root / 'shared_repos'
    rules_file = workspace_root / 'config' / 'maintainer_rules.md'
    image = get_image_for_distro(distro, custom_image)

    config: Dict[str, Any] = {
        "name": f"ROS 2 Maintainer Sandbox ({session_dir.name})",
        "image": image,
        "workspaceFolder": "/workspace",
        "workspaceMount": f"source={session_dir},target=/workspace,type=bind",
        "mounts": [
            f"source={tools_dir},target=/workspace/tools,type=bind,readonly",
            f"source={shared_repos_dir},target={shared_repos_dir},type=bind",
        ],
        "containerEnv": {
            "PATH": "/workspace/tools/bin:/root/.local/bin:${containerEnv:PATH}",
            "ROS_DISTRO": distro,
            "ROS_MAINTAINER_SESSION_ID": session_dir.name,
            "ROS_MAINTAINER_GATEWAY_URL": gateway_url or "http://host.docker.internal:8765",
            "PYTHONUNBUFFERED": "1",
        },
        "runArgs": [
            "--add-host=host.docker.internal:host-gateway",
            "--security-opt=seccomp=unconfined",
        ],
        "customizations": {
            "vscode": {
                "extensions": [
                    "ms-vscode.cpptools",
                    "ms-python.python",
                    "ms-iot.vscode-ros",
                    "twxs.cmake",
                    "eamodio.gitlens",
                ],
                "settings": {
                    "terminal.integrated.defaultProfile.linux": "bash",
                    "python.defaultInterpreterPath": "/usr/bin/python3",
                },
            },
        },
        "postCreateCommand": (
            "bash -c 'source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null && "
            "echo \"source /opt/ros/$ROS_DISTRO/setup.bash\" >> ~/.bashrc && "
            "git config --global --add safe.directory \"*\" 2>/dev/null || true && "
            f"mkdir -p {shlex.quote(str(session_dir.parent))} && "
            f"ln -sfn /workspace {shlex.quote(str(session_dir))} && "
            "(pip install -r /workspace/tools/requirements.txt 2>/dev/null || true)'"
        ),
    }

    if rules_file.exists():
        config["mounts"].append(f"source={rules_file},target=/workspace/MAINTAINER_RULES.md,type=bind,readonly")

    return config


def write_devcontainer_config(
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> Path:
    """
    Write the devcontainer.json configuration to the session's .devcontainer directory.
    """
    devcontainer_dir = session_dir / '.devcontainer'
    devcontainer_dir.mkdir(parents=True, exist_ok=True)
    config_file = devcontainer_dir / 'devcontainer.json'

    config = generate_devcontainer_config(
        session_dir=session_dir,
        workspace_root=workspace_root,
        distro=distro,
        custom_image=custom_image,
        gateway_url=gateway_url,
    )

    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)

    return config_file


def get_container_status(session_id: str, runtime: Optional[str] = None) -> Dict[str, Any]:
    """Check whether the session container is currently running."""
    rt = runtime or detect_container_runtime()
    container_name = get_container_name(session_id)
    if not rt:
        return {
            'running': False,
            'status': 'no_runtime',
            'container_name': container_name,
            'runtime': None,
        }

    res = subprocess.run(
        [rt, 'inspect', '-f', '{{.State.Running}}', container_name],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0 and res.stdout.strip().lower() == 'true':
        return {
            'running': True,
            'status': 'running',
            'container_name': container_name,
            'runtime': rt,
        }
    return {
        'running': False,
        'status': 'stopped',
        'container_name': container_name,
        'runtime': rt,
    }


def start_session_container(
    session_id: str,
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
    runtime: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Start a detached sandbox container for the given session so commands and builds
    can be executed inside it via `session exec` or the MCP `exec_in_session` tool.
    """
    rt = runtime or detect_container_runtime()
    if not rt:
        return {
            'success': False,
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    status_info = get_container_status(session_id, runtime=rt)
    container_name = status_info['container_name']
    if status_info['running']:
        return {
            'success': True,
            'status': 'already_running',
            'container_name': container_name,
            'runtime': rt,
        }

    # Remove any exited container with the same name
    subprocess.run([rt, 'rm', '-f', container_name], capture_output=True, text=True)

    session_dir = session_dir.resolve()
    workspace_root = workspace_root.resolve()
    tools_dir = workspace_root / 'tools'
    shared_repos_dir = workspace_root / 'shared_repos'
    rules_file = workspace_root / 'config' / 'maintainer_rules.md'
    image = get_image_for_distro(distro, custom_image)

    cmd = [
        rt, 'run', '-d',
        '--name', container_name,
        '--workdir', '/workspace',
        '--add-host=host.docker.internal:host-gateway',
        '--security-opt=seccomp=unconfined',
        '-v', f"{session_dir}:/workspace",
        '-v', f"{tools_dir}:/workspace/tools:ro",
        '-v', f"{shared_repos_dir}:{shared_repos_dir}",
    ]
    if rules_file.exists():
        cmd.extend(['-v', f"{rules_file}:/workspace/MAINTAINER_RULES.md:ro"])

    # Mount the host's `gh` CLI binary read-only on Linux (without mounting ~/.config/gh)
    # so `gh` commands inside the container work using ROS_CONTAINER_GITHUB_TOKEN.
    if sys.platform.startswith('linux'):
        gh_bin = shutil.which('gh')
        if gh_bin and Path(gh_bin).is_file():
            cmd.extend(['-v', f"{Path(gh_bin).resolve()}:/usr/local/bin/gh:ro"])

    container_env = {
        'ROS_DISTRO': distro,
        'ROS_MAINTAINER_SESSION_ID': session_id,
        'ROS_MAINTAINER_GATEWAY_URL': gateway_url or 'http://host.docker.internal:8765',
        'PYTHONUNBUFFERED': '1',
    }
    for k, v in container_env.items():
        cmd.extend(['-e', f"{k}={v}"])

    # Pass GITHUB_TOKEN / GH_TOKEN via environment inheritance (`-e KEY` without `=VALUE`)
    # so the secret token value is never exposed in process arguments (`ps aux`).
    run_env = os.environ.copy()
    token = get_container_github_token(workspace_root)
    if token and token.lower() != 'none':
        run_env['GITHUB_TOKEN'] = token
        run_env['GH_TOKEN'] = token
        cmd.extend(['-e', 'GITHUB_TOKEN', '-e', 'GH_TOKEN'])
    else:
        run_env.pop('GITHUB_TOKEN', None)
        run_env.pop('GH_TOKEN', None)

    cmd.extend([image, 'sleep', 'infinity'])

    res = subprocess.run(cmd, env=run_env, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        return {
            'success': False,
            'status': 'failed',
            'container_name': container_name,
            'runtime': rt,
            'error': res.stderr.strip() or res.stdout.strip(),
        }

    # Run post-create setup inside the container:
    # 1. Source ROS setup.bash in ~/.bashrc
    # 2. Configure git safe.directory '*' for bind-mounted worktrees
    # 3. Symlink host session_dir to /workspace so both host and container paths resolve
    setup_cmd = (
        "source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null || true; "
        "grep -q '/opt/ros/' ~/.bashrc 2>/dev/null || "
        "echo 'source /opt/ros/$ROS_DISTRO/setup.bash' >> ~/.bashrc; "
        "git config --global --add safe.directory '*' 2>/dev/null || true; "
        f"mkdir -p {shlex.quote(str(session_dir.parent))} && "
        f"ln -sfn /workspace {shlex.quote(str(session_dir))}; "
        "(pip install -r /workspace/tools/requirements.txt 2>/dev/null || true)"
    )
    subprocess.run(
        [rt, 'exec', container_name, 'bash', '-c', setup_cmd],
        capture_output=True,
        text=True,
        timeout=120,
    )

    return {
        'success': True,
        'status': 'started',
        'container_name': container_name,
        'runtime': rt,
        'image': image,
        'distro': distro,
    }


def exec_in_session_container(
    session_id: str,
    command: str,
    workdir: str = '/workspace',
    timeout: int = 600,
    runtime: Optional[str] = None,
    auto_start: bool = True,
    session_dir: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
    distro: str = 'rolling',
) -> Dict[str, Any]:
    """
    Execute a shell command inside the session container with the ROS environment sourced.
    """
    rt = runtime or detect_container_runtime()
    if not rt:
        return {
            'success': False,
            'returncode': 127,
            'stdout': '',
            'stderr': 'No container runtime (docker or podman) found on PATH.',
        }

    status_info = get_container_status(session_id, runtime=rt)
    container_name = status_info['container_name']

    if not status_info['running']:
        if auto_start and session_dir is not None and workspace_root is not None:
            start_res = start_session_container(
                session_id=session_id,
                session_dir=session_dir,
                workspace_root=workspace_root,
                distro=distro,
                runtime=rt,
            )
            if not start_res.get('success'):
                return {
                    'success': False,
                    'returncode': 1,
                    'stdout': '',
                    'stderr': f"Failed to auto-start container '{container_name}': {start_res.get('error')}",
                }
        else:
            return {
                'success': False,
                'returncode': 1,
                'stdout': '',
                'stderr': (
                    f"Container '{container_name}' is not running. "
                    f"Start it first with `ros-maintainer-harness session up {session_id}`."
                ),
            }

    wrapped_cmd = (
        "source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null || true; "
        "if [ -f /workspace/install/setup.bash ]; then "
        "source /workspace/install/setup.bash 2>/dev/null || true; fi; "
        "export PATH=\"/workspace/tools/bin:/root/.local/bin:$PATH\"; "
        f"{command}"
    )

    try:
        res = subprocess.run(
            [rt, 'exec', '-w', workdir, container_name, 'bash', '-c', wrapped_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            'success': res.returncode == 0,
            'returncode': res.returncode,
            'stdout': res.stdout,
            'stderr': res.stderr,
            'container_name': container_name,
        }
    except subprocess.TimeoutExpired as e:
        return {
            'success': False,
            'returncode': 124,
            'stdout': (e.stdout or '') if isinstance(e.stdout, str) else '',
            'stderr': f"Command timed out after {timeout} seconds.",
            'container_name': container_name,
        }


def stop_session_container(session_id: str, runtime: Optional[str] = None) -> Dict[str, Any]:
    """Stop and remove the sandbox container for a session."""
    rt = runtime or detect_container_runtime()
    container_name = get_container_name(session_id)
    if not rt:
        return {
            'success': False,
            'container_name': container_name,
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    res = subprocess.run([rt, 'rm', '-f', container_name], capture_output=True, text=True)
    return {
        'success': res.returncode == 0,
        'container_name': container_name,
        'runtime': rt,
        'output': res.stdout.strip() or res.stderr.strip(),
    }
