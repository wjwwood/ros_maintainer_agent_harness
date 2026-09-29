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
import re
import shlex
import shutil
from typing import Any, Dict, List, Optional, Tuple

from .worktree import read_session_metadata

HOST_PASSTHROUGH_BINARIES = {
    'ros-maintainer-harness',
    'agentapi',
    'docker',
    'podman',
    'export',
    'true',
    'false',
    'echo',
}


def is_running_in_container() -> bool:
    """Return True if the current process is already running inside a container."""
    if os.environ.get('ROS_MAINTAINER_SESSION_ID'):
        return True
    return Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()


def get_harness_executable() -> str:
    """Locate the `ros-maintainer-harness` executable on the host."""
    harness_bin = shutil.which('ros-maintainer-harness')
    if harness_bin:
        return harness_bin
    local_bin = Path.home() / '.local' / 'bin' / 'ros-maintainer-harness'
    if local_bin.exists():
        return str(local_bin)
    return 'ros-maintainer-harness'


def _extract_session_from_path(
    path_str: str,
    sessions_dir: Path,
) -> Optional[Tuple[str, Path]]:
    """If `path_str` is inside `sessions_dir/<session_id>`, return `(session_id, session_dir)`."""
    if not path_str:
        return None
    try:
        candidate = Path(path_str).expanduser().resolve()
        rel = candidate.relative_to(sessions_dir.resolve())
        if rel.parts:
            session_id = rel.parts[0]
            session_dir = sessions_dir.resolve() / session_id
            if session_dir.is_dir():
                return session_id, session_dir
    except Exception:
        return None
    return None


def resolve_session_for_hook(
    workspace_root: Path,
    conversation_id: str = '',
    cwd: str = '',
    command_line: str = '',
    workspace_paths: Optional[List[str]] = None,
    explicit_session_id: Optional[str] = None,
) -> Optional[Tuple[str, Path, Dict[str, Any]]]:
    """
    Determine whether a tool invocation belongs to a maintainer session.

    Resolution order:
    1. `cwd` is inside `<workspace_root>/sessions/<session_id>`
    2. `workspace_paths` contains `<workspace_root>/sessions/<session_id>`
    3. `conversation_id` matches a session's `conversation_id` in `session.json`
       (and does not match `hub_conversation_id`)
    4. `explicit_session_id` if provided and `conversation_id` is not a Hub conversation
    """
    ws_root = workspace_root.resolve()
    sessions_dir = ws_root / 'sessions'
    if not sessions_dir.is_dir():
        return None

    # 1. Check Cwd
    found = _extract_session_from_path(cwd, sessions_dir)
    if found:
        sid, sdir = found
        return sid, sdir, read_session_metadata(sdir)

    # 2. Check workspace_paths
    for wp in (workspace_paths or []):
        found = _extract_session_from_path(wp, sessions_dir)
        if found:
            sid, sdir = found
            return sid, sdir, read_session_metadata(sdir)

    # 3. Check conversation_id in sessions/*/session.json
    is_hub_conversation = False
    if conversation_id:
        for sdir in sorted(sessions_dir.iterdir()):
            if not sdir.is_dir():
                continue
            meta = read_session_metadata(sdir)
            if meta.get('hub_conversation_id') == conversation_id:
                is_hub_conversation = True
            if (
                meta.get('conversation_id') == conversation_id
                and meta.get('hub_conversation_id') != conversation_id
            ):
                return sdir.name, sdir, meta

    if is_hub_conversation:
        return None

    # 4. Fallback to explicit_session_id when not in Hub conversation and Cwd is not workspace root
    if explicit_session_id:
        try:
            if cwd and Path(cwd).expanduser().resolve() == ws_root:
                return None
        except Exception:
            pass
        sdir = sessions_dir / explicit_session_id
        if sdir.is_dir():
            return explicit_session_id, sdir, read_session_metadata(sdir)

    return None


