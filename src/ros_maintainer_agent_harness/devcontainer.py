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

import dataclasses
import fnmatch
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .config import HarnessPolicy, load_policy
from .runner import CommandRunner, get_default_runner

DEFAULT_DISTRO_IMAGES = {
    'rolling': 'docker.io/osrf/ros:rolling-desktop',
    'jazzy': 'docker.io/osrf/ros:jazzy-desktop',
    'iron': 'docker.io/osrf/ros:iron-desktop',
    'humble': 'docker.io/osrf/ros:humble-desktop',
    'kilted': 'docker.io/osrf/ros:kilted-desktop',
    'lyrical': 'docker.io/osrf/ros:lyrical-desktop',
    'noetic': 'docker.io/osrf/ros:noetic-desktop',
}

DEFAULT_HUB_IMAGE = 'ros-maintainer-harness-hub:latest'
HUB_CONTAINER_NAME = 'ros-harness-hub'


@dataclasses.dataclass
class MountSpec:
    source: str
    target: str
    read_only: bool = True

    def to_volume_arg(self) -> str:
        suffix = ':ro' if self.read_only else ''
        return f"{self.source}:{self.target}{suffix}"


@dataclasses.dataclass
class PortPublishSpec:
    host_ip: str = '127.0.0.1'
    host_port: int = 0
    container_port: int = 0

    def to_publish_arg(self) -> str:
        return f"{self.host_ip}:{self.host_port}:{self.container_port}"


@dataclasses.dataclass
class LaunchSpec:
    """
    Structured container launch specification for ``hub`` and ``session`` roles.

    Note on ``seccomp=unconfined`` for session containers:
    ROS 2 C++ unit test suites (GoogleTest death tests, ASAN/LSAN leak detection via ptrace,
    and CycloneDDS/FastDDS shared-memory discovery syscalls) require ``seccomp=unconfined``
    inside Docker containers. It is enabled on session containers when
    ``policy.containers.seccomp_unconfined`` is True, and never enabled on the Hub container.
    """

    role: str  # 'hub' or 'session'
    container_name: str
    image: str
    workdir: str
    workspace_root: str
    session_id: Optional[str] = None
    session_dir: Optional[str] = None
    mounts: List[MountSpec] = dataclasses.field(default_factory=list)
    env: Dict[str, str] = dataclasses.field(default_factory=dict)
    inherited_env_keys: List[str] = dataclasses.field(default_factory=list)
    ports: List[PortPublishSpec] = dataclasses.field(default_factory=list)
    extra_hosts: List[str] = dataclasses.field(
        default_factory=lambda: ['host.docker.internal:host-gateway']
    )
    security_opts: List[str] = dataclasses.field(default_factory=list)
    restart_policy: Optional[str] = None
    cpus: Optional[str] = None
    memory: Optional[str] = None
    pids_limit: Optional[int] = None
    extra_flags: List[str] = dataclasses.field(default_factory=list)
    command: List[str] = dataclasses.field(default_factory=lambda: ['sleep', 'infinity'])

    def to_docker_run_argv(self, runtime: str = 'docker') -> List[str]:
        argv: List[str] = [
            runtime,
            'run',
            '-d',
            '--name',
            self.container_name,
            '--workdir',
            self.workdir,
        ]
        if self.restart_policy:
            argv.extend(['--restart', self.restart_policy])
        for host_map in self.extra_hosts:
            argv.append(f"--add-host={host_map}")
        for sec_opt in self.security_opts:
            argv.append(f"--security-opt={sec_opt}")
        if self.cpus:
            argv.extend(['--cpus', str(self.cpus)])
        if self.memory:
            argv.extend(['--memory', str(self.memory)])
        if self.pids_limit is not None:
            argv.extend(['--pids-limit', str(self.pids_limit)])
        for port_spec in self.ports:
            argv.extend(['-p', port_spec.to_publish_arg()])
        for mount in self.mounts:
            argv.extend(['-v', mount.to_volume_arg()])
        for k, v in self.env.items():
            argv.extend(['-e', f"{k}={v}"])
        for k in self.inherited_env_keys:
            argv.extend(['-e', k])
        if self.extra_flags:
            argv.extend(self.extra_flags)
        argv.append(self.image)
        argv.extend(self.command)
        return argv


def get_image_for_distro(distro: str, custom_image: Optional[str] = None) -> str:
    """Resolve the container image name for a target ROS distro."""
    if custom_image:
        return custom_image
    return DEFAULT_DISTRO_IMAGES.get(distro.lower(), f"docker.io/osrf/ros:{distro.lower()}-desktop")


def _get_allowed_images(policy: Optional[HarnessPolicy] = None, role: Optional[str] = None) -> List[str]:
    allowed: List[str] = []
    if role in (None, 'session'):
        for img in DEFAULT_DISTRO_IMAGES.values():
            allowed.append(img)
            if img.startswith('docker.io/'):
                allowed.append(img[len('docker.io/'):])
    if role in (None, 'hub'):
        allowed.append(DEFAULT_HUB_IMAGE)
        allowed.append('ros-maintainer-harness-hub:*')
    if policy is not None:
        for custom in policy.containers.allowed_images:
            if custom and custom not in allowed:
                allowed.append(custom)
    return allowed


def _is_image_allowed(image: str, policy: Optional[HarnessPolicy] = None, role: Optional[str] = None) -> bool:
    if not image or not image.strip():
        return False
    for pattern in _get_allowed_images(policy=policy, role=role):
        if image == pattern or fnmatch.fnmatch(image, pattern):
            return True
    return False


def _has_dotdot_component(raw_path: str) -> bool:
    if not raw_path:
        return False
    normalized = raw_path.replace('\\', '/')
    parts = [p for p in normalized.split('/') if p]
    return '..' in parts or raw_path.strip() == '..'


def _check_forbidden_mount_path(raw_path: str, resolved_path: Path) -> Optional[str]:
    low_raw = raw_path.replace('\\', '/').lower()
    low_res = resolved_path.as_posix().lower()
    for candidate in (low_raw, low_res):
        if 'docker.sock' in candidate or 'podman.sock' in candidate:
            return f"Mounting the container runtime socket ('{raw_path}') is forbidden."
        if candidate.endswith('/.ssh') or '/.ssh/' in candidate:
            return f"Mounting host SSH directory ('{raw_path}') is forbidden."
        if candidate.endswith('/.config/gh') or '/.config/gh/' in candidate:
            return f"Mounting host GitHub CLI configuration ('{raw_path}') is forbidden."
        if (
            candidate.endswith('/.local/share/opencode')
            or '/.local/share/opencode/' in candidate
            or candidate.endswith('/.config/opencode')
            or '/.config/opencode/' in candidate
        ):
            return f"Mounting host OpenCode auth/config directory ('{raw_path}') is forbidden."
        if candidate.endswith('/bin/gh') or Path(raw_path).name == 'gh':
            return f"Mounting host gh binary ('{raw_path}') is forbidden."
    try:
        home_resolved = Path.home().resolve()
        if resolved_path == home_resolved:
            return f"Mounting host home directory ('{raw_path}') is forbidden."
    except Exception:
        pass
    return None


