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

from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as pkg_version
import json
import os
from pathlib import Path
import plistlib
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

from .auth import ensure_private_dir, get_default_state_dir, TokenStore
from .runner import CommandRunner, get_default_runner

CREDENTIAL_ENV_KEYS = frozenset({
    'ROS_HOST_GITHUB_TOKEN',
    'ROS_CONTAINER_GITHUB_TOKEN',
    'GITHUB_TOKEN',
    'GH_TOKEN',
    'GITHUB_ACCESS_TOKEN',
    'ROS_CI_GITHUB_TOKEN',
    'JENKINS_TOKEN',
    'ROS_CI_JENKINS_TOKEN',
    'ANTHROPIC_API_KEY',
    'OPENAI_API_KEY',
    'GEMINI_API_KEY',
    'GOOGLE_GENERATIVE_AI_API_KEY',
    'OPENROUTER_API_KEY',
})

WRITE_CREDENTIAL_ENV_KEYS = frozenset({
    'ROS_HOST_GITHUB_TOKEN',
    'GITHUB_TOKEN',
    'GH_TOKEN',
    'GITHUB_ACCESS_TOKEN',
    'ROS_CI_GITHUB_TOKEN',
    'JENKINS_TOKEN',
    'ROS_CI_JENKINS_TOKEN',
})


def get_harness_version() -> str:
    """Return the installed ros_maintainer_agent_harness package version."""
    try:
        return pkg_version('ros_maintainer_agent_harness')
    except PackageNotFoundError:
        return '0.1.0'


def parse_env_file(env_file: Path) -> Dict[str, str]:
    """Parse a key=value .env file into a dictionary."""
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


def write_private_env_file(env_file: Path, values: Dict[str, str], header: str) -> Path:
    """Write key=value entries to `env_file` with 0600 permissions."""
    ensure_private_dir(env_file.parent)
    lines = [
        f'# {header}',
        '# Do not commit this file to version control.',
    ]
    for k, v in values.items():
        lines.append(f'{k}="{v}"')
    tmp_file = env_file.with_suffix('.env.tmp')
    tmp_file.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    try:
        tmp_file.chmod(0o600)
    except OSError:
        pass
    tmp_file.replace(env_file)
    try:
        env_file.chmod(0o600)
    except OSError:
        pass
    return env_file