def _first_executable_in_segment(segment: str) -> Optional[str]:
    """Extract the binary basename from a single shell command segment."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.strip().split()
    idx = 0
    in_env = False
    while idx < len(tokens):
        tok = tokens[idx]
        idx += 1
        # Skip env assignments like PATH=... or FOO=bar
        if '=' in tok and not tok.startswith('-') and not tok.startswith('/'):
            continue
        if tok == 'env':
            in_env = True
            continue
        if in_env and tok.startswith('-'):
            if tok in ('-u', '--unset', '-C', '--chdir', '-S', '--split-string') and idx < len(tokens):
                idx += 1
            continue
        if tok in ('command', 'nohup'):
            continue
        return Path(tok).name
    return None


def is_host_passthrough_command(command_line: str) -> bool:
    """
    Return True if `command_line` is purely a host-control command
    (`ros-maintainer-harness`, `agentapi`, `docker`, `podman`) that should run on the host
    without being wrapped into `session exec`.
    """
    stripped = (command_line or '').strip()
    if not stripped:
        return True

    # Split on top-level &&, ||, ;, or newline
    segments = [s.strip() for s in re.split(r'&&|\|\||;|\n', stripped) if s.strip()]
    if not segments:
        return True

    saw_control_bin = False
    for seg in segments:
        exe = _first_executable_in_segment(seg)
        if not exe:
            continue
        if exe not in HOST_PASSTHROUGH_BINARIES:
            return False
        if exe in ('ros-maintainer-harness', 'agentapi', 'docker', 'podman'):
            saw_control_bin = True

    return saw_control_bin


def is_forbidden_uncontainerized_host_command(command_line: str) -> Optional[str]:
    """
    Check if a command running outside any session is attempting to run `colcon` or `rosdep`
    directly on the host OS.
    """
    stripped = (command_line or '').strip()
    if not stripped or is_host_passthrough_command(stripped):
        return None

    segments = [s.strip() for s in re.split(r'&&|\|\||;|\||\n', stripped) if s.strip()]
    for seg in segments:
        exe = _first_executable_in_segment(seg)
        if exe in ('colcon', 'rosdep'):
            return (
                f"Direct host execution of '{exe}' is prohibited. "
                "Create or use an isolated session in sessions/<id> so builds and tests "
                "execute inside a sandbox container."
            )
    return None


def compute_container_workdir(cwd: str, session_dir: Path) -> str:
    """Map a host `cwd` (or `/workspace/...`) to the corresponding path inside the container."""
    if not cwd:
        return '/workspace'
    if cwd == '/workspace' or cwd.startswith('/workspace/'):
        return cwd
    try:
        resolved_cwd = Path(cwd).expanduser().resolve()
        resolved_session = session_dir.resolve()
        rel = resolved_cwd.relative_to(resolved_session)
        if not rel.parts:
            return '/workspace'
        return '/workspace/' + '/'.join(rel.parts)
    except Exception:
        return '/workspace'


def build_session_exec_command(
    workspace_root: Path,
    session_id: str,
    container_workdir: str,
    command_line: str,
) -> str:
    """Wrap `command_line` in `ros-maintainer-harness -w <ws_root> session exec -d <workdir> <session_id>`."""
    harness_bin = get_harness_executable()
    ws_str = str(workspace_root.resolve())
    return (
        f"{shlex.quote(harness_bin)} -w {shlex.quote(ws_str)} "
        f"session exec -d {shlex.quote(container_workdir)} "
        f"{shlex.quote(session_id)} -- {shlex.quote(command_line)}"
    )


def _format_allow(
    is_claude_format: bool,
    rewritten_cmd: Optional[str] = None,
    rewritten_cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Format the PreToolUse hook response for either Antigravity/Gemini or Claude Code."""
    if rewritten_cmd is None and rewritten_cwd is None:
        return {}

    if is_claude_format:
        if rewritten_cmd is None:
            return {}
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'allow',
                'updatedInput': {
                    'command': rewritten_cmd,
                },
            },
        }

    result: Dict[str, Any] = {'decision': 'allow'}
    overwrite: Dict[str, Any] = {}
    if rewritten_cmd is not None:
        overwrite['CommandLine'] = rewritten_cmd
    if rewritten_cwd is not None:
        overwrite['Cwd'] = rewritten_cwd
    if overwrite:
        result['overwrite'] = overwrite
    return result


def _format_deny(is_claude_format: bool, reason: str) -> Dict[str, Any]:
    """Format a PreToolUse denial response."""
    if is_claude_format:
        return {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'permissionDecision': 'deny',
                'permissionDecisionReason': reason,
            },
        }
    return {
        'decision': 'deny',
        'reason': reason,
    }