def validate_launch_spec(
    spec: LaunchSpec,
    policy: Optional[HarnessPolicy] = None,
    raise_on_error: bool = False,
) -> Tuple[bool, List[str]]:
    """
    Validate a ``LaunchSpec`` against the container launch policy before invoking Docker/Podman.

    Returns:
        ``(is_valid, errors)``
    """
    from .worktree import validate_session_id

    errors: List[str] = []

    if spec.role not in ('hub', 'session'):
        errors.append(f"Invalid launch spec role '{spec.role}': must be 'hub' or 'session'.")

    if spec.role == 'session':
        try:
            validate_session_id(spec.session_id or '')
        except ValueError as e:
            errors.append(str(e))

    # 1. Image allowlist check
    if not _is_image_allowed(spec.image, policy=policy, role=spec.role):
        errors.append(
            f"Container image '{spec.image}' is not in the allowed image list for role '{spec.role}'."
        )

    # 2. Forbidden flags & security options check
    idx = 0
    flags = list(spec.extra_flags or [])
    while idx < len(flags):
        flag = flags[idx].strip()
        next_arg = flags[idx + 1].strip() if idx + 1 < len(flags) else ''
        if flag == '--privileged' or flag.startswith('--privileged='):
            errors.append(f"Forbidden container flag '{flag}': privileged containers are prohibited.")
        elif flag in ('--network=host', '--net=host') or (
            flag in ('--network', '--net') and next_arg == 'host'
        ):
            errors.append("Forbidden container flag: host network mode ('--network host') is prohibited.")
        elif flag in ('--pid=host', '--ipc=host', '--uts=host', '--userns=host') or (
            flag in ('--pid', '--ipc', '--uts', '--userns') and next_arg == 'host'
        ):
            errors.append(f"Forbidden container flag: host namespace sharing ('{flag}') is prohibited.")
        elif flag == '--cap-add' or flag.startswith('--cap-add='):
            errors.append(f"Forbidden container flag '{flag}': adding Linux capabilities is prohibited.")
        elif flag == '--device' or flag.startswith('--device='):
            errors.append(f"Forbidden container flag '{flag}': host device passthrough is prohibited.")
        idx += 1

    allow_seccomp = True if policy is None else bool(policy.containers.seccomp_unconfined)
    for sec_opt in spec.security_opts:
        if sec_opt == 'seccomp=unconfined':
            if spec.role != 'session' or not allow_seccomp:
                errors.append(
                    f"Security option '{sec_opt}' is not permitted for role '{spec.role}' under current policy."
                )
        else:
            errors.append(f"Forbidden security option '{sec_opt}'.")

    # 3. Port bindings check (127.0.0.1 only)
    for p in spec.ports:
        if p.host_ip != '127.0.0.1':
            errors.append(
                f"Port binding '{p.to_publish_arg()}' must bind strictly to '127.0.0.1', got '{p.host_ip}'."
            )
        if not (1 <= int(p.host_port) <= 65535 and 1 <= int(p.container_port) <= 65535):
            errors.append(
                f"Port binding '{p.to_publish_arg()}' has invalid port number(s): "
                f"host_port={p.host_port}, container_port={p.container_port}."
            )

    # 4. Mount validation (no '..', no forbidden paths, symlink-resolved inside workspace_root, identical path)
    if not spec.workspace_root or _has_dotdot_component(spec.workspace_root):
        errors.append(f"Invalid workspace_root '{spec.workspace_root}'.")
        ws_root = Path(spec.workspace_root or '.').resolve()
    else:
        ws_root = Path(spec.workspace_root).resolve()

    expected_session_dir: Optional[Path] = None
    if spec.role == 'session' and spec.session_id and not errors:
        expected_session_dir = (ws_root / 'sessions' / spec.session_id).resolve()

    seen_targets: Dict[Path, MountSpec] = {}
    for m in spec.mounts:
        if _has_dotdot_component(m.source) or _has_dotdot_component(m.target):
            errors.append(
                f"Mount '{m.to_volume_arg()}' contains '..' path traversal component."
            )
            continue

        src_path = Path(m.source)
        tgt_path = Path(m.target)
        src_resolved = src_path.resolve()
        tgt_resolved = tgt_path.resolve()

        forbidden_src = _check_forbidden_mount_path(m.source, src_resolved)
        if forbidden_src:
            errors.append(forbidden_src)
            continue
        forbidden_tgt = _check_forbidden_mount_path(m.target, tgt_resolved)
        if forbidden_tgt:
            errors.append(forbidden_tgt)
            continue

        if not src_resolved.is_relative_to(ws_root):
            errors.append(
                f"Mount source '{m.source}' resolves to '{src_resolved}', which is outside "
                f"workspace root '{ws_root}'."
            )
            continue

        if src_resolved != tgt_resolved:
            errors.append(
                f"Mount '{m.to_volume_arg()}' violates identical-path mount requirement "
                f"(resolved source '{src_resolved}' != resolved target '{tgt_resolved}')."
            )
            continue

        seen_targets[tgt_resolved] = m

        if spec.role == 'session' and expected_session_dir is not None:
            sessions_root = (ws_root / 'sessions').resolve()
            if src_resolved == ws_root or src_resolved == sessions_root:
                errors.append(
                    f"Session container '{spec.session_id}' cannot mount workspace/sessions root '{src_resolved}'."
                )
            elif src_resolved.is_relative_to(sessions_root) and not src_resolved.is_relative_to(expected_session_dir):
                errors.append(
                    f"Session container '{spec.session_id}' cannot mount another session's directory "
                    f"('{src_resolved}')."
                )
            elif src_resolved.is_relative_to((ws_root / 'audit').resolve()):
                errors.append(f"Session container '{spec.session_id}' cannot mount audit directory '{src_resolved}'.")
            elif (
                src_resolved.is_relative_to((ws_root / 'config').resolve())
                and src_resolved != (ws_root / 'config' / 'maintainer_rules.md').resolve()
            ):
                errors.append(
                    f"Session container '{spec.session_id}' cannot mount config path '{src_resolved}' "
                    "(only config/maintainer_rules.md read-only is permitted)."
                )
            elif src_resolved == (ws_root / 'tools').resolve() and not m.read_only:
                errors.append("Session mount for 'tools/' must be read-only.")
            elif src_resolved == (ws_root / 'config' / 'maintainer_rules.md').resolve() and not m.read_only:
                errors.append("Session mount for 'config/maintainer_rules.md' must be read-only.")

    if spec.role == 'hub':
        cfg_dir = (ws_root / 'config').resolve()
        aud_dir = (ws_root / 'audit').resolve()
        if cfg_dir not in seen_targets or not seen_targets[cfg_dir].read_only:
            errors.append("Hub container must mount '<workspace_root>/config' read-only.")
        if aud_dir not in seen_targets or not seen_targets[aud_dir].read_only:
            errors.append("Hub container must mount '<workspace_root>/audit' read-only.")

    is_valid = len(errors) == 0
    if not is_valid and raise_on_error:
        raise ValueError('; '.join(errors))
    return (is_valid, errors)