def get_credentials_file(
    state_dir: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> Path:
    """Return the host-only credentials file path outside the workspace tree."""
    import hashlib
    sdir = ensure_private_dir((state_dir or get_default_state_dir()).resolve())
    explicit_state = state_dir is not None or bool((os.environ.get('ROS_MAINTAINER_STATE_DIR') or '').strip())
    if workspace_root is not None and not explicit_state:
        ws_hash = hashlib.sha256(str(workspace_root.resolve()).encode('utf-8')).hexdigest()[:12]
        ws_sdir = ensure_private_dir(sdir / 'workspaces' / ws_hash)
        return ws_sdir / 'credentials.env'
    return sdir / 'credentials.env'


def load_state_credentials(
    state_dir: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> Dict[str, str]:
    """Load credentials stored in `<state_dir>/credentials.env`."""
    return parse_env_file(get_credentials_file(state_dir, workspace_root=workspace_root))


def save_state_credential(
    key: str,
    value: str,
    state_dir: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> Path:
    """Save or update a credential in `<state_dir>/credentials.env` with 0600 permissions."""
    cred_file = get_credentials_file(state_dir, workspace_root=workspace_root)
    existing = parse_env_file(cred_file)
    existing[key] = value
    return write_private_env_file(
        cred_file,
        existing,
        'ROS Maintainer Agent Harness Host Credentials (outside workspace)',
    )


def migrate_workspace_credentials(
    workspace_root: Path,
    state_dir: Optional[Path] = None,
) -> List[str]:
    """
    Migrate credentials out of `<workspace_root>/.env` into `<state_dir>/credentials.env`
    so no write credentials remain inside the mounted workspace tree.
    """
    ws_env_file = workspace_root.resolve() / '.env'
    if not ws_env_file.exists():
        return []

    ws_vars = parse_env_file(ws_env_file)
    if not ws_vars:
        return []

    cred_file = get_credentials_file(state_dir, workspace_root=workspace_root)
    state_vars = parse_env_file(cred_file)
    migrated: List[str] = []
    remaining: Dict[str, str] = {}

    for k, v in ws_vars.items():
        if k in CREDENTIAL_ENV_KEYS or k.endswith('_TOKEN') or k.endswith('_API_KEY'):
            if k not in state_vars or not state_vars[k]:
                state_vars[k] = v
            migrated.append(k)
        else:
            remaining[k] = v

    if migrated:
        write_private_env_file(
            cred_file,
            state_vars,
            'ROS Maintainer Agent Harness Host Credentials (outside workspace)',
        )
        if remaining:
            write_private_env_file(
                ws_env_file,
                remaining,
                'ROS Maintainer Agent Harness Workspace Environment',
            )
        else:
            try:
                ws_env_file.unlink()
            except OSError:
                pass

    return migrated


def find_workspace_write_credentials(workspace_root: Path) -> List[str]:
    """Return any write credential keys still present inside `<workspace_root>/.env`."""
    ws_env_file = workspace_root.resolve() / '.env'
    if not ws_env_file.exists():
        return []
    ws_vars = parse_env_file(ws_env_file)
    found: List[str] = []
    for k, v in ws_vars.items():
        if not v or v.lower() == 'none':
            continue
        if k in WRITE_CREDENTIAL_ENV_KEYS:
            found.append(k)
    return found


def validate_gateway_bind_host(host: str, allow_wide_bind: bool = False) -> Tuple[bool, str]:
    """
    Validate the launch service bind address.
    Refuses wide binds (`0.0.0.0`, `::`, `*`, empty) unless `allow_wide_bind=True`.
    """
    cleaned = (host or '').strip()
    if cleaned in ('0.0.0.0', '::', '[::]', '*', ''):
        if not allow_wide_bind:
            return (
                False,
                (
                    f"Refusing to bind launch service to wide address '{cleaned or '0.0.0.0'}'. "
                    "Bind to '127.0.0.1' (or pass --allow-wide-bind to override explicitly)."
                ),
            )
    return (True, '')


def detect_linux_docker_bridge_ip(runner: Optional[CommandRunner] = None) -> Optional[str]:
    """
    On Linux, detect the IPv4 address of `docker0` (for example `172.17.0.1`) so containers
    using `--add-host=host.docker.internal:host-gateway` can reach the host launch service.
    """
    if not sys.platform.startswith('linux'):
        return None
    active_runner = runner or get_default_runner()
    if not active_runner.which('ip'):
        return None
    try:
        res = active_runner.run(['ip', '-4', '-o', 'addr', 'show', 'dev', 'docker0'], timeout=3)
        if res.returncode == 0 and res.stdout:
            m = re.search(r'\binet\s+(\d+\.\d+\.\d+\.\d+)/\d+', res.stdout)
            if m:
                ip = m.group(1)
                if ip != '127.0.0.1' and not ip.startswith('0.'):
                    return ip
    except Exception:
        pass
    return None


class _TCPBridgeForwarder:
    """
    Lightweight loopback forwarder that accepts connections on `(bridge_ip, port)` (e.g. `docker0`)
    and forwards them to `(target_host, port)` (`127.0.0.1`) so Linux containers can reach the
    gateway at `host.docker.internal:<port>` without binding the gateway to `0.0.0.0`.
    """

    def __init__(self, listen_host: str, port: int, target_host: str = '127.0.0.1'):
        self.listen_host = listen_host
        self.port = port
        self.target_host = target_host
        self._stop_event = threading.Event()
        self._server_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if self.listen_host == self.target_host:
            return False
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.listen_host, self.port))
            srv.listen(32)
            srv.settimeout(0.5)
            self._server_sock = srv
        except OSError:
            return False

        self._thread = threading.Thread(
            target=self._accept_loop,
            name=f"rmah-bridge-forwarder-{self.listen_host}:{self.port}",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_sock is not None:
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None

    def _accept_loop(self) -> None:
        while not self._stop_event.is_set() and self._server_sock is not None:
            try:
                client_sock, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._pipe_sockets,
                args=(client_sock,),
                daemon=True,
            )
            t.start()

    def _pipe_sockets(self, client_sock: socket.socket) -> None:
        upstream_sock = None
        try:
            upstream_sock = socket.create_connection((self.target_host, self.port), timeout=5.0)
            sockets = [client_sock, upstream_sock]
            while not self._stop_event.is_set():
                readable, _, _ = select.select(sockets, [], [], 1.0)
                if not readable:
                    continue
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    other = upstream_sock if s is client_sock else client_sock
                    other.sendall(data)
        except OSError:
            pass
        finally:
            try:
                client_sock.close()
            except OSError:
                pass
            if upstream_sock is not None:
                try:
                    upstream_sock.close()
                except OSError:
                    pass


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with `pid` exists and is alive."""
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        process_query_limited = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == still_active
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _gateway_paths(state_dir: Optional[Path] = None) -> Tuple[Path, Path, Path, Path]:
    sdir = ensure_private_dir((state_dir or get_default_state_dir()).resolve())
    return (
        sdir,
        sdir / 'gateway.pid',
        sdir / 'gateway.json',
        sdir / 'gateway.log',
    )


def get_gateway_status(
    workspace_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Inspect the managed launch service status (`gateway status [--json]`).
    Cleans up stale pidfiles automatically if the recorded PID is no longer alive.
    """
    ws_root = (workspace_root or Path.cwd()).resolve()
    policy_path = ws_root / 'config' / 'policy.yaml'
    sdir, pid_file, meta_file, log_file = _gateway_paths(state_dir)
    token_store = TokenStore(sdir)
    active_tokens = token_store.list_active_tokens()

    meta: Dict[str, Any] = {}
    if meta_file.exists():
        try:
            loaded = json.loads(meta_file.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            pass

    pid: Optional[int] = None
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding='utf-8').strip())
        except Exception:
            pid = None
    elif isinstance(meta.get('pid'), int):
        pid = int(meta['pid'])

    stale_pid_cleaned = False
    running = False
    if pid is not None:
        if is_pid_alive(pid):
            running = True
        else:
            stale_pid_cleaned = True
            pid = None
            try:
                pid_file.unlink()
            except OSError:
                pass
            try:
                meta_file.unlink()
            except OSError:
                pass

    bind_address = str(meta.get('bind_address') or host or '127.0.0.1')
    bridge_address = meta.get('bridge_address')
    resolved_port = int(meta.get('port') or port or 8765)
    transport = str(meta.get('transport') or 'streamable-http')
    version = str(meta.get('version') or get_harness_version())
    healthy = check_gateway_health(bind_address, resolved_port, timeout=0.5) if running else False

    return {
        'running': running,
        'healthy': healthy,
        'status': 'running' if running else 'stopped',
        'pid': pid,
        'bind_address': bind_address,
        'bridge_address': bridge_address,
        'port': resolved_port,
        'url': f"http://{bind_address}:{resolved_port}/mcp",
        'transport': transport,
        'version': version,
        'workspace_root': str(meta.get('workspace_root') or ws_root),
        'policy_path': str(meta.get('policy_path') or policy_path),
        'state_dir': str(sdir),
        'pidfile': str(pid_file),
        'log_file': str(log_file),
        'registered_callers': len(active_tokens),
        'active_tokens': active_tokens,
        'stale_pid_cleaned': stale_pid_cleaned,
    }


