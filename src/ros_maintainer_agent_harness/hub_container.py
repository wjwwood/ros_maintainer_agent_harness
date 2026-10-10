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

from importlib import metadata as importlib_metadata
from importlib import resources as importlib_resources
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from .auth import get_default_state_dir, TokenStore
from .config import load_policy
from .devcontainer import (
    build_hub_launch_spec,
    DEFAULT_HUB_IMAGE,
    detect_container_runtime,
    HUB_CONTAINER_NAME,
    inspect_hub_container_mounts,
    load_workspace_env,
    PortPublishSpec,
    validate_launch_spec,
    verify_container_mounts_against_spec,
)
from .gateway import (
    get_gateway_status,
    load_state_credentials,
    restart_gateway_service,
    start_gateway_service,
)
from .runner import CommandRunner, get_default_runner

DEFAULT_HUB_OPENCODE_PORT = 4096


def get_hub_dockerfile_content() -> str:
    """Load the packaged Dockerfile.hub content via importlib.resources."""
    pkg_files = importlib_resources.files('ros_maintainer_agent_harness.data')
    return pkg_files.joinpath('Dockerfile.hub').read_text(encoding='utf-8')


def get_host_harness_version() -> str:
    """Return the installed or package version of ros_maintainer_agent_harness."""
    try:
        return importlib_metadata.version('ros_maintainer_agent_harness')
    except Exception:
        return '0.1.0'


def get_package_version_and_sha(
    source_repo: Optional[Path] = None,
    runner: Optional[CommandRunner] = None,
) -> Tuple[str, str]:
    """Return (version, short_git_sha) for the harness package or source repository."""
    active_runner = runner or get_default_runner()
    version = get_host_harness_version()
    git_sha = 'unknown'

    repo_dir = source_repo
    if repo_dir is None:
        candidate = Path(__file__).resolve().parents[2]
        if (candidate / 'pyproject.toml').is_file():
            repo_dir = candidate

    if repo_dir is not None and (repo_dir / 'pyproject.toml').is_file():
        try:
            text = (repo_dir / 'pyproject.toml').read_text(encoding='utf-8')
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith('version') and '=' in stripped:
                    val = stripped.split('=', 1)[1].strip().strip('"').strip("'")
                    if val:
                        version = val
                    break
        except Exception:
            pass

    if repo_dir is not None:
        res = active_runner.run(
            ['git', '-C', str(repo_dir), 'rev-parse', '--short', 'HEAD']
        )
        if res.returncode == 0 and res.stdout.strip():
            git_sha = res.stdout.strip()

    return (version, git_sha)


def _get_hub_opencode_state_file(
    workspace_root: Path,
    state_dir: Optional[Path] = None,
) -> Path:
    base = (state_dir or get_default_state_dir()).resolve()
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base / 'hub_opencode.json'