def build_session_launch_spec(
    session_id: str,
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
    writable_shared_repos: bool = False,
    include_github_token: bool = False,
    ports: Optional[List[PortPublishSpec]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    extra_inherited_env_keys: Optional[List[str]] = None,
    command: Optional[List[str]] = None,
    policy: Optional[HarnessPolicy] = None,
) -> LaunchSpec:
    """
    Build the canonical ``LaunchSpec`` for a ROS 2 session container.
    All workspace mounts use identical host and container paths inside ``workspace_root``.
    """
    ws_root = workspace_root.resolve()
    sess_dir = session_dir.resolve()
    active_policy = policy or load_policy(ws_root / 'config' / 'policy.yaml')

    tools_dir = (ws_root / 'tools').resolve()
    shared_repos_dir = (ws_root / 'shared_repos').resolve()
    rules_file = (ws_root / 'config' / 'maintainer_rules.md').resolve()
    image = get_image_for_distro(distro, custom_image)

    mounts: List[MountSpec] = [
        MountSpec(source=str(sess_dir), target=str(sess_dir), read_only=False),
        MountSpec(source=str(tools_dir), target=str(tools_dir), read_only=True),
        MountSpec(
            source=str(shared_repos_dir),
            target=str(shared_repos_dir),
            read_only=not writable_shared_repos,
        ),
    ]
    if rules_file.exists():
        mounts.append(MountSpec(source=str(rules_file), target=str(rules_file), read_only=True))

    env: Dict[str, str] = {
        'ROS_DISTRO': distro,
        'ROS_MAINTAINER_SESSION_ID': session_id,
        'ROS_MAINTAINER_GATEWAY_URL': (
            gateway_url or f"http://host.docker.internal:{active_policy.server.port}"
        ),
        'PYTHONUNBUFFERED': '1',
        'GIT_OPTIONAL_LOCKS': '0',
    }
    if extra_env:
        env.update(extra_env)

    inherited_keys: List[str] = ['ROS_MAINTAINER_GATEWAY_TOKEN']
    if include_github_token:
        inherited_keys.extend(['GITHUB_TOKEN', 'GH_TOKEN'])
    if extra_inherited_env_keys:
        for k in extra_inherited_env_keys:
            if k not in inherited_keys:
                inherited_keys.append(k)

    security_opts = ['seccomp=unconfined'] if active_policy.containers.seccomp_unconfined else []

    return LaunchSpec(
        role='session',
        container_name=get_container_name(session_id),
        image=image,
        workdir=str(sess_dir),
        workspace_root=str(ws_root),
        session_id=session_id,
        session_dir=str(sess_dir),
        mounts=mounts,
        env=env,
        inherited_env_keys=inherited_keys,
        ports=list(ports or []),
        extra_hosts=['host.docker.internal:host-gateway'],
        security_opts=security_opts,
        restart_policy=None,
        cpus=active_policy.containers.cpus,
        memory=active_policy.containers.memory,
        pids_limit=active_policy.containers.pids_limit,
        extra_flags=[],
        command=list(command) if command is not None else ['sleep', 'infinity'],
    )


def build_hub_launch_spec(
    workspace_root: Path,
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
    include_github_token: bool = False,
    ports: Optional[List[PortPublishSpec]] = None,
    extra_env: Optional[Dict[str, str]] = None,
    extra_inherited_env_keys: Optional[List[str]] = None,
    command: Optional[List[str]] = None,
    policy: Optional[HarnessPolicy] = None,
) -> LaunchSpec:
    """
    Build the canonical ``LaunchSpec`` for the Maintainer Hub container.
    Mounts ``workspace_root`` at its identical path with ``config/`` and ``audit/`` mounted
    read-only over it, and never mounts the Docker socket.
    """
    ws_root = workspace_root.resolve()
    active_policy = policy or load_policy(ws_root / 'config' / 'policy.yaml')
    image = custom_image or DEFAULT_HUB_IMAGE

    cfg_path = str((ws_root / 'config').resolve())
    aud_path = str((ws_root / 'audit').resolve())
    mounts: List[MountSpec] = [
        MountSpec(source=str(ws_root), target=str(ws_root), read_only=False),
        MountSpec(source=cfg_path, target=cfg_path, read_only=True),
        MountSpec(source=aud_path, target=aud_path, read_only=True),
    ]

    env: Dict[str, str] = {
        'ROS_MAINTAINER_GATEWAY_URL': (
            gateway_url or f"http://host.docker.internal:{active_policy.server.port}"
        ),
        'PYTHONUNBUFFERED': '1',
        'GIT_OPTIONAL_LOCKS': '0',
    }
    if extra_env:
        env.update(extra_env)

    inherited_keys: List[str] = ['ROS_MAINTAINER_GATEWAY_TOKEN']
    if include_github_token:
        inherited_keys.extend(['GITHUB_TOKEN', 'GH_TOKEN'])
    if extra_inherited_env_keys:
        for k in extra_inherited_env_keys:
            if k not in inherited_keys:
                inherited_keys.append(k)

    return LaunchSpec(
        role='hub',
        container_name=HUB_CONTAINER_NAME,
        image=image,
        workdir=str(ws_root),
        workspace_root=str(ws_root),
        session_id=None,
        session_dir=None,
        mounts=mounts,
        env=env,
        inherited_env_keys=inherited_keys,
        ports=list(ports or []),
        extra_hosts=['host.docker.internal:host-gateway'],
        security_opts=[],
        restart_policy='unless-stopped',
        cpus=active_policy.containers.cpus,
        memory=active_policy.containers.memory,
        pids_limit=active_policy.containers.pids_limit,
        extra_flags=[],
        command=list(command) if command is not None else ['sleep', 'infinity'],
    )


def verify_container_mounts_against_spec(
    inspect_data: Union[str, List[Dict[str, Any]], Dict[str, Any]],
    expected_spec: LaunchSpec,
) -> Dict[str, Any]:
    """
    Compare ``docker inspect`` JSON output against an expected ``LaunchSpec``.
    Reports any missing mounts, unexpected bind mounts, RW/RO mode drift, or
    privileged/host-network violations.
    """
    if isinstance(inspect_data, str):
        parsed = json.loads(inspect_data)
    else:
        parsed = inspect_data

    if isinstance(parsed, list):
        if not parsed:
            return {
                'verified': False,
                'container_name': expected_spec.container_name,
                'role': expected_spec.role,
                'drifts': ['Empty docker inspect payload.'],
                'expected_mounts': [dataclasses.asdict(m) for m in expected_spec.mounts],
                'actual_mounts': [],
            }
        container_obj = parsed[0]
    elif isinstance(parsed, dict):
        container_obj = parsed
    else:
        raise ValueError(f"Unexpected docker inspect payload type: {type(parsed)!r}")

    drifts: List[str] = []

    host_cfg = container_obj.get('HostConfig') or {}
    if host_cfg.get('Privileged') is True:
        drifts.append("Container is running with HostConfig.Privileged=true.")
    net_mode = (host_cfg.get('NetworkMode') or '').strip().lower()
    if net_mode == 'host':
        drifts.append("Container is running with HostConfig.NetworkMode='host'.")

    raw_mounts = container_obj.get('Mounts') or []
    actual_by_target: Dict[str, Dict[str, Any]] = {}
    actual_normalized: List[Dict[str, Any]] = []
    for m in raw_mounts:
        if not isinstance(m, dict):
            continue
        src = str(Path(m.get('Source', '')).resolve()) if m.get('Source') else ''
        dst = str(Path(m.get('Destination', '')).resolve()) if m.get('Destination') else ''
        rw = bool(m.get('RW', True))
        read_only = not rw
        entry = {
            'source': src,
            'target': dst,
            'read_only': read_only,
            'type': m.get('Type', 'bind'),
        }
        actual_normalized.append(entry)
        if dst:
            actual_by_target[dst] = entry

    expected_by_target: Dict[str, MountSpec] = {}
    for exp in expected_spec.mounts:
        exp_src = str(Path(exp.source).resolve())
        exp_dst = str(Path(exp.target).resolve())
        expected_by_target[exp_dst] = MountSpec(source=exp_src, target=exp_dst, read_only=exp.read_only)

    for exp_dst, exp_mount in expected_by_target.items():
        if exp_dst not in actual_by_target:
            drifts.append(
                f"Missing expected mount: {exp_mount.source} -> {exp_mount.target} "
                f"({'ro' if exp_mount.read_only else 'rw'})."
            )
            continue
        act = actual_by_target[exp_dst]
        if act['source'] != exp_mount.source:
            drifts.append(
                f"Mount source mismatch at '{exp_dst}': expected '{exp_mount.source}', got '{act['source']}'."
            )
        if act['read_only'] != exp_mount.read_only:
            drifts.append(
                f"Mount mode drift at '{exp_dst}': expected {'ro' if exp_mount.read_only else 'rw'}, "
                f"got {'ro' if act['read_only'] else 'rw'}."
            )

    for act_dst, act in actual_by_target.items():
        if act_dst not in expected_by_target:
            drifts.append(
                f"Unexpected extra mount: {act['source']} -> {act['target']} "
                f"({'ro' if act['read_only'] else 'rw'})."
            )

    return {
        'verified': len(drifts) == 0,
        'container_name': expected_spec.container_name,
        'role': expected_spec.role,
        'expected_mounts': [dataclasses.asdict(m) for m in expected_spec.mounts],
        'actual_mounts': actual_normalized,
        'drifts': drifts,
    }


def inspect_session_container_mounts(
    session_id: str,
    workspace_root: Path,
    session_dir: Optional[Path] = None,
    distro: Optional[str] = None,
    writable_shared_repos: Optional[bool] = None,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """
    Run ``docker inspect`` on a session container and verify its mounts against the expected ``LaunchSpec``.
    """
    from .worktree import read_session_metadata

    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    ws_root = workspace_root.resolve()
    sess_dir = (session_dir or (ws_root / 'sessions' / session_id)).resolve()
    meta = read_session_metadata(sess_dir) if sess_dir.is_dir() else {}
    resolved_distro = distro or meta.get('distro') or 'rolling'
    resolved_writable = (
        bool(meta.get('writable_shared_repos', False))
        if writable_shared_repos is None
        else bool(writable_shared_repos)
    )

    expected_spec = build_session_launch_spec(
        session_id=session_id,
        session_dir=sess_dir,
        workspace_root=ws_root,
        distro=resolved_distro,
        writable_shared_repos=resolved_writable,
    )
    if not rt:
        return {
            'verified': False,
            'container_name': expected_spec.container_name,
            'role': 'session',
            'drifts': ['No container runtime (docker or podman) found on PATH.'],
            'expected_mounts': [dataclasses.asdict(m) for m in expected_spec.mounts],
            'actual_mounts': [],
        }

    res = active_runner.run([rt, 'inspect', expected_spec.container_name])
    if res.returncode != 0 or not res.stdout.strip():
        return {
            'verified': False,
            'container_name': expected_spec.container_name,
            'role': 'session',
            'drifts': [
                f"Failed to inspect container '{expected_spec.container_name}': "
                f"{res.stderr.strip() or 'container not found'}"
            ],
            'expected_mounts': [dataclasses.asdict(m) for m in expected_spec.mounts],
            'actual_mounts': [],
        }

    return verify_container_mounts_against_spec(res.stdout, expected_spec)


def inspect_hub_container_mounts(
    workspace_root: Path,
    custom_image: Optional[str] = None,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """
    Run ``docker inspect`` on the Hub container and verify its mounts against the expected ``LaunchSpec``.
    """
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    ws_root = workspace_root.resolve()
    expected_spec = build_hub_launch_spec(workspace_root=ws_root, custom_image=custom_image)

    if not rt:
        return {
            'verified': False,
            'container_name': expected_spec.container_name,
            'role': 'hub',
            'drifts': ['No container runtime (docker or podman) found on PATH.'],
            'expected_mounts': [dataclasses.asdict(m) for m in expected_spec.mounts],
            'actual_mounts': [],
        }

    res = active_runner.run([rt, 'inspect', expected_spec.container_name])
    if res.returncode != 0 or not res.stdout.strip():
        return {
            'verified': False,
            'container_name': expected_spec.container_name,
            'role': 'hub',
            'drifts': [
                f"Failed to inspect container '{expected_spec.container_name}': "
                f"{res.stderr.strip() or 'container not found'}"
            ],
            'expected_mounts': [dataclasses.asdict(m) for m in expected_spec.mounts],
            'actual_mounts': [],
        }

    return verify_container_mounts_against_spec(res.stdout, expected_spec)


def load_workspace_env(workspace_root: Path) -> Dict[str, str]:
    """
    Load environment variables and credentials for a workspace.
    Automatically migrates any credentials from `<workspace_root>/.env` into the host-only
    state directory (`~/.local/state/ros_maintainer_agent_harness/credentials.env`) so no
    credentials remain inside the mounted workspace tree.
    """
    from .gateway import load_state_credentials, migrate_workspace_credentials, parse_env_file

    ws_root = workspace_root.resolve()
    migrate_workspace_credentials(ws_root)
    env_vars: Dict[str, str] = {}
    env_file = ws_root / '.env'
    if env_file.exists():
        env_vars.update(parse_env_file(env_file))
    env_vars.update(load_state_credentials(workspace_root=ws_root))
    return env_vars


def save_workspace_env_var(workspace_root: Path, key: str, value: str) -> Path:
    """
    Save or update a configuration/credential variable with 0600 permissions.
    Credentials (`*_TOKEN`, `*_API_KEY`, etc.) are stored outside the workspace tree in
    `~/.local/state/ros_maintainer_agent_harness/`.
    """
    from .gateway import (
        CREDENTIAL_ENV_KEYS,
        migrate_workspace_credentials,
        parse_env_file,
        save_state_credential,
        write_private_env_file,
    )

    ws_root = workspace_root.resolve()
    migrate_workspace_credentials(ws_root)
    if key in CREDENTIAL_ENV_KEYS or key.endswith('_TOKEN') or key.endswith('_API_KEY'):
        return save_state_credential(key, value, workspace_root=ws_root)

    env_file = ws_root / '.env'
    existing = parse_env_file(env_file)
    existing[key] = value
    return write_private_env_file(
        env_file,
        existing,
        'ROS Maintainer Agent Harness Workspace Environment',
    )


def get_container_github_token(workspace_root: Path) -> Optional[str]:
    """Retrieve the configured ROS_CONTAINER_GITHUB_TOKEN from environment or host credentials state."""
    ws_env = load_workspace_env(workspace_root)
    token = os.environ.get('ROS_CONTAINER_GITHUB_TOKEN') or ws_env.get('ROS_CONTAINER_GITHUB_TOKEN')
    return token


def detect_container_runtime(runner: Optional[CommandRunner] = None) -> Optional[str]:
    """Detect available container runtime ('docker' or 'podman')."""
    active_runner = runner or get_default_runner()
    for candidate in ('docker', 'podman'):
        if active_runner.which(candidate):
            return candidate
    return None


def get_container_name(session_id: str) -> str:
    """Return the standardized container name for a session."""
    return f"ros-harness-{session_id}"


def _normalize_arch(raw_arch: Optional[str]) -> str:
    val = (raw_arch or '').strip().lower()
    if val in ('aarch64', 'arm64', 'arm64v8'):
        return 'arm64'
    if val in ('x86_64', 'amd64', 'x64'):
        return 'amd64'
    return val or 'unknown'


def check_token_and_environment(
    workspace_root: Path,
    runner: Optional[CommandRunner] = None,
    state_dir: Optional[Path] = None,
    host_arch_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Check workspace readiness: initialization, container engine (OrbStack/Docker/Podman),
    architecture, gateway launch service, Hub container, session images, OpenCode client,
    LLM credentials, GitHub tokens, and global MCP config.
    """
    import platform as py_platform
    from .gateway import get_gateway_status, get_harness_version, load_state_credentials

    active_runner = runner or get_default_runner()
    ws_root = workspace_root.resolve()
    ws_initialized = (ws_root / 'config' / 'policy.yaml').exists()
    runtime = detect_container_runtime(runner=active_runner)
    host_version = get_harness_version()
    host_arch = _normalize_arch(host_arch_override or py_platform.machine())

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
    state_creds = load_state_credentials(state_dir=state_dir, workspace_root=ws_root)
    host_token = (
        os.environ.get('ROS_HOST_GITHUB_TOKEN')
        or ws_env.get('ROS_HOST_GITHUB_TOKEN')
        or state_creds.get('ROS_HOST_GITHUB_TOKEN')
        or os.environ.get('GITHUB_TOKEN')
        or os.environ.get('GH_TOKEN')
    )
    gh_cli_token = None
    if active_runner.which('gh'):
        try:
            res = active_runner.run(
                ['gh', 'auth', 'token'],
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

    def _safe_run(cmd: Sequence[str], timeout: float = 2.0):
        from .runner import CommandResult

        try:
            return active_runner.run(cmd, timeout=timeout)
        except Exception as exc:
            return CommandResult(
                args=[str(part) for part in cmd],
                returncode=1,
                stdout='',
                stderr=str(exc),
            )

    # 1. Container Engine & Architecture Check
    docker_host = os.environ.get('DOCKER_HOST')
    docker_context = None
    engine_reachable = False
    engine_kind = 'unreachable'
    engine_arch: Optional[str] = None

    if runtime:
        if runtime == 'docker':
            ctx_res = _safe_run(['docker', 'context', 'show'], timeout=2.0)
            if ctx_res.returncode == 0 and ctx_res.stdout.strip():
                docker_context = ctx_res.stdout.strip()

        info_res = _safe_run([runtime, 'info', '--format', '{{json .}}'], timeout=2.0)
        if info_res.returncode == 0 and info_res.stdout.strip():
            engine_reachable = True
            raw_info = info_res.stdout.strip()
            try:
                info_json = json.loads(raw_info)
            except Exception:
                info_json = {}
            os_str = str(info_json.get('OperatingSystem') or raw_info).lower()
            name_str = str(info_json.get('Name') or '').lower()
            ctx_str = (docker_context or '').lower()
            dh_str = (docker_host or '').lower()
            if 'orbstack' in os_str or 'orbstack' in name_str or 'orbstack' in ctx_str or 'orbstack' in dh_str:
                engine_kind = 'orbstack'
            elif 'docker desktop' in os_str or 'desktop-linux' in ctx_str:
                engine_kind = 'docker-desktop'
            elif runtime == 'podman':
                engine_kind = 'podman'
            else:
                engine_kind = 'linux-docker'
            raw_daemon_arch = info_json.get('Architecture')
            if raw_daemon_arch:
                engine_arch = _normalize_arch(str(raw_daemon_arch))

    arch_mismatch = bool(engine_arch and host_arch != 'unknown' and engine_arch != host_arch)

    engine_report = {
        'runtime': runtime,
        'reachable': engine_reachable,
        'kind': engine_kind,
        'docker_host': docker_host,
        'context': docker_context,
        'host_arch': host_arch,
        'engine_arch': engine_arch,
        'arch_mismatch': arch_mismatch,
    }

    # 2. Gateway Launch Service Check
    gw_status = get_gateway_status(ws_root, state_dir=state_dir)
    gw_running = bool(gw_status.get('running'))
    gw_version = gw_status.get('version')
    gw_version_matches = bool(not gw_version or gw_version == host_version)

    # 3. Hub Container Check
    hub_image_present = False
    hub_running = False
    hub_image_version: Optional[str] = None
    hub_mounts_verified = False
    hub_no_docker_socket = True
    hub_config_ro = False
    hub_audit_ro = False
    hub_drifts: List[str] = []
    gw_reachable_from_hub = False

    if runtime and engine_reachable:
        img_res = _safe_run([runtime, 'image', 'inspect', DEFAULT_HUB_IMAGE], timeout=2.0)
        if img_res.returncode == 0 and img_res.stdout.strip():
            hub_image_present = True
            try:
                img_parsed = json.loads(img_res.stdout)
                img_obj = img_parsed[0] if isinstance(img_parsed, list) and img_parsed else img_parsed
                labels = (img_obj.get('Config') or {}).get('Labels') or {}
                hub_image_version = (
                    labels.get('io.ros-maintainer-harness.version')
                    or labels.get('org.opencontainers.image.version')
                )
            except Exception:
                pass

        hub_inspect = _safe_run([runtime, 'inspect', HUB_CONTAINER_NAME], timeout=2.0)
        if hub_inspect.returncode == 0 and hub_inspect.stdout.strip():
            try:
                parsed = json.loads(hub_inspect.stdout)
                c_obj = parsed[0] if isinstance(parsed, list) and parsed else parsed
            except Exception:
                c_obj = {}
            hub_running = bool((c_obj.get('State') or {}).get('Running', False))
            c_labels = ((c_obj.get('Config') or {}).get('Labels')) or {}
            if not hub_image_version:
                hub_image_version = (
                    c_labels.get('io.ros-maintainer-harness.version')
                    or c_labels.get('org.opencontainers.image.version')
                )
            expected_hub_spec = build_hub_launch_spec(ws_root)
            m_ver = verify_container_mounts_against_spec([c_obj], expected_hub_spec)
            hub_mounts_verified = bool(m_ver['verified'])
            hub_drifts = list(m_ver['drifts'])
            for m in m_ver['actual_mounts']:
                src_low = str(m.get('source') or '').lower()
                tgt_str = str(m.get('target') or '')
                if 'docker.sock' in src_low or 'podman.sock' in src_low:
                    hub_no_docker_socket = False
                if tgt_str == str((ws_root / 'config').resolve()) and m.get('read_only'):
                    hub_config_ro = True
                if tgt_str == str((ws_root / 'audit').resolve()) and m.get('read_only'):
                    hub_audit_ro = True

            if hub_running and gw_running:
                gw_port = gw_status.get('port') or 8765
                probe = _safe_run(
                    [
                        runtime,
                        'exec',
                        HUB_CONTAINER_NAME,
                        'curl',
                        '-fsS',
                        '--max-time',
                        '3',
                        f'http://host.docker.internal:{gw_port}/healthz',
                    ],
                    timeout=3.0,
                )
                gw_reachable_from_hub = probe.returncode == 0

    hub_version_matches = bool(not hub_image_version or hub_image_version == host_version)

    gateway_report = {
        'running': gw_running,
        'bind_host': gw_status.get('host', '127.0.0.1'),
        'port': gw_status.get('port', 8765),
        'pid': gw_status.get('pid'),
        'version': gw_version,
        'version_matches_host': gw_version_matches,
        'reachable_from_hub': gw_reachable_from_hub,
    }

    hub_report = {
        'image_present': hub_image_present,
        'running': hub_running,
        'image_version': hub_image_version,
        'version_matches_host': hub_version_matches,
        'mounts_verified': hub_mounts_verified,
        'no_docker_socket': hub_no_docker_socket,
        'config_read_only': hub_config_ro,
        'audit_read_only': hub_audit_ro,
        'drifts': hub_drifts,
    }

    # 4. Session Images & Architecture Check
    unsupported_arm64_distros: List[str] = []
    if host_arch == 'arm64':
        unsupported_arm64_distros.append('noetic')
    session_images_report = {
        'host_arch': host_arch,
        'default_images': dict(DEFAULT_DISTRO_IMAGES),
        'unsupported_arm64_distros': unsupported_arm64_distros,
    }

    # 5. OpenCode Client & LLM Credentials Check
    opencode_path = active_runner.which('opencode')
    opencode_version: Optional[str] = None
    if opencode_path:
        oc_ver_res = _safe_run(['opencode', '--version'], timeout=2.0)
        if oc_ver_res.returncode == 0 and oc_ver_res.stdout.strip():
            opencode_version = oc_ver_res.stdout.strip().splitlines()[0]

    configured_llm_providers: List[str] = []
    for key_name in (
        'ANTHROPIC_API_KEY',
        'OPENAI_API_KEY',
        'GEMINI_API_KEY',
        'GOOGLE_GENERATIVE_AI_API_KEY',
        'OPENROUTER_API_KEY',
    ):
        val = os.environ.get(key_name) or ws_env.get(key_name) or state_creds.get(key_name)
        if val and val.strip() and val.strip().lower() != 'none':
            configured_llm_providers.append(key_name)

    opencode_report = {
        'installed': bool(opencode_path),
        'path': opencode_path,
        'version': opencode_version,
        'llm_credential_configured': len(configured_llm_providers) > 0,
        'configured_llm_providers': configured_llm_providers,
    }

    warnings: List[str] = []
    recommendations: List[str] = []
    remediations: List[Dict[str, str]] = []

    def _add_remediation(component: str, issue: str, fix_cmd: str) -> None:
        warnings.append(issue)
        recommendations.append(f"Run `{fix_cmd}`.")
        remediations.append({'component': component, 'issue': issue, 'fix_command': fix_cmd})

    if not ws_initialized:
        _add_remediation(
            'workspace',
            f"Workspace at {ws_root} is not initialized.",
            f"ros-maintainer-harness -w {ws_root} init",
        )

    if not runtime:
        warnings.append("No container runtime (docker or podman) found on PATH.")
        recommendations.append("Install Docker or OrbStack so session builds and tests run in isolated containers.")
        remediations.append({
            'component': 'engine',
            'issue': 'No container runtime (docker or podman) found on PATH.',
            'fix_command': 'brew install --cask orbstack',
        })
    elif not engine_reachable:
        _add_remediation(
            'engine',
            f"Container engine '{runtime}' is installed but the daemon is not reachable.",
            'orb start' if sys.platform == 'darwin' else 'sudo systemctl start docker',
        )

    if arch_mismatch:
        _add_remediation(
            'engine',
            f"Container engine architecture ({engine_arch}) does not match host architecture ({host_arch}).",
            'docker context use orbstack' if sys.platform == 'darwin' else 'docker context use default',
        )

    if not container_token_configured:
        warnings.append(
            "ROS_CONTAINER_GITHUB_TOKEN is not configured. Agents must not silently pass your host "
            "`gh auth token` into containers."
        )
        fix_tok = f"ros-maintainer-harness -w {ws_root} token-setup --container-token <TOKEN>"
        no_tok = f"ros-maintainer-harness -w {ws_root} token-setup --no-token"
        recommendations.append(
            f"Configure a read-only fine-grained GitHub PAT with `{fix_tok}` "
            f"or explicitly opt into unauthenticated mode with `{no_tok}`."
        )
        remediations.append({
            'component': 'github_token',
            'issue': 'ROS_CONTAINER_GITHUB_TOKEN is not configured.',
            'fix_command': fix_tok,
        })

    if token_matches_host_gh:
        warnings.append(
            "ROS_CONTAINER_GITHUB_TOKEN is identical to your active `gh auth token` on the host. "
            "If your `gh` CLI token has write permissions, untrusted code in the container could use it."
        )
        recommendations.append(
            "Create a separate fine-grained Personal Access Token with zero repository write permissions "
            "for ROS_CONTAINER_GITHUB_TOKEN."
        )

    if not gw_running:
        _add_remediation(
            'gateway_service',
            'Gateway launch service is not running.',
            f"ros-maintainer-harness -w {ws_root} gateway start",
        )
    elif not gw_version_matches:
        _add_remediation(
            'gateway_service',
            f"Gateway service version ({gw_version}) differs from host version ({host_version}).",
            f"ros-maintainer-harness -w {ws_root} gateway restart",
        )

    if runtime and engine_reachable:
        if not hub_image_present:
            _add_remediation(
                'hub',
                f"Hub container image '{DEFAULT_HUB_IMAGE}' is not built.",
                f"ros-maintainer-harness -w {ws_root} hub build",
            )
        elif not hub_running:
            _add_remediation(
                'hub',
                f"Hub container '{HUB_CONTAINER_NAME}' is not running.",
                f"ros-maintainer-harness -w {ws_root} hub start",
            )
        else:
            if not hub_mounts_verified or not hub_no_docker_socket:
                _add_remediation(
                    'hub',
                    f"Hub container '{HUB_CONTAINER_NAME}' mounts drifted from policy: {'; '.join(hub_drifts)}",
                    f"ros-maintainer-harness -w {ws_root} hub restart",
                )
            if not hub_version_matches:
                _add_remediation(
                    'hub',
                    f"Hub image version ({hub_image_version}) differs from host version ({host_version}).",
                    f"ros-maintainer-harness -w {ws_root} redeploy",
                )
            if gw_running and not gw_reachable_from_hub:
                _add_remediation(
                    'gateway_service',
                    'Gateway service is not reachable from inside the Hub container via host.docker.internal.',
                    f"ros-maintainer-harness -w {ws_root} gateway restart",
                )

    if unsupported_arm64_distros:
        warnings.append(
            f"Host architecture is arm64: official OSRF images for {', '.join(unsupported_arm64_distros)} "
            "are amd64-only and require Rosetta/QEMU emulation."
        )

    if not opencode_report['installed']:
        oc_install_cmd = (
            'brew install anomalyco/tap/opencode'
            if sys.platform == 'darwin'
            else 'curl -fsSL https://opencode.ai/install | bash'
        )
        _add_remediation(
            'opencode',
            "OpenCode CLI ('opencode') is not installed on the host PATH.",
            oc_install_cmd,
        )

    if not opencode_report['llm_credential_configured']:
        _add_remediation(
            'opencode',
            'No LLM provider API key (ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY) is configured in host state.',
            f"ros-maintainer-harness -w {ws_root} token-setup --llm-key ANTHROPIC_API_KEY=<KEY>",
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
        'host_version': host_version,
        'container_runtime': runtime,
        'container_token_configured': container_token_configured,
        'container_token_mode': container_token_mode,
        'host_token_configured': host_token_configured,
        'token_matches_host_gh_warning': token_matches_host_gh,
        'global_mcp_configured': global_mcp_configured,
        'engine': engine_report,
        'gateway_service': gateway_report,
        'hub': hub_report,
        'session_images': session_images_report,
        'opencode': opencode_report,
        'remediations': remediations,
        'warnings': warnings,
        'recommendations': recommendations,
    }


def generate_devcontainer_config(
    session_dir: Path,
    workspace_root: Path,
    distro: str = 'rolling',
    custom_image: Optional[str] = None,
    gateway_url: Optional[str] = None,
    writable_shared_repos: bool = False,
) -> Dict[str, Any]:
    """
    Generate a complete devcontainer.json configuration dictionary for a session.
    By default, `shared_repos/` is mounted read-only (`writable_shared_repos=False`) so untrusted
    build/test code inside the container cannot mutate shared git objects or refs across sessions.
    """
    session_dir = session_dir.resolve()
    workspace_root = workspace_root.resolve()
    tools_dir = workspace_root / 'tools'
    shared_repos_dir = workspace_root / 'shared_repos'
    rules_file = workspace_root / 'config' / 'maintainer_rules.md'
    image = get_image_for_distro(distro, custom_image)

    shared_mount = (
        f"source={shared_repos_dir},target={shared_repos_dir},type=bind"
        if writable_shared_repos
        else f"source={shared_repos_dir},target={shared_repos_dir},type=bind,readonly"
    )

    config: Dict[str, Any] = {
        "name": f"ROS 2 Maintainer Sandbox ({session_dir.name})",
        "image": image,
        "workspaceFolder": "/workspace",
        "workspaceMount": f"source={session_dir},target=/workspace,type=bind",
        "mounts": [
            f"source={tools_dir},target=/workspace/tools,type=bind,readonly",
            shared_mount,
        ],
        "containerEnv": {
            "PATH": f"{tools_dir}/bin:/workspace/tools/bin:/root/.local/bin:${{containerEnv:PATH}}",
            "ROS_DISTRO": distro,
            "ROS_MAINTAINER_SESSION_ID": session_dir.name,
            "ROS_MAINTAINER_GATEWAY_URL": gateway_url or "http://host.docker.internal:8765",
            "PYTHONUNBUFFERED": "1",
            "GIT_OPTIONAL_LOCKS": "0",
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
    writable_shared_repos: bool = False,
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
        writable_shared_repos=writable_shared_repos,
    )

    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)

    return config_file


def get_container_status(
    session_id: str,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Check whether the session container is currently running."""
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    container_name = get_container_name(session_id)
    if not rt:
        return {
            'running': False,
            'status': 'no_runtime',
            'container_name': container_name,
            'runtime': None,
        }

    res = active_runner.run(
        [rt, 'inspect', '-f', '{{.State.Running}}', container_name],
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
    writable_shared_repos: Optional[bool] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """
    Start a detached sandbox container for the given session using a validated ``LaunchSpec``
    so commands and builds can be executed inside it via `session exec` or `exec_in_session`.

    By default, `shared_repos/` is mounted read-only (`:ro`). Pass `writable_shared_repos=True`
    (or `ros-maintainer-harness session up <session_id> --writable-shared-repos`) to mount
    `shared_repos/` read-write when explicitly requested or after pre-build review.
    """
    from .worktree import read_session_metadata, write_session_metadata

    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    if not rt:
        return {
            'success': False,
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    session_meta = read_session_metadata(session_dir) if session_dir.is_dir() else {}
    mode_changed = False
    if writable_shared_repos is None:
        use_writable_shared = bool(session_meta.get('writable_shared_repos', False))
    else:
        use_writable_shared = bool(writable_shared_repos)
        if session_dir.is_dir():
            prev_mode = bool(session_meta.get('writable_shared_repos', False))
            if prev_mode != use_writable_shared:
                mode_changed = True
            write_session_metadata(session_dir, {'writable_shared_repos': use_writable_shared})
            try:
                write_devcontainer_config(
                    session_dir=session_dir,
                    workspace_root=workspace_root,
                    distro=distro,
                    custom_image=custom_image,
                    gateway_url=gateway_url,
                    writable_shared_repos=use_writable_shared,
                )
            except Exception:
                pass

    status_info = get_container_status(session_id, runtime=rt, runner=active_runner)
    container_name = status_info['container_name']
    if status_info['running'] and not mode_changed and writable_shared_repos is None:
        return {
            'success': True,
            'status': 'already_running',
            'container_name': container_name,
            'runtime': rt,
            'writable_shared_repos': use_writable_shared,
        }

    session_dir = session_dir.resolve()
    workspace_root = workspace_root.resolve()
    policy = load_policy(workspace_root / 'config' / 'policy.yaml')
    token = get_container_github_token(workspace_root)
    has_github_token = bool(token and token.lower() != 'none')

    spec = build_session_launch_spec(
        session_id=session_id,
        session_dir=session_dir,
        workspace_root=workspace_root,
        distro=distro,
        custom_image=custom_image,
        gateway_url=gateway_url,
        writable_shared_repos=use_writable_shared,
        include_github_token=has_github_token,
        policy=policy,
    )
    valid, errors = validate_launch_spec(spec, policy=policy)
    if not valid:
        return {
            'success': False,
            'status': 'policy_denied',
            'container_name': container_name,
            'runtime': rt,
            'error': '; '.join(errors),
            'errors': errors,
        }

    # Remove any exited container (or running container when mount mode was explicitly changed)
    active_runner.run([rt, 'rm', '-f', container_name])

    from .auth import TokenStore

    token_store = TokenStore()
    issued_token = token_store.issue_token(
        role=f"session:{session_id}",
        session_id=session_id,
        container_name=container_name,
        replace_existing=True,
    )

    run_env = os.environ.copy()
    run_env['ROS_MAINTAINER_GATEWAY_TOKEN'] = issued_token.token
    if has_github_token and token:
        run_env['GITHUB_TOKEN'] = token
        run_env['GH_TOKEN'] = token
    else:
        run_env.pop('GITHUB_TOKEN', None)
        run_env.pop('GH_TOKEN', None)

    cmd = spec.to_docker_run_argv(runtime=rt)
    res = active_runner.run(cmd, env=run_env, timeout=300)
    if res.returncode != 0:
        token_store.revoke_token_by_id(issued_token.token_id)
        return {
            'success': False,
            'status': 'failed',
            'container_name': container_name,
            'runtime': rt,
            'error': res.stderr.strip() or res.stdout.strip(),
        }

    tools_dir = (workspace_root / 'tools').resolve()
    # Run post-create setup inside the container:
    # 1. Source ROS setup.bash in ~/.bashrc
    # 2. Configure git safe.directory '*' for bind-mounted checkouts
    # 3. Symlink /workspace to the identical-path host session_dir so both paths resolve
    setup_cmd = (
        "source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null || true; "
        "grep -q '/opt/ros/' ~/.bashrc 2>/dev/null || "
        "echo 'source /opt/ros/$ROS_DISTRO/setup.bash' >> ~/.bashrc; "
        "git config --global --add safe.directory '*' 2>/dev/null || true; "
        f"ln -sfn {shlex.quote(str(session_dir))} /workspace; "
        f"(pip install -r {shlex.quote(str(tools_dir / 'requirements.txt'))} 2>/dev/null || true)"
    )
    active_runner.run(
        [rt, 'exec', container_name, 'bash', '-c', setup_cmd],
        timeout=120,
    )

    return {
        'success': True,
        'status': 'started',
        'container_name': container_name,
        'runtime': rt,
        'image': spec.image,
        'distro': distro,
        'writable_shared_repos': use_writable_shared,
        'token_id': issued_token.token_id,
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
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """
    Execute a shell command inside the session container with the ROS environment sourced.
    """
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    if not rt:
        return {
            'success': False,
            'returncode': 127,
            'stdout': '',
            'stderr': 'No container runtime (docker or podman) found on PATH.',
        }

    status_info = get_container_status(session_id, runtime=rt, runner=active_runner)
    container_name = status_info['container_name']

    if not status_info['running']:
        if auto_start and session_dir is not None and workspace_root is not None:
            start_res = start_session_container(
                session_id=session_id,
                session_dir=session_dir,
                workspace_root=workspace_root,
                distro=distro,
                runtime=rt,
                runner=active_runner,
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

    chown_trap = ""
    if hasattr(os, 'getuid') and hasattr(os, 'getgid'):
        try:
            uid = os.getuid()
            gid = os.getgid()
            if uid != 0:
                chown_trap = (
                    f"trap 'find /workspace/src -user 0 -exec chown -h {uid}:{gid} {{}} + "
                    f"2>/dev/null || true' EXIT; "
                )
        except Exception:
            pass

    tools_bin = (
        f"{(workspace_root.resolve() / 'tools' / 'bin')}:"
        if workspace_root is not None
        else ''
    )
    wrapped_cmd = (
        f"{chown_trap}"
        "source /opt/ros/$ROS_DISTRO/setup.bash 2>/dev/null || true; "
        "if [ -f /workspace/install/setup.bash ]; then "
        "source /workspace/install/setup.bash 2>/dev/null || true; fi; "
        f"export PATH=\"{tools_bin}/workspace/tools/bin:/root/.local/bin:$PATH\"; "
        f"{command}"
    )

    from .audit import redact_credentials

    try:
        res = active_runner.run(
            [rt, 'exec', '-w', workdir, container_name, 'bash', '-c', wrapped_cmd],
            timeout=timeout,
        )
        return {
            'success': res.returncode == 0,
            'returncode': res.returncode,
            'stdout': redact_credentials(res.stdout),
            'stderr': redact_credentials(res.stderr),
            'container_name': container_name,
        }
    except subprocess.TimeoutExpired as e:
        raw_out = (e.stdout or '') if isinstance(e.stdout, str) else ''
        return {
            'success': False,
            'returncode': 124,
            'stdout': redact_credentials(raw_out),
            'stderr': f"Command timed out after {timeout} seconds.",
            'container_name': container_name,
        }


def stop_session_container(
    session_id: str,
    runtime: Optional[str] = None,
    runner: Optional[CommandRunner] = None,
) -> Dict[str, Any]:
    """Stop and remove the sandbox container for a session and revoke its bearer token."""
    from .auth import TokenStore

    revoked_count = TokenStore().revoke_tokens_for_session(session_id)
    active_runner = runner or get_default_runner()
    rt = runtime or detect_container_runtime(runner=active_runner)
    container_name = get_container_name(session_id)
    if not rt:
        return {
            'success': False,
            'container_name': container_name,
            'revoked_tokens': revoked_count,
            'error': 'No container runtime (docker or podman) found on PATH.',
        }

    res = active_runner.run([rt, 'rm', '-f', container_name])
    return {
        'success': res.returncode == 0,
        'container_name': container_name,
        'runtime': rt,
        'revoked_tokens': revoked_count,
        'output': res.stdout.strip() or res.stderr.strip(),
    }