def evaluate_pre_tool_use(
    payload: Dict[str, Any],
    workspace_root: Path,
    explicit_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Evaluate a `PreToolUse` hook event and transparently rewrite session shell commands
    to execute inside the session's Docker/Podman sandbox container via `session exec`.
    """
    is_claude_format = 'tool_name' in payload and 'toolCall' not in payload

    if is_running_in_container():
        return {}

    if is_claude_format:
        tool_name = payload.get('tool_name', '')
        if tool_name.lower() != 'bash':
            return {}
        tool_input = payload.get('tool_input') or {}
        command_line = tool_input.get('command', '')
        cwd = payload.get('cwd', '')
        conversation_id = payload.get('session_id', '')
        workspace_paths: List[str] = [cwd] if cwd else []
    else:
        tool_call = payload.get('toolCall') or {}
        tool_name = tool_call.get('name', '')
        if tool_name != 'run_command':
            return {}
        args = tool_call.get('args') or tool_call.get('arguments') or {}
        command_line = args.get('CommandLine', '')
        cwd = args.get('Cwd', '')
        conversation_id = payload.get('conversationId', '')
        workspace_paths = payload.get('workspacePaths') or []

    if not command_line:
        return {}

    ws_root = workspace_root.resolve()
    session_match = resolve_session_for_hook(
        workspace_root=ws_root,
        conversation_id=conversation_id,
        cwd=cwd,
        command_line=command_line,
        workspace_paths=workspace_paths,
        explicit_session_id=explicit_session_id,
    )

    # Allow host-control commands (`ros-maintainer-harness`, `agentapi`, `docker`) to pass through
    if is_host_passthrough_command(command_line):
        rewritten_cwd = None
        if cwd and (cwd == '/workspace' or cwd.startswith('/workspace/')):
            rewritten_cwd = str(session_match[1].resolve()) if session_match else str(ws_root)
        return _format_allow(is_claude_format, rewritten_cwd=rewritten_cwd)

    if session_match is None:
        deny_reason = is_forbidden_uncontainerized_host_command(command_line)
        if deny_reason:
            return _format_deny(is_claude_format, deny_reason)
        return {}

    session_id, session_dir, _ = session_match
    container_workdir = compute_container_workdir(cwd, session_dir)
    rewritten_cmd = build_session_exec_command(
        workspace_root=ws_root,
        session_id=session_id,
        container_workdir=container_workdir,
        command_line=command_line,
    )
    rewritten_cwd = str(session_dir.resolve())

    return _format_allow(
        is_claude_format,
        rewritten_cmd=rewritten_cmd,
        rewritten_cwd=rewritten_cwd,
    )


def get_harness_hook_command(
    workspace_root: Path,
    session_id: Optional[str] = None,
) -> str:
    """Build the shell command string for invoking the PreToolUse hook."""
    ws_root = workspace_root.resolve()
    harness_bin = get_harness_executable()
    cmd = f"{shlex.quote(harness_bin)} -w {shlex.quote(str(ws_root))} hook pre-tool-use"
    if session_id:
        cmd += f" --session {shlex.quote(session_id)}"
    return cmd


def _merge_hooks_json(hooks_path: Path, hook_cmd: str) -> Path:
    """Create or update a `hooks.json` file with the `ros-maintainer-container-exec` PreToolUse hook."""
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if hooks_path.exists():
        try:
            content = hooks_path.read_text(encoding='utf-8').strip()
            if content:
                loaded = json.loads(content)
                if isinstance(loaded, dict):
                    data = loaded
        except Exception:
            data = {}

    entry = {
        'matcher': 'run_command',
        'hooks': [
            {
                'type': 'command',
                'command': hook_cmd,
                'timeout': 120,
            }
        ],
    }

    data['ros-maintainer-container-exec'] = {
        'enabled': True,
        'PreToolUse': [entry],
    }
    hooks_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    return hooks_path


def _merge_claude_settings_hooks(settings_path: Path, hook_cmd: str) -> Path:
    """Create or update `.claude/settings.json` with the PreToolUse Bash container hook."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    if settings_path.exists():
        try:
            content = settings_path.read_text(encoding='utf-8').strip()
            if content:
                loaded = json.loads(content)
                if isinstance(loaded, dict):
                    data = loaded
        except Exception:
            data = {}

    hooks_section = data.setdefault('hooks', {})
    pre_tool_list = hooks_section.setdefault('PreToolUse', [])
    if not isinstance(pre_tool_list, list):
        pre_tool_list = []
        hooks_section['PreToolUse'] = pre_tool_list

    # Remove any prior ros-maintainer-harness hook entry before inserting the updated one
    filtered = [
        entry for entry in pre_tool_list
        if not (
            isinstance(entry, dict)
            and any(
                'hook pre-tool-use' in str(h.get('command', ''))
                for h in (entry.get('hooks') or [])
                if isinstance(h, dict)
            )
        )
    ]
    filtered.append({
        'matcher': 'Bash',
        'hooks': [
            {
                'type': 'command',
                'command': hook_cmd,
                'timeout': 120,
            }
        ],
    })
    hooks_section['PreToolUse'] = filtered
    settings_path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    return settings_path


def install_hooks_config(
    workspace_root: Path,
    target_dir: Optional[Path] = None,
    session_id: Optional[str] = None,
    include_global: bool = False,
) -> Dict[str, Path]:
    """
    Install `PreToolUse` container-routing hooks into `target_dir` (defaults to `workspace_root`)
    for both Antigravity/Gemini (`.agents/hooks.json`, `_agents/hooks.json`) and Claude Code
    (`.claude/settings.json`), and optionally into `~/.gemini/config/hooks.json`.
    """
    ws_root = workspace_root.resolve()
    base_dir = (target_dir or ws_root).resolve()
    hook_cmd = get_harness_hook_command(ws_root, session_id=session_id)

    written: Dict[str, Path] = {}
    written['antigravity'] = _merge_hooks_json(base_dir / '.agents' / 'hooks.json', hook_cmd)
    written['antigravity_alt'] = _merge_hooks_json(base_dir / '_agents' / 'hooks.json', hook_cmd)
    written['claude'] = _merge_claude_settings_hooks(base_dir / '.claude' / 'settings.json', hook_cmd)

    if include_global:
        try:
            global_hooks = Path.home() / '.gemini' / 'config' / 'hooks.json'
            global_cmd = get_harness_hook_command(ws_root, session_id=None)
            written['global'] = _merge_hooks_json(global_hooks, global_cmd)
        except Exception:
            pass

    return written