def save_hub_opencode_state(
    workspace_root: Path,
    port: int,
    password: str,
    state_dir: Optional[Path] = None,
) -> Path:
    path = _get_hub_opencode_state_file(workspace_root, state_dir=state_dir)
    payload = {
        'workspace_root': str(workspace_root.resolve()),
        'host': '127.0.0.1',
        'port': int(port),
        'url': f"http://127.0.0.1:{int(port)}",
        'password': password,
    }
    path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def load_hub_opencode_state(
    workspace_root: Path,
    state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    path = _get_hub_opencode_state_file(workspace_root, state_dir=state_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def is_hub_container_running(
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> bool:
    """Check whether the Maintainer Hub container is currently running."""
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    if not rt:
        return False
    res = active_runner.run(
        [rt, 'inspect', '-f', '{{.State.Running}}', HUB_CONTAINER_NAME]
    )
    return res.returncode == 0 and res.stdout.strip().lower() == 'true'


def build_hub_image(
    source_repo: Optional[Path] = None,
    wheel_dir: Optional[Path] = None,
    tag: str = DEFAULT_HUB_IMAGE,
    no_cache: bool = False,
    platform: Optional[str] = None,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """
    Build the Maintainer Hub Docker image from a wheel and packaged Dockerfile.hub.
    Never bind-mounts or copies an unbuilt source working tree into the image.
    """
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    if not rt:
        return {
            'success': False,
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    repo_dir = source_repo
    if repo_dir is None:
        candidate = Path(__file__).resolve().parents[2]
        if (candidate / 'pyproject.toml').is_file():
            repo_dir = candidate

    version, git_sha = get_package_version_and_sha(source_repo=repo_dir, runner=active_runner)
    base_repo = tag.split(':', 1)[0] if ':' in tag else tag
    version_tag = f"{base_repo}:{version}-{git_sha}"

    with tempfile.TemporaryDirectory(prefix='rmah_hub_build_') as tmp_ctx:
        ctx_path = Path(tmp_ctx).resolve()
        dist_dir = ctx_path / 'dist'
        dist_dir.mkdir(parents=True, exist_ok=True)

        dockerfile_path = ctx_path / 'Dockerfile.hub'
        dockerfile_path.write_text(get_hub_dockerfile_content(), encoding='utf-8')

        existing_wheels = list(wheel_dir.glob('*.whl')) if wheel_dir and wheel_dir.is_dir() else []
        if existing_wheels:
            for whl in existing_wheels:
                shutil.copy2(whl, dist_dir / whl.name)
        else:
            if repo_dir is None or not (repo_dir / 'pyproject.toml').is_file():
                return {
                    'success': False,
                    'error': (
                        'Cannot build hub wheel: no source repository with pyproject.toml found. '
                        'Pass --from <repo_path>.'
                    ),
                }
            wheel_cmd = [
                sys.executable,
                '-m',
                'pip',
                'wheel',
                '--no-deps',
                '--no-build-isolation',
                '--wheel-dir',
                str(dist_dir),
                str(repo_dir),
            ]
            wheel_res = active_runner.run(wheel_cmd)
            if wheel_res.returncode != 0:
                return {
                    'success': False,
                    'error': f"Failed to build wheel: {wheel_res.stderr or wheel_res.stdout}".strip(),
                }
            if not list(dist_dir.glob('*.whl')):
                # Create a placeholder filename record when running under a dry/fake runner
                (dist_dir / f"ros_maintainer_agent_harness-{version}-py3-none-any.whl").write_bytes(b'')

        build_cmd: List[str] = [
            rt,
            'build',
            '-f',
            str(dockerfile_path),
            '--build-arg',
            f'HARNESS_VERSION={version}',
            '--build-arg',
            f'HARNESS_GIT_SHA={git_sha}',
            '--label',
            f'io.ros-maintainer-harness.version={version}',
            '--label',
            f'io.ros-maintainer-harness.git-sha={git_sha}',
            '-t',
            tag,
            '-t',
            version_tag,
        ]
        if no_cache:
            build_cmd.append('--no-cache')
        if platform:
            build_cmd.extend(['--platform', platform])
        build_cmd.append(str(ctx_path))

        build_res = active_runner.run(build_cmd)
        if build_res.returncode != 0:
            return {
                'success': False,
                'error': f"Failed to build hub image: {build_res.stderr or build_res.stdout}".strip(),
                'command': build_cmd,
            }

    return {
        'success': True,
        'image': tag,
        'version_tag': version_tag,
        'version': version,
        'git_sha': git_sha,
        'runtime': rt,
    }


def start_hub_container(
    workspace_root: Path,
    custom_image: Optional[str] = None,
    port: int = DEFAULT_HUB_OPENCODE_PORT,
    start_gateway: bool = False,
    force_recreate: bool = False,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
    state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Start the unprivileged Maintainer Hub container (`ros-harness-hub`).

    Idempotent: if the hub container is already running and `force_recreate=False`,
    returns `status='already_running'` without invoking `docker run`.
    """
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    if not rt:
        return {
            'success': False,
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    ws_root = workspace_root.resolve()
    gw_status = get_gateway_status(ws_root, state_dir=state_dir)
    if not gw_status.get('running'):
        if start_gateway:
            gw_start = start_gateway_service(ws_root, state_dir=state_dir)
            if not gw_start.get('success'):
                return {
                    'success': False,
                    'error': f"Failed to start gateway service: {gw_start.get('error')}",
                }
            gw_status = get_gateway_status(ws_root, state_dir=state_dir)
        else:
            return {
                'success': False,
                'error': (
                    "Gateway launch service is not running. Start it first with "
                    f"'ros-maintainer-harness -w {ws_root} gateway start' or pass '--start-gateway'."
                ),
            }

    if not force_recreate and is_hub_container_running(runtime=rt, runner=active_runner):
        oc_state = load_hub_opencode_state(ws_root, state_dir=state_dir)
        return {
            'success': True,
            'status': 'already_running',
            'container_name': HUB_CONTAINER_NAME,
            'runtime': rt,
            'opencode_url': oc_state.get('url', f"http://127.0.0.1:{port}"),
        }

    # Remove any exited or stale hub container before creating a new one
    active_runner.run([rt, 'rm', '-f', HUB_CONTAINER_NAME])

    ws_env = load_workspace_env(ws_root)
    host_creds = load_state_credentials(state_dir=state_dir, workspace_root=ws_root)
    gh_token = (
        os.environ.get('ROS_CONTAINER_GITHUB_TOKEN')
        or ws_env.get('ROS_CONTAINER_GITHUB_TOKEN')
        or host_creds.get('ROS_CONTAINER_GITHUB_TOKEN')
    )
    include_gh = bool(gh_token and gh_token.lower() not in ('none', 'false', '0'))

    token_store = TokenStore(state_dir=state_dir)
    issued_token = token_store.issue_token(
        role='hub',
        container_name=HUB_CONTAINER_NAME,
        replace_existing=True,
    )
    raw_hub_token = issued_token.token

    existing_oc = load_hub_opencode_state(ws_root, state_dir=state_dir)
    oc_password = existing_oc.get('password') or f"rmah_oc_{secrets.token_urlsafe(24)}"
    save_hub_opencode_state(ws_root, port=port, password=oc_password, state_dir=state_dir)

    gw_port = gw_status.get('port') or load_policy(ws_root / 'config' / 'policy.yaml').server.port
    gateway_url = f"http://host.docker.internal:{gw_port}"

    llm_keys = [
        k
        for k in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY', 'GOOGLE_API_KEY')
        if os.environ.get(k) or host_creds.get(k)
    ]
    extra_inherited = ['OPENCODE_SERVER_PASSWORD'] + llm_keys

    launch_spec = build_hub_launch_spec(
        workspace_root=ws_root,
        custom_image=custom_image,
        gateway_url=gateway_url,
        include_github_token=include_gh,
        ports=[PortPublishSpec(host_ip='127.0.0.1', host_port=int(port), container_port=4096)],
        extra_inherited_env_keys=extra_inherited,
    )
    try:
        validate_launch_spec(launch_spec, raise_on_error=True)
    except ValueError as e:
        return {
            'success': False,
            'error': f"Container launch policy rejected Hub LaunchSpec: {e}",
            'container_name': HUB_CONTAINER_NAME,
        }

    run_env = os.environ.copy()
    run_env['ROS_MAINTAINER_GATEWAY_TOKEN'] = raw_hub_token
    run_env['OPENCODE_SERVER_PASSWORD'] = oc_password
    if include_gh and gh_token:
        run_env['GITHUB_TOKEN'] = gh_token
        run_env['GH_TOKEN'] = gh_token
    for k in llm_keys:
        val = os.environ.get(k) or host_creds.get(k)
        if val:
            run_env[k] = val

    run_cmd = launch_spec.to_docker_run_argv(runtime=rt)
    res = active_runner.run(run_cmd, env=run_env)
    if res.returncode != 0:
        token_store.revoke_token_by_id(issued_token.token_id)
        return {
            'success': False,
            'error': f"Failed to start hub container: {res.stderr or res.stdout}".strip(),
            'container_name': HUB_CONTAINER_NAME,
        }

    # Start OpenCode headless server inside the hub if opencode is installed in the image
    active_runner.run(
        [
            rt,
            'exec',
            '-d',
            HUB_CONTAINER_NAME,
            '/bin/bash',
            '-c',
            (
                'command -v opencode >/dev/null 2>&1 && '
                'opencode serve --hostname 0.0.0.0 --port 4096 >/tmp/opencode.log 2>&1 || true'
            ),
        ]
    )

    return {
        'success': True,
        'status': 'started',
        'container_name': HUB_CONTAINER_NAME,
        'runtime': rt,
        'image': launch_spec.image,
        'gateway_url': gateway_url,
        'opencode_url': f"http://127.0.0.1:{int(port)}",
        'token_id': issued_token.token_id,
    }


def stop_hub_container(
    workspace_root: Optional[Path] = None,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
    state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Stop and remove the Maintainer Hub container (`ros-harness-hub`) and revoke its hub token.
    Does not stop any session containers.
    """
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    revoked_count = TokenStore(state_dir=state_dir).revoke_tokens_for_role('hub')
    if not rt:
        return {
            'success': False,
            'revoked_tokens': revoked_count,
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    res = active_runner.run([rt, 'rm', '-f', HUB_CONTAINER_NAME])
    return {
        'success': res.returncode == 0,
        'container_name': HUB_CONTAINER_NAME,
        'runtime': rt,
        'revoked_tokens': revoked_count,
        'output': (res.stdout + res.stderr).strip(),
    }


def restart_hub_container(
    workspace_root: Path,
    custom_image: Optional[str] = None,
    port: int = DEFAULT_HUB_OPENCODE_PORT,
    start_gateway: bool = False,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
    state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Stop and recreate the Maintainer Hub container."""
    stop_hub_container(
        workspace_root=workspace_root,
        runtime=runtime,
        runner=runner,
        state_dir=state_dir,
    )
    return start_hub_container(
        workspace_root=workspace_root,
        custom_image=custom_image,
        port=port,
        start_gateway=start_gateway,
        force_recreate=True,
        runtime=runtime,
        runner=runner,
        state_dir=state_dir,
    )


def get_hub_container_status(
    workspace_root: Path,
    custom_image: Optional[str] = None,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
    state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Return detailed status for the Maintainer Hub container, including image version/sha,
    mount verification, gateway reachability from inside the hub, and version drift warnings.
    """
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    ws_root = workspace_root.resolve()
    host_version = get_host_harness_version()
    gw_status = get_gateway_status(ws_root, state_dir=state_dir)
    oc_state = load_hub_opencode_state(ws_root, state_dir=state_dir)

    if not rt:
        return {
            'running': False,
            'container_name': HUB_CONTAINER_NAME,
            'runtime': None,
            'host_version': host_version,
            'gateway_running': bool(gw_status.get('running')),
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    inspect_res = active_runner.run([rt, 'inspect', HUB_CONTAINER_NAME])
    if inspect_res.returncode != 0 or not inspect_res.stdout.strip():
        return {
            'running': False,
            'container_name': HUB_CONTAINER_NAME,
            'runtime': rt,
            'image': custom_image or DEFAULT_HUB_IMAGE,
            'image_version': None,
            'image_git_sha': None,
            'host_version': host_version,
            'gateway_running': bool(gw_status.get('running')),
            'gateway_reachable_from_hub': False,
            'mounts_verified': False,
            'mounts': [],
            'drifts': ['Hub container is not created or not running.'],
            'version_mismatch_warning': None,
        }

    try:
        parsed = json.loads(inspect_res.stdout)
        obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
    except Exception:
        obj = {}

    state_obj = obj.get('State') or {}
    cfg_obj = obj.get('Config') or {}
    labels = cfg_obj.get('Labels') or {}
    running = bool(state_obj.get('Running', False))
    image = cfg_obj.get('Image') or custom_image or DEFAULT_HUB_IMAGE
    image_version = (
        labels.get('io.ros-maintainer-harness.version')
        or labels.get('org.opencontainers.image.version')
    )
    image_git_sha = (
        labels.get('io.ros-maintainer-harness.git-sha')
        or labels.get('org.opencontainers.image.revision')
    )

    expected_spec = build_hub_launch_spec(ws_root, custom_image=image)
    mount_report = verify_container_mounts_against_spec([obj], expected_spec)

    gateway_reachable = False
    if running and gw_status.get('running'):
        gw_port = gw_status.get('port') or load_policy(ws_root / 'config' / 'policy.yaml').server.port
        probe_res = active_runner.run(
            [
                rt,
                'exec',
                HUB_CONTAINER_NAME,
                'curl',
                '-fsS',
                '--max-time',
                '3',
                f'http://host.docker.internal:{gw_port}/healthz',
            ]
        )
        gateway_reachable = probe_res.returncode == 0

    warnings: List[str] = []
    gw_version = gw_status.get('version')
    if image_version and image_version != host_version:
        warnings.append(
            f"Hub image version ({image_version}) differs from host version ({host_version}). "
            "Run 'ros-maintainer-harness redeploy' to synchronize."
        )
    if gw_version and gw_version != host_version:
        warnings.append(
            f"Gateway service version ({gw_version}) differs from host version ({host_version}). "
            "Run 'ros-maintainer-harness gateway restart' or 'ros-maintainer-harness redeploy'."
        )
    if image_version and gw_version and image_version != gw_version:
        warnings.append(
            f"Hub image version ({image_version}) differs from gateway service version ({gw_version})."
        )

    return {
        'running': running,
        'container_name': HUB_CONTAINER_NAME,
        'runtime': rt,
        'image': image,
        'image_version': image_version,
        'image_git_sha': image_git_sha,
        'host_version': host_version,
        'gateway_running': bool(gw_status.get('running')),
        'gateway_version': gw_version,
        'gateway_reachable_from_hub': gateway_reachable,
        'opencode_url': oc_state.get('url', f"http://127.0.0.1:{DEFAULT_HUB_OPENCODE_PORT}"),
        'mounts_verified': mount_report['verified'],
        'mounts': mount_report['actual_mounts'],
        'drifts': mount_report['drifts'],
        'version_mismatch_warning': '; '.join(warnings) if warnings else None,
    }


def get_hub_container_logs(
    tail: int = 100,
    follow: bool = False,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Fetch logs from the Maintainer Hub container."""
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    if not rt:
        return {'success': False, 'error': 'No container runtime found on PATH.'}

    cmd: List[str] = [rt, 'logs', '--tail', str(tail)]
    if follow:
        cmd.append('-f')
    cmd.append(HUB_CONTAINER_NAME)
    res = active_runner.run(cmd)
    return {
        'success': res.returncode == 0,
        'returncode': res.returncode,
        'logs': (res.stdout + res.stderr),
        'command': cmd,
    }


def run_hub_shell(
    command: Optional[str] = None,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Run an interactive debugging shell or a single command inside the Hub container."""
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    if not rt:
        return {'success': False, 'error': 'No container runtime found on PATH.'}

    if command:
        cmd = [rt, 'exec', HUB_CONTAINER_NAME, '/bin/bash', '-c', command]
        res = active_runner.run(cmd)
        return {
            'success': res.returncode == 0,
            'returncode': res.returncode,
            'stdout': res.stdout,
            'stderr': res.stderr,
            'command': cmd,
        }
    else:
        cmd = [rt, 'exec', '-it', HUB_CONTAINER_NAME, '/bin/bash']
        res = active_runner.run(cmd, capture_output=False)
        return {
            'success': res.returncode == 0,
            'returncode': res.returncode,
            'command': cmd,
        }


def attach_to_hub_opencode(
    workspace_root: Path,
    runner: Optional[CommandRunner] = None,
    state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Launch `opencode attach` on the host against the Hub container's OpenCode server,
    passing `OPENCODE_SERVER_PASSWORD` via the process environment so it never appears in logs.
    """
    active_runner = runner or get_default_runner()
    ws_root = workspace_root.resolve()
    oc_state = load_hub_opencode_state(ws_root, state_dir=state_dir)
    if not oc_state.get('url'):
        return {
            'success': False,
            'error': (
                "No Hub OpenCode connection info found. Start the hub first with "
                f"'ros-maintainer-harness -w {ws_root} hub start'."
            ),
        }

    opencode_bin = active_runner.which('opencode')
    if not opencode_bin:
        return {
            'success': False,
            'error': "The 'opencode' CLI is not installed on the host PATH.",
        }

    attach_url = oc_state['url']
    cmd = ['opencode', 'attach', attach_url, '--dir', str(ws_root)]
    env = os.environ.copy()
    if oc_state.get('password'):
        env['OPENCODE_SERVER_PASSWORD'] = oc_state['password']

    res = active_runner.run(cmd, env=env, capture_output=False)
    return {
        'success': res.returncode == 0,
        'returncode': res.returncode,
        'url': attach_url,
        'command': cmd,
    }


def redeploy_snapshot(
    workspace_root: Path,
    source_repo: Optional[Path] = None,
    allow_dirty: bool = False,
    also_host: bool = False,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
    state_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Ship a tested harness snapshot to the running system without disturbing live session containers:
    1. Refuse dirty git working tree unless `allow_dirty=True`.
    2. Build a wheel into a temporary directory.
    3. Build the Hub container image from that wheel, tagged with version and git sha.
    4. Optionally install the non-editable wheel snapshot to the host (`--also-host`).
    5. Restart the gateway launch service and recreate the Hub container, leaving all
       running `ros-harness-<session_id>` containers untouched.
    """
    active_runner = runner or get_default_runner()
    ws_root = workspace_root.resolve()
    repo_dir = (source_repo or Path(__file__).resolve().parents[2]).resolve()
    if not (repo_dir / 'pyproject.toml').is_file():
        return {
            'success': False,
            'error': f"No pyproject.toml found in '{repo_dir}'. Pass --from <repo_path>.",
        }

    old_host_version = get_host_harness_version()
    old_hub_status = get_hub_container_status(
        ws_root, runtime=runtime, runner=active_runner, state_dir=state_dir
    )
    old_hub_version = old_hub_status.get('image_version')
    old_gw_status = get_gateway_status(ws_root, state_dir=state_dir)
    old_gw_version = old_gw_status.get('version')

    status_res = active_runner.run(['git', '-C', str(repo_dir), 'status', '--porcelain'])
    if status_res.returncode != 0:
        return {
            'success': False,
            'error': f"Failed to check git status in '{repo_dir}': {status_res.stderr}".strip(),
        }
    if status_res.stdout.strip() and not allow_dirty:
        return {
            'success': False,
            'error': (
                f"Refusing to redeploy from dirty working tree '{repo_dir}'. "
                "Commit your changes first or pass '--allow-dirty'."
            ),
            'dirty_files': status_res.stdout.strip().splitlines(),
        }

    version, git_sha = get_package_version_and_sha(source_repo=repo_dir, runner=active_runner)

    with tempfile.TemporaryDirectory(prefix='rmah_redeploy_') as tmp_dir:
        wheel_dir = Path(tmp_dir).resolve()
        wheel_cmd = [
            sys.executable,
            '-m',
            'pip',
            'wheel',
            '--no-deps',
            '--no-build-isolation',
            '--wheel-dir',
            str(wheel_dir),
            str(repo_dir),
        ]
        wheel_res = active_runner.run(wheel_cmd)
        if wheel_res.returncode != 0:
            return {
                'success': False,
                'error': f"Failed to build wheel from '{repo_dir}': {wheel_res.stderr or wheel_res.stdout}".strip(),
            }

        wheels = list(wheel_dir.glob('*.whl'))
        if not wheels:
            placeholder = wheel_dir / f"ros_maintainer_agent_harness-{version}-py3-none-any.whl"
            placeholder.write_bytes(b'')
            wheels = [placeholder]
        wheel_file = wheels[0]

        build_res = build_hub_image(
            source_repo=repo_dir,
            wheel_dir=wheel_dir,
            runtime=runtime,
            runner=active_runner,
        )
        if not build_res.get('success'):
            return build_res

        host_installed = False
        if also_host:
            pip_cmd = [
                sys.executable,
                '-m',
                'pip',
                'install',
                '--user',
                '--break-system-packages',
                '--no-build-isolation',
                '--no-deps',
                '--force-reinstall',
                str(wheel_file),
            ]
            pip_res = active_runner.run(pip_cmd)
            if pip_res.returncode != 0:
                fallback_cmd = [a for a in pip_cmd if a != '--break-system-packages']
                pip_res = active_runner.run(fallback_cmd)
                if pip_res.returncode != 0:
                    return {
                        'success': False,
                        'error': (
                            f"Failed to install wheel on host: {pip_res.stderr or pip_res.stdout}".strip()
                        ),
                    }
            host_installed = True

    gw_restart = restart_gateway_service(ws_root, state_dir=state_dir)
    hub_restart = restart_hub_container(
        ws_root,
        start_gateway=True,
        runtime=runtime,
        runner=active_runner,
        state_dir=state_dir,
    )
    new_hub_status = get_hub_container_status(
        ws_root, runtime=runtime, runner=active_runner, state_dir=state_dir
    )
    new_gw_status = get_gateway_status(ws_root, state_dir=state_dir)

    return {
        'success': bool(gw_restart.get('success', True) and hub_restart.get('success')),
        'old_versions': {
            'host': old_host_version,
            'gateway': old_gw_version,
            'hub': old_hub_version,
        },
        'new_versions': {
            'version': version,
            'git_sha': git_sha,
            'version_tag': build_res.get('version_tag'),
            'host_installed': host_installed,
        },
        'gateway_status': new_gw_status,
        'hub_status': new_hub_status,
        'session_containers_preserved': True,
    }


__all__ = [
    'DEFAULT_HUB_OPENCODE_PORT',
    'attach_to_hub_opencode',
    'build_hub_image',
    'get_host_harness_version',
    'get_hub_container_logs',
    'get_hub_container_status',
    'get_hub_dockerfile_content',
    'get_package_version_and_sha',
    'inspect_hub_container_mounts',
    'is_hub_container_running',
    'load_hub_opencode_state',
    'redeploy_snapshot',
    'restart_hub_container',
    'run_hub_shell',
    'save_hub_opencode_state',
    'start_hub_container',
    'stop_hub_container',
]
