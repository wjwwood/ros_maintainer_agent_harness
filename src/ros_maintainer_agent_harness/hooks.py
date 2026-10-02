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
    'rmah-session-exec',
    'agentapi',
    'export',
    'true',
    'false',
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


def get_session_exec_executable() -> str:
    """Return the shortest valid command to invoke `rmah-session-exec` on the host."""
    if shutil.which('rmah-session-exec'):
        return 'rmah-session-exec'
    local_bin = Path.home() / '.local' / 'bin' / 'rmah-session-exec'
    if local_bin.exists():
        return '~/.local/bin/rmah-session-exec'
    return 'rmah-session-exec'


def ensure_agent_bin_symlinks() -> Dict[str, Path]:
    """
    Symlink `ros-maintainer-harness` and `rmah-session-exec` into any existing
    `~/.gemini/*/bin` directory so they are on the default non-interactive agent `$PATH`.
    """
    created: Dict[str, Path] = {}
    try:
        gemini_dir = Path.home() / '.gemini'
        if not gemini_dir.is_dir():
            return created
        local_bin_dir = Path.home() / '.local' / 'bin'
        for bin_dir in sorted(gemini_dir.glob('*/bin')):
            if not bin_dir.is_dir() or not os.access(bin_dir, os.W_OK):
                continue
            for exe_name in ('ros-maintainer-harness', 'rmah-session-exec'):
                src_exe = local_bin_dir / exe_name
                if not src_exe.exists():
                    which_path = shutil.which(exe_name)
                    if which_path:
                        src_exe = Path(which_path).resolve()
                if not src_exe.exists():
                    continue
                dest_link = bin_dir / exe_name
                if dest_link.resolve() == src_exe.resolve() and dest_link.exists():
                    created[f"{bin_dir.parent.name}:{exe_name}"] = dest_link
                    continue
                if dest_link.exists() or dest_link.is_symlink():
                    dest_link.unlink()
                dest_link.symlink_to(src_exe)
                created[f"{bin_dir.parent.name}:{exe_name}"] = dest_link
    except Exception:
        pass
    return created


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
    """Extract the binary basename from a single shell command segment, stripping common wrapper prefixes."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        tokens = segment.strip().split()
    idx = 0
    wrapper_mode: Optional[str] = None
    while idx < len(tokens):
        tok = tokens[idx]
        idx += 1
        # Skip env assignments like PATH=... or FOO=bar
        if '=' in tok and not tok.startswith('-') and not tok.startswith('/'):
            continue
        base_tok = Path(tok.replace('\\', '/')).name
        if base_tok.lower().endswith('.exe'):
            base_tok = base_tok[:-4]

        if wrapper_mode == 'env':
            if tok.startswith('-'):
                if tok in ('-u', '--unset', '-C', '--chdir', '-S', '--split-string') and idx < len(tokens):
                    idx += 1
                continue
            wrapper_mode = None
        elif wrapper_mode == 'sudo':
            if tok.startswith('-'):
                if tok in ('-u', '--user', '-g', '--group', '-C', '-D', '-h', '-p', '-r', '-t') and idx < len(tokens):
                    idx += 1
                continue
            wrapper_mode = None
        elif wrapper_mode == 'timeout':
            if tok.startswith('-'):
                if tok in ('-k', '--kill-after', '-s', '--signal') and idx < len(tokens):
                    idx += 1
                continue
            # First non-option token after timeout is the duration (e.g. '5' or '10s')
            wrapper_mode = None
            continue
        elif wrapper_mode == 'nice':
            if tok.startswith('-'):
                if tok in ('-n', '--adjustment') and idx < len(tokens):
                    idx += 1
                continue
            wrapper_mode = None

        if base_tok in ('env', 'sudo', 'timeout', 'nice'):
            wrapper_mode = base_tok
            continue
        if base_tok in ('command', 'nohup', 'exec', 'builtin', 'time', 'stdbuf', 'ionice'):
            while idx < len(tokens) and tokens[idx].startswith('-'):
                idx += 1
            continue
        return base_tok
    return None


def _is_single_heredoc_session_exec(stripped: str) -> bool:
    """Return True if `stripped` is a single `rmah-session-exec <<'EOF'\\n...\\nEOF` invocation."""
    lines = stripped.split('\n')
    if len(lines) < 2:
        return False
    first_line = lines[0].strip()
    last_line = lines[-1].strip()
    if last_line != 'EOF':
        return False
    if not first_line.endswith("<<'EOF'") and not first_line.endswith('<<"EOF"'):
        return False
    header_without_heredoc = first_line[:first_line.rfind('<<')].strip()
    header_segs = _split_top_level_segments(header_without_heredoc, include_pipe=True)
    if len(header_segs) != 1:
        return False
    return _first_executable_in_segment(header_segs[0]) == 'rmah-session-exec'


def _split_top_level_segments(command_line: str, include_pipe: bool = False) -> List[str]:
    """
    Split a shell command string on top-level `&&`, `||`, `;`, or `\n`
    (and optionally `|`) while ignoring separators inside single (`'...'`),
    ANSI-C (`$'...'`), or double (`"..."`) quotes.
    """
    segments: List[str] = []
    buf: List[str] = []
    in_single = False
    in_ansi_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(command_line)

    while i < n:
        ch = command_line[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue

        if ch == '\\' and not in_single:
            escaped = True
            buf.append(ch)
            i += 1
            continue

        if not in_single and not in_ansi_single and not in_double and command_line.startswith("$'", i):
            in_ansi_single = True
            buf.append("$'")
            i += 2
            continue

        if ch == "'" and not in_double:
            if in_ansi_single:
                in_ansi_single = False
            elif not in_single:
                in_single = True
            else:
                in_single = False
            buf.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single and not in_ansi_single:
            in_double = not in_double
            buf.append(ch)
            i += 1
            continue

        if not in_single and not in_ansi_single and not in_double:
            if command_line.startswith('&&', i) or command_line.startswith('||', i):
                seg = ''.join(buf).strip()
                if seg:
                    segments.append(seg)
                buf = []
                i += 2
                continue
            if ch in (';', '\n') or (include_pipe and ch == '|'):
                seg = ''.join(buf).strip()
                if seg:
                    segments.append(seg)
                buf = []
                i += 1
                continue

        buf.append(ch)
        i += 1

    tail = ''.join(buf).strip()
    if tail:
        segments.append(tail)
    return segments


def _contains_shell_substitution(command_line: str) -> bool:
    """
    Return True if `command_line` contains `$(`, backticks, or process substitution
    outside single quotes (`'...'` and ANSI-C `$'...'`).
    """
    in_single = False
    in_ansi_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(command_line)
    while i < n:
        ch = command_line[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == '\\' and not in_single:
            escaped = True
            i += 1
            continue
        if not in_single and not in_ansi_single and not in_double and command_line.startswith("$'", i):
            in_ansi_single = True
            i += 2
            continue
        if ch == "'" and not in_double:
            if in_ansi_single:
                in_ansi_single = False
            elif not in_single:
                in_single = True
            else:
                in_single = False
            i += 1
            continue
        if ch == '"' and not in_single and not in_ansi_single:
            in_double = not in_double
            i += 1
            continue
        if not in_single and not in_ansi_single:
            if ch == '`':
                return True
            if command_line.startswith('$(', i):
                return True
            if not in_double and (command_line.startswith('<(', i) or command_line.startswith('>(', i)):
                return True
        i += 1
    return False


def is_host_passthrough_command(command_line: str) -> bool:
    """
    Return True if `command_line` is purely a host-control command
    (`ros-maintainer-harness`, `rmah-session-exec`, `agentapi`) that should run on the host
    without being wrapped into `rmah-session-exec`.
    """
    stripped = (command_line or '').strip()
    if not stripped:
        return True

    if _is_single_heredoc_session_exec(stripped):
        return True

    if _contains_shell_substitution(stripped):
        return False

    segments = _split_top_level_segments(stripped, include_pipe=True)
    if not segments:
        return True

    saw_control_bin = False
    for seg in segments:
        exe = _first_executable_in_segment(seg)
        if not exe:
            continue
        if exe not in HOST_PASSTHROUGH_BINARIES:
            return False
        if exe in ('ros-maintainer-harness', 'rmah-session-exec', 'agentapi'):
            saw_control_bin = True

    return saw_control_bin


def is_forbidden_session_command(command_line: str, session_id: str) -> Optional[str]:
    """
    Check if a command inside a maintainer session is attempting to invoke `docker`/`podman` directly,
    extract host credentials (`gh auth token`), or bypass the harness (`ci_for_pr.py`).
    """
    stripped = (command_line or '').strip()
    if not stripped:
        return None

    if _is_single_heredoc_session_exec(stripped):
        return None

    segments = _split_top_level_segments(stripped, include_pipe=True)
    if len(segments) == 1:
        only_exe = _first_executable_in_segment(segments[0])
        if only_exe == 'rmah-session-exec' and not _contains_shell_substitution(segments[0]):
            return None
        if (
            only_exe == 'ros-maintainer-harness'
            and 'session exec' in segments[0]
            and not _contains_shell_substitution(segments[0])
        ):
            return None

    first_exe = _first_executable_in_segment(stripped)
    if first_exe == 'ros-maintainer-harness' and _contains_shell_substitution(stripped):
        return (
            "Unquoted shell command substitution ($(...) or backticks inside double quotes) "
            "is not permitted in host 'ros-maintainer-harness' commands. "
            "Wrap arguments containing markdown backticks in single quotes ('...' or $'...') "
            "or pass --body-file <path>."
        )

    for seg in segments:
        exe = _first_executable_in_segment(seg)
        if exe in ('docker', 'podman'):
            return (
                f"Direct '{exe}' invocation is disabled in session '{session_id}'. "
                f"Your shell commands are automatically routed into container 'ros-harness-{session_id}' "
                "by the PreToolUse hook. Run 'colcon', 'pytest', 'git', or 'gh' directly without 'docker', "
                "or use 'ros-maintainer-harness' CLI / MCP tools. If a tool or container fails, "
                "STOP immediately and ask the user for help instead of trying to bypass or debug it."
            )
        if exe in ('bloom-release', 'git-bloom-release'):
            return (
                f"Direct '{exe}' invocation inside container 'ros-harness-{session_id}' is disabled "
                "because containers do not hold release push or rosdistro credentials. "
                f"Use 'ros-maintainer-harness release bloom <repository> -s {session_id} --rosdistro <distro> "
                "-m \"...\"' or the 'run_bloom_release' MCP tool to run bloom-release on the host "
                "with maintainer approval."
            )
        if exe == 'catkin_prepare_release':
            if '--no-push' not in seg and '-h' not in seg and '--help' not in seg:
                return (
                    f"Running 'catkin_prepare_release' without '--no-push' in session '{session_id}' is disabled "
                    "because the session container does not hold git push credentials. "
                    "Run 'catkin_prepare_release --no-push ...' inside the container to create the local release "
                    f"commit and tag, then use 'ros-maintainer-harness release push -s {session_id} "
                    "-b <distro_branch> -t <tag> -m \"...\"' (or MCP 'push_release') to push the release "
                    "commit and tag from the host."
                )

    lower_cmd = stripped.lower()
    if 'ci_for_pr.py' in lower_cmd or 'ros-github-scripts' in lower_cmd:
        return (
            f"Direct invocation of 'ci_for_pr.py' or external scripts is disabled in session '{session_id}'. "
            f"Use 'ros-maintainer-harness ci launch -s {session_id} --comment -m \"Run CI\"' or the "
            "'launch_jenkins_ci' MCP tool. If CI launch fails, STOP immediately and ask the user for help."
        )
    if 'gh auth token' in lower_cmd:
        return (
            f"Reading host 'gh auth token' is prohibited in session '{session_id}'. "
            "Use the MCP gateway tools ('launch_jenkins_ci', 'git_push', 'create_pull_request') for "
            "authenticated host operations. If an MCP tool or CLI command fails, STOP immediately "
            "and ask the user for help."
        )

    return None


def is_forbidden_session_file_read(file_path: str, session_id: str) -> Optional[str]:
    """
    Check if a session agent is attempting to read harness source code, MCP/hook configs,
    external CI scripts, or conversation transcripts to debug or circumvent the harness.
    """
    if not file_path:
        return None
    norm = file_path.replace('\\', '/')
    basename = Path(norm).name

    # Allow legitimate memory files under ~/memory/... or /home/<user>/memory/... (provided no '..' traversal)
    blocked_mem_names = ('mcp_config.json', 'hooks.json', 'transcript.jsonl', 'transcript_full.jsonl')
    if '..' not in norm.split('/') and basename not in blocked_mem_names:
        home_mem = str(Path.home() / 'memory').replace('\\', '/') + '/'
        if norm.startswith(home_mem) or re.match(r'^/(?:home|Users|usr/local/google/home)/[^/]+/memory/', norm):
            return None

    forbidden_Substrings = (
        'ros_maintainer_agent_harness',
        'ros-maintainer-agent-harness',
        'ros-github-scripts',
        'ci_for_pr.py',
        'transcript.jsonl',
        'transcript_full.jsonl',
    )
    forbidden_basenames = (
        'mcp_config.json',
        'hooks.json',
    )
    if any(sub in norm for sub in forbidden_Substrings) or basename in forbidden_basenames:
        return (
            f"Access to '{basename}' is disabled in maintainer session '{session_id}'. "
            "Do NOT read or debug harness source code, MCP server internals, hook configurations, "
            "or conversation transcripts. If an MCP tool or CLI command returned an error, "
            "STOP immediately and ask the user for help with the error message."
        )
    return None


def _rewrite_bare_harness_passthrough(command_line: str) -> Optional[str]:
    """
    If `ros-maintainer-harness` is not on the current `$PATH` but exists in `~/.local/bin`,
    prepend `export PATH="$HOME/.local/bin:$PATH"; ` to bare `ros-maintainer-harness` host
    commands so non-interactive agent shells do not fail with exit code 127.
    """
    if 'session exec' in command_line or 'rmah-session-exec' in command_line or '.local/bin' in command_line:
        return None
    if shutil.which('ros-maintainer-harness') is not None:
        return None
    if get_harness_executable() == 'ros-maintainer-harness':
        return None
    segments = _split_top_level_segments(command_line, include_pipe=True)
    for seg in segments:
        if _first_executable_in_segment(seg) == 'ros-maintainer-harness':
            return f'export PATH="$HOME/.local/bin:$PATH"; {command_line}'
    return None


def is_forbidden_uncontainerized_host_command(command_line: str) -> Optional[str]:
    """
    Check if a command running outside any session is attempting to run `colcon` or `rosdep`
    directly on the host OS.
    """
    stripped = (command_line or '').strip()
    if not stripped or is_host_passthrough_command(stripped):
        return None

    segments = _split_top_level_segments(stripped, include_pipe=True)
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


def compute_host_session_cwd(cwd: str, session_dir: Path) -> Tuple[str, bool]:
    """
    Map a tool call's `cwd` to a valid existing directory on the host inside `session_dir`.
    Returns `(host_cwd, needs_explicit_workdir_flag)`.
    """
    resolved_session = session_dir.resolve()
    if not cwd:
        return str(resolved_session), False

    if cwd == '/workspace' or cwd.startswith('/workspace/'):
        rel_str = cwd[len('/workspace'):].lstrip('/')
        if not rel_str:
            return str(resolved_session), False
        candidate = (resolved_session / rel_str).resolve()
        if candidate.is_dir() and (candidate == resolved_session or resolved_session in candidate.parents):
            return str(candidate), False
        return str(resolved_session), True

    try:
        resolved_cwd = Path(cwd).expanduser().resolve()
        if resolved_cwd == resolved_session or resolved_session in resolved_cwd.parents:
            if resolved_cwd.is_dir():
                return str(resolved_cwd), False
            return str(resolved_session), True
    except Exception:
        pass

    return str(resolved_session), False


def _choose_heredoc_delimiter(command_line: str) -> str:
    """Choose a heredoc delimiter that does not collide with any line in `command_line`."""
    lines = {line.strip() for line in command_line.splitlines()}
    for candidate in ('EOF', '__EOF__', '__RMAH_EOF__'):
        if candidate not in lines:
            return candidate
    idx = 1
    while f'__RMAH_EOF_{idx}__' in lines:
        idx += 1
    return f'__RMAH_EOF_{idx}__'


def build_session_exec_command(
    workspace_root: Path,
    session_id: str,
    container_workdir: str,
    command_line: str,
    explicit_workdir: bool = False,
) -> str:
    """
    Wrap `command_line` in a concise `rmah-session-exec` invocation.
    Because the host process `Cwd` is already inside `sessions/<session_id>[/<subdir>]`,
    `rmah-session-exec` infers workspace root, session ID, and container workdir automatically.
    """
    exec_bin = get_session_exec_executable()
    prefix = exec_bin
    if explicit_workdir and container_workdir and container_workdir != '/workspace':
        prefix += f" -d {shlex.quote(container_workdir)}"

    if '\n' in command_line or "'" in command_line:
        delim = _choose_heredoc_delimiter(command_line)
        body = command_line if command_line.endswith('\n') else command_line + '\n'
        return f"{prefix} <<'{delim}'\n{body}{delim}"

    return f"{prefix} {shlex.quote(command_line)}"


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
    to execute inside the session's Docker/Podman sandbox container via `rmah-session-exec`,
    while blocking session attempts to circumvent the container or debug harness internals.
    """
    is_claude_format = 'tool_name' in payload and 'toolCall' not in payload

    if is_running_in_container():
        return {}

    ws_root = workspace_root.resolve()

    if is_claude_format:
        tool_name = payload.get('tool_name', '')
        tool_input = payload.get('tool_input') or {}
        cwd = payload.get('cwd', '')
        conversation_id = payload.get('session_id', '')
        workspace_paths: List[str] = [cwd] if cwd else []
        if tool_name.lower() == 'read':
            file_path = tool_input.get('file_path', '')
            session_match = resolve_session_for_hook(
                workspace_root=ws_root,
                conversation_id=conversation_id,
                cwd=cwd,
                workspace_paths=workspace_paths,
                explicit_session_id=explicit_session_id,
            )
            if session_match is not None:
                deny_msg = is_forbidden_session_file_read(file_path, session_match[0])
                if deny_msg:
                    return _format_deny(is_claude_format, deny_msg)
            return {}
        if tool_name.lower() != 'bash':
            return {}
        command_line = tool_input.get('command', '')
    else:
        tool_call = payload.get('toolCall') or {}
        tool_name = tool_call.get('name', '')
        args = tool_call.get('args') or tool_call.get('arguments') or {}
        conversation_id = payload.get('conversationId', '')
        workspace_paths = payload.get('workspacePaths') or []
        if tool_name == 'view_file':
            file_path = args.get('AbsolutePath', '')
            session_match = resolve_session_for_hook(
                workspace_root=ws_root,
                conversation_id=conversation_id,
                cwd='',
                workspace_paths=workspace_paths,
                explicit_session_id=explicit_session_id,
            )
            if session_match is not None:
                deny_msg = is_forbidden_session_file_read(file_path, session_match[0])
                if deny_msg:
                    return _format_deny(is_claude_format, deny_msg)
            return {}
        if tool_name != 'run_command':
            return {}
        command_line = args.get('CommandLine', '')
        cwd = args.get('Cwd', '')

    if not command_line:
        return {}

    session_match = resolve_session_for_hook(
        workspace_root=ws_root,
        conversation_id=conversation_id,
        cwd=cwd,
        command_line=command_line,
        workspace_paths=workspace_paths,
        explicit_session_id=explicit_session_id,
    )

    if session_match is not None:
        session_deny = is_forbidden_session_command(command_line, session_match[0])
        if session_deny:
            return _format_deny(is_claude_format, session_deny)

    # Allow host-control commands (`ros-maintainer-harness`, `rmah-session-exec`, `agentapi`) to pass through
    if is_host_passthrough_command(command_line):
        rewritten_cmd = _rewrite_bare_harness_passthrough(command_line)
        rewritten_cwd = None
        if cwd and (cwd == '/workspace' or cwd.startswith('/workspace/')):
            rewritten_cwd = str(session_match[1].resolve()) if session_match else str(ws_root)
        return _format_allow(
            is_claude_format,
            rewritten_cmd=rewritten_cmd,
            rewritten_cwd=rewritten_cwd,
        )

    if session_match is None:
        deny_reason = is_forbidden_uncontainerized_host_command(command_line)
        if deny_reason:
            return _format_deny(is_claude_format, deny_reason)
        return {}

    session_id, session_dir, _ = session_match
    container_workdir = compute_container_workdir(cwd, session_dir)
    rewritten_cwd, needs_explicit_workdir = compute_host_session_cwd(cwd, session_dir)
    if is_claude_format and container_workdir != '/workspace':
        # Claude Code PreToolUse updatedInput only rewrites `command`, not `cwd`,
        # so include `-d` when `cwd` is not already inside `session_dir`
        if not cwd or Path(cwd).expanduser().resolve() != Path(rewritten_cwd):
            needs_explicit_workdir = True

    rewritten_cmd = build_session_exec_command(
        workspace_root=ws_root,
        session_id=session_id,
        container_workdir=container_workdir,
        command_line=command_line,
        explicit_workdir=needs_explicit_workdir,
    )

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
        'matcher': 'run_command|view_file',
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
    """Create or update `.claude/settings.json` with the PreToolUse Bash/Read container hook."""
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
        'matcher': 'Bash|Read',
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
    for both Antigravity/Gemini (`.agents/hooks.json`) and Claude Code (`.claude/settings.json`),
    and optionally into `~/.gemini/config/hooks.json`.
    """
    ws_root = workspace_root.resolve()
    base_dir = (target_dir or ws_root).resolve()
    if include_global:
        ensure_agent_bin_symlinks()
    hook_cmd = get_harness_hook_command(ws_root, session_id=session_id)

    # Remove duplicate _agents/hooks.json if present so hooks do not fire twice per directory
    alt_hooks = base_dir / '_agents' / 'hooks.json'
    if alt_hooks.exists():
        try:
            alt_hooks.unlink()
            if not any(alt_hooks.parent.iterdir()):
                alt_hooks.parent.rmdir()
        except Exception:
            pass

    written: Dict[str, Path] = {}
    written['antigravity'] = _merge_hooks_json(base_dir / '.agents' / 'hooks.json', hook_cmd)
    written['claude'] = _merge_claude_settings_hooks(base_dir / '.claude' / 'settings.json', hook_cmd)

    if include_global:
        try:
            global_hooks = Path.home() / '.gemini' / 'config' / 'hooks.json'
            global_cmd = get_harness_hook_command(ws_root, session_id=None)
            written['global'] = _merge_hooks_json(global_hooks, global_cmd)
        except Exception:
            pass

    return written