def check_gateway_health(host: str = '127.0.0.1', port: int = 8765, timeout: float = 1.5) -> bool:
    """Return True if the gateway HTTP health endpoint responds with 200 OK."""
    target = '127.0.0.1' if host in ('0.0.0.0', '::', '[::]') else host
    url = f"http://{target}:{port}/health"
    try:
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status) == 200
    except Exception:
        return False


def start_gateway_service(
    workspace_root: Optional[Path] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    transport: str = 'streamable-http',
    allow_wide_bind: bool = False,
    state_dir: Optional[Path] = None,
    wait_ready: bool = True,
    ready_timeout: float = 10.0,
    spawn_process: bool = True,
    workspace_path: Optional[Path] = None,
    startup_timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Start the host MCP launch service as a managed background process (`gateway start`).
    """
    from .workspace import WorkspaceLayout

    ws_root = (workspace_root or workspace_path or Path.cwd()).resolve()
    if startup_timeout is not None:
        ready_timeout = float(startup_timeout)
    layout = WorkspaceLayout(ws_root)
    policy = layout.get_policy()

    bind_host = (host if host is not None else policy.server.host) or '127.0.0.1'
    bind_port = int(port if port is not None else policy.server.port)

    valid_host, host_err = validate_gateway_bind_host(bind_host, allow_wide_bind=allow_wide_bind)
    if not valid_host:
        return {
            'success': False,
            'status': 'refused_wide_bind',
            'error': host_err,
        }

    sdir, pid_file, meta_file, log_file = _gateway_paths(state_dir)
    migrated = migrate_workspace_credentials(ws_root, state_dir=sdir)

    current = get_gateway_status(ws_root, state_dir=sdir, host=bind_host, port=bind_port)
    if current['running']:
        res = dict(current)
        res['success'] = True
        res['already_running'] = True
        res['status'] = 'already_running'
        res['migrated_credentials'] = migrated
        return res

    # Ensure an admin token exists for host CLI/dev callers
    token_store = TokenStore(sdir)
    active_admin = [t for t in token_store.list_active_tokens() if t.get('role') == 'admin']
    if not active_admin:
        token_store.issue_token(role='admin', container_name='host-cli', replace_existing=True)

    bridge_ip = detect_linux_docker_bridge_ip() if bind_host == '127.0.0.1' else None

    if not spawn_process:
        # Used by unit tests that test pidfile/state management without spawning uvicorn
        return {
            'success': True,
            'already_running': False,
            'status': 'dry_run',
            'bind_address': bind_host,
            'bridge_address': bridge_ip,
            'port': bind_port,
            'url': f"http://{bind_host}:{bind_port}/mcp",
            'transport': transport,
            'state_dir': str(sdir),
            'migrated_credentials': migrated,
        }

    cmd = [
        sys.executable,
        '-m',
        'ros_maintainer_agent_harness.cli',
        '-w',
        str(ws_root),
        'serve',
        '--transport',
        transport,
        '--host',
        bind_host,
        '--port',
        str(bind_port),
        '--require-auth',
    ]
    if allow_wide_bind:
        cmd.append('--allow-wide-bind')

    child_env = os.environ.copy()
    child_env['ROS_MAINTAINER_STATE_DIR'] = str(sdir)
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    existing_pypath = child_env.get('PYTHONPATH', '')
    child_env['PYTHONPATH'] = (
        f"{pkg_parent}{os.pathsep}{existing_pypath}" if existing_pypath else pkg_parent
    )
    for k, v in load_state_credentials(sdir, workspace_root=ws_root).items():
        if v and k not in child_env:
            child_env[k] = v

    log_f = open(log_file, 'a', encoding='utf-8')
    try:
        log_file.chmod(0o600)
    except OSError:
        pass

    popen_kwargs: Dict[str, Any] = {
        'stdout': log_f,
        'stderr': subprocess.STDOUT,
        'stdin': subprocess.DEVNULL,
        'env': child_env,
        'cwd': str(ws_root),
    }
    if hasattr(os, 'setsid'):
        popen_kwargs['start_new_session'] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    log_f.close()

    pid = int(proc.pid)
    now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    pid_file.write_text(f"{pid}\n", encoding='utf-8')
    try:
        pid_file.chmod(0o600)
    except OSError:
        pass

    meta_payload = {
        'pid': pid,
        'bind_address': bind_host,
        'bridge_address': bridge_ip,
        'port': bind_port,
        'transport': transport,
        'version': get_harness_version(),
        'workspace_root': str(ws_root),
        'policy_path': str(ws_root / 'config' / 'policy.yaml'),
        'started_at': now_iso,
    }
    meta_file.write_text(json.dumps(meta_payload, indent=2) + '\n', encoding='utf-8')
    try:
        meta_file.chmod(0o600)
    except OSError:
        pass

    if wait_ready:
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return {
                    'success': False,
                    'already_running': False,
                    'status': 'failed',
                    'pid': pid,
                    'error': f"Gateway process exited early with code {proc.returncode}. Check {log_file}.",
                    'log_file': str(log_file),
                }
            if check_gateway_health(bind_host, bind_port, timeout=0.5):
                break
            time.sleep(0.1)

    status_report = get_gateway_status(ws_root, state_dir=sdir, host=bind_host, port=bind_port)
    status_report['success'] = status_report['running']
    status_report['already_running'] = False
    status_report['status'] = 'started' if status_report['running'] else 'failed'
    status_report['migrated_credentials'] = migrated
    return status_report


def stop_gateway_service(
    workspace_root: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """Stop the managed host launch service (`gateway stop`)."""
    ws_root = (workspace_root or Path.cwd()).resolve()
    sdir, pid_file, meta_file, _ = _gateway_paths(state_dir)
    current = get_gateway_status(ws_root, state_dir=sdir)
    pid = current.get('pid')

    if not current['running'] or pid is None:
        return {
            'success': True,
            'status': 'already_stopped',
            'pid': None,
            'stale_pid_cleaned': current.get('stale_pid_cleaned', False),
        }

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_pid_alive(pid):
            break
        time.sleep(0.05)

    if is_pid_alive(pid):
        sigkill = getattr(signal, 'SIGKILL', signal.SIGTERM)
        try:
            os.kill(pid, sigkill)
        except OSError:
            pass

    for fpath in (pid_file, meta_file):
        try:
            if fpath.exists():
                fpath.unlink()
        except OSError:
            pass

    return {
        'success': True,
        'status': 'stopped',
        'pid': pid,
        'stopped_pid': pid,
    }


def restart_gateway_service(
    workspace_root: Optional[Path] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    transport: str = 'streamable-http',
    allow_wide_bind: bool = False,
    state_dir: Optional[Path] = None,
    wait_ready: bool = True,
    workspace_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Restart the managed host launch service (`gateway restart`)."""
    ws_root = (workspace_root or workspace_path or Path.cwd()).resolve()
    stop_res = stop_gateway_service(ws_root, state_dir=state_dir)
    start_res = start_gateway_service(
        workspace_root=ws_root,
        host=host,
        port=port,
        transport=transport,
        allow_wide_bind=allow_wide_bind,
        state_dir=state_dir,
        wait_ready=wait_ready,
    )
    start_res['previous_pid'] = stop_res.get('stopped_pid')
    return start_res


def read_gateway_logs(
    state_dir: Optional[Path] = None,
    lines: int = 50,
) -> str:
    """Return the last `lines` lines from `<state_dir>/gateway.log`."""
    _, _, _, log_file = _gateway_paths(state_dir)
    if not log_file.exists():
        return ''
    try:
        all_lines = log_file.read_text(encoding='utf-8', errors='replace').splitlines()
        if lines > 0:
            all_lines = all_lines[-lines:]
        return '\n'.join(all_lines)
    except Exception:
        return ''


LAUNCHD_LABEL = 'org.osrf.ros_maintainer_agent_harness.gateway'


def generate_launchd_plist(
    workspace_root: Path,
    host: str = '127.0.0.1',
    port: int = 8765,
    state_dir: Optional[Path] = None,
) -> bytes:
    """Generate a macOS launchd plist XML payload for the host launch service."""
    ws_root = workspace_root.resolve()
    sdir, _, _, log_file = _gateway_paths(state_dir)
    payload = {
        'Label': LAUNCHD_LABEL,
        'ProgramArguments': [
            sys.executable,
            '-m',
            'ros_maintainer_agent_harness.cli',
            '-w',
            str(ws_root),
            'serve',
            '--transport',
            'streamable-http',
            '--host',
            host,
            '--port',
            str(port),
            '--require-auth',
        ],
        'WorkingDirectory': str(ws_root),
        'EnvironmentVariables': {
            'ROS_MAINTAINER_WS': str(ws_root),
            'ROS_MAINTAINER_STATE_DIR': str(sdir),
            'PATH': f"{Path.home() / '.local' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        'RunAtLoad': True,
        'KeepAlive': True,
        'StandardOutPath': str(log_file),
        'StandardErrorPath': str(log_file),
    }
    return plistlib.dumps(payload)


def install_launchd_service(
    workspace_root: Optional[Path] = None,
    host: str = '127.0.0.1',
    port: int = 8765,
    state_dir: Optional[Path] = None,
    output_path: Optional[Path] = None,
    print_only: bool = False,
    workspace_path: Optional[Path] = None,
    plist_path: Optional[Path] = None,
    load: bool = False,
) -> Dict[str, Any]:
    """Write or return the macOS launchd plist for the host launch service."""
    ws_root = (workspace_root or workspace_path or Path.cwd()).resolve()
    plist_bytes = generate_launchd_plist(
        workspace_root=ws_root,
        host=host,
        port=port,
        state_dir=state_dir,
    )
    plist_text = plist_bytes.decode('utf-8')
    chosen_out = output_path or plist_path
    target_path = (
        chosen_out.resolve()
        if chosen_out is not None
        else (Path.home() / 'Library' / 'LaunchAgents' / f'{LAUNCHD_LABEL}.plist')
    )
    if not print_only:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(plist_bytes)
        if load and sys.platform == 'darwin':
            subprocess.run(['launchctl', 'load', '-w', str(target_path)], check=False)

    return {
        'success': True,
        'label': LAUNCHD_LABEL,
        'plist_path': str(target_path),
        'written': not print_only,
        'plist_content': plist_text,
    }
