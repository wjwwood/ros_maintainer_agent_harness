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

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from typing import Any, Dict, Iterator, List, Optional, Tuple


@dataclass(frozen=True)
class CallerIdentity:
    """Verified identity of a caller invoking an MCP gateway tool."""

    role: str
    session_id: Optional[str] = None
    token_id: Optional[str] = None

    @property
    def is_admin(self) -> bool:
        return self.role == 'admin'

    @property
    def is_hub(self) -> bool:
        return self.role == 'hub'

    @property
    def is_session(self) -> bool:
        return self.role.startswith('session:')


@dataclass(frozen=True)
class IssuedToken:
    """Result returned when the launch service issues a new bearer token."""

    token_id: str
    token: str
    role: str
    session_id: Optional[str]
    container_name: Optional[str]
    issued_at: str
    token_hash: str = ''


_ACTIVE_CALLER_VAR: contextvars.ContextVar[Optional[CallerIdentity]] = contextvars.ContextVar(
    'rmah_active_caller', default=None
)


def get_active_caller() -> Optional[CallerIdentity]:
    """Return the active CallerIdentity bound to the current execution context, if any."""
    return _ACTIVE_CALLER_VAR.get()


@contextmanager
def caller_context(caller: Optional[CallerIdentity]) -> Iterator[Optional[CallerIdentity]]:
    """Bind a CallerIdentity for the duration of a block."""
    tok = _ACTIVE_CALLER_VAR.set(caller)
    try:
        yield caller
    finally:
        _ACTIVE_CALLER_VAR.reset(tok)


@contextmanager
def token_caller_context(
    raw_token: Optional[str],
    token_store: 'TokenStore',
) -> Iterator[Optional[CallerIdentity]]:
    """Verify `raw_token` against `token_store` and bind the resulting CallerIdentity."""
    caller = token_store.verify_token(raw_token)
    with caller_context(caller):
        yield caller


def get_default_state_dir() -> Path:
    """
    Return the host-only state directory outside the mounted workspace tree.
    Honors $ROS_MAINTAINER_STATE_DIR if set, otherwise defaults to
    ~/.local/state/ros_maintainer_agent_harness.
    """
    override = (os.environ.get('ROS_MAINTAINER_STATE_DIR') or '').strip()
    if override:
        return Path(override).expanduser().resolve()
    xdg_state = (os.environ.get('XDG_STATE_HOME') or '').strip()
    if xdg_state:
        base = Path(xdg_state).expanduser().resolve()
    else:
        try:
            home_dir = Path.home()
        except (RuntimeError, OSError):
            import tempfile
            home_dir = Path(tempfile.gettempdir())
        base = (home_dir / '.local' / 'state').resolve()
    return base / 'ros_maintainer_agent_harness'


def ensure_private_dir(path: Path) -> Path:
    """Create a directory with 0700 permissions."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


class TokenStore:
    """
    Manages per-container bearer tokens in `<state_dir>/tokens.json` outside the workspace.
    Only SHA-256 hashes of tokens are persisted to disk.
    """

    def __init__(self, state_dir: Optional[Path] = None):
        self.state_dir = ensure_private_dir((state_dir or get_default_state_dir()).resolve())
        self.tokens_file = self.state_dir / 'tokens.json'

    def _load_records(self) -> List[Dict[str, Any]]:
        if not self.tokens_file.exists():
            return []
        try:
            data = json.loads(self.tokens_file.read_text(encoding='utf-8'))
            if isinstance(data, dict) and isinstance(data.get('tokens'), list):
                return list(data['tokens'])
        except Exception:
            pass
        return []

    def _save_records(self, records: List[Dict[str, Any]]) -> None:
        ensure_private_dir(self.state_dir)
        tmp_file = self.tokens_file.with_suffix('.json.tmp')
        payload = {'version': 1, 'tokens': records}
        tmp_file.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        try:
            tmp_file.chmod(0o600)
        except OSError:
            pass
        tmp_file.replace(self.tokens_file)
        try:
            self.tokens_file.chmod(0o600)
        except OSError:
            pass

    def issue_token(
        self,
        role: str,
        session_id: Optional[str] = None,
        container_name: Optional[str] = None,
        replace_existing: bool = True,
    ) -> IssuedToken:
        """
        Issue a new random bearer token for `role` ('hub', 'session:<id>', or 'admin').
        If `replace_existing` is True, any prior active tokens for the same role or session are revoked.
        """
        if role.startswith('session:'):
            inferred_sid = role.split(':', 1)[1].strip()
            if not session_id:
                session_id = inferred_sid
        elif session_id and role == 'session':
            role = f"session:{session_id}"

        records = self._load_records()
        now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        if replace_existing:
            for rec in records:
                if rec.get('revoked'):
                    continue
                same_role = (rec.get('role') == role and role in ('hub', 'admin'))
                same_session = bool(session_id and rec.get('session_id') == session_id)
                same_container = bool(container_name and rec.get('container_name') == container_name)
                if same_role or same_session or same_container:
                    rec['revoked'] = True
                    rec['revoked_at'] = now_iso

        token_id = f"tok-{secrets.token_hex(4)}"
        raw_token = f"rmah_tok_{secrets.token_urlsafe(32)}"
        token_hash = _hash_token(raw_token)

        records.append({
            'token_id': token_id,
            'token_hash': token_hash,
            'role': role,
            'session_id': session_id,
            'container_name': container_name,
            'issued_at': now_iso,
            'revoked': False,
        })
        self._save_records(records)

        return IssuedToken(
            token_id=token_id,
            token=raw_token,
            role=role,
            session_id=session_id,
            container_name=container_name,
            issued_at=now_iso,
            token_hash=token_hash,
        )

    def verify_token(self, raw_token: Optional[str]) -> Optional[CallerIdentity]:
        """Verify a raw bearer token (or 'Bearer <token>' header) and return CallerIdentity if valid."""
        if not raw_token or not isinstance(raw_token, str):
            return None
        cleaned = raw_token.strip()
        if cleaned.lower().startswith('bearer '):
            cleaned = cleaned[7:].strip()
        if not cleaned:
            return None

        candidate_hash = _hash_token(cleaned)
        for rec in self._load_records():
            if rec.get('revoked'):
                continue
            stored_hash = str(rec.get('token_hash') or '')
            if stored_hash and hmac.compare_digest(stored_hash, candidate_hash):
                role = str(rec.get('role') or '')
                sid = rec.get('session_id')
                if not sid and role.startswith('session:'):
                    sid = role.split(':', 1)[1].strip()
                return CallerIdentity(
                    role=role,
                    session_id=sid,
                    token_id=str(rec.get('token_id') or ''),
                )
        return None

    def revoke_token_by_id(self, token_id: str) -> bool:
        records = self._load_records()
        now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        changed = False
        for rec in records:
            if rec.get('token_id') == token_id and not rec.get('revoked'):
                rec['revoked'] = True
                rec['revoked_at'] = now_iso
                changed = True
        if changed:
            self._save_records(records)
        return changed

    def revoke_tokens_for_session(self, session_id: str) -> int:
        records = self._load_records()
        now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        count = 0
        target_role = f"session:{session_id}"
        for rec in records:
            if not rec.get('revoked') and (
                rec.get('session_id') == session_id or rec.get('role') == target_role
            ):
                rec['revoked'] = True
                rec['revoked_at'] = now_iso
                count += 1
        if count:
            self._save_records(records)
        return count

    def revoke_tokens_for_role(self, role: str) -> int:
        records = self._load_records()
        now_iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        count = 0
        for rec in records:
            if not rec.get('revoked') and rec.get('role') == role:
                rec['revoked'] = True
                rec['revoked_at'] = now_iso
                count += 1
        if count:
            self._save_records(records)
        return count

    def list_active_tokens(self) -> List[Dict[str, Any]]:
        """Return non-revoked token metadata (excluding token hashes)."""
        active = []
        for rec in self._load_records():
            if not rec.get('revoked'):
                active.append({
                    'token_id': rec.get('token_id'),
                    'role': rec.get('role'),
                    'session_id': rec.get('session_id'),
                    'container_name': rec.get('container_name'),
                    'issued_at': rec.get('issued_at'),
                })
        return active


# Tool categories for role-based access control
HUB_AND_ADMIN_TOOLS = frozenset({
    'check_environment',
    'get_workspace_status',
    'get_next_actions',
    'list_sessions',
    'list_approval_requests',
    'scaffold_session_from_pr',
    'create_session',
    'prune_session',
    'start_session_container',
    'stop_session_container',
    'start_session_conversation',
    'generate_mcp_config',
    'get_session_launch_info',
})

SESSION_MUTATION_TOOLS = frozenset({
    'git_push',
    'create_pull_request',
    'edit_pull_request',
})

RELEASE_MUTATION_TOOLS = frozenset({
    'push_release',
    'run_bloom_release',
})

SHARED_SESSION_AND_HUB_TOOLS = frozenset({
    'log_status',
    'update_session_status',
    'get_ci_status',
    'get_ci_summary',
    'list_ci_runs',
    'find_restarted_ci',
    'launch_jenkins_ci',
    'cancel_ci_run',
    'get_maintainer_rules',
    'check_policy',
    'get_session_attach_info',
})

HUB_SESSION_OWNERSHIP_CHECK_TOOLS = frozenset({
    'update_session_status',
    'log_status',
    'exec_in_session',
    'stop_session_container',
    'prune_session',
    'launch_jenkins_ci',
    'find_restarted_ci',
    'get_ci_status',
    'list_ci_runs',
    'cancel_ci_run',
    'list_approval_requests',
})


def authorize_tool_call(
    caller: Optional[CallerIdentity],
    tool_name: str,
    bound_args: Dict[str, Any],
    workspace_root: Path,
    allow_hub_exec_in_session: bool = True,
    allow_session_release_tools: bool = False,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Authorize `caller` to invoke `tool_name` with `bound_args`.

    Returns:
        (allowed: bool, error_message: str, arg_overrides: Dict[str, Any])
    """
    if caller is None:
        return (False, 'Authentication required: missing or invalid bearer token.', {})

    if caller.is_admin:
        return (True, '', {})

    overrides: Dict[str, Any] = {}

    if caller.is_hub:
        if tool_name in SESSION_MUTATION_TOOLS or tool_name in RELEASE_MUTATION_TOOLS:
            return (
                False,
                f"Role 'hub' is not permitted to call session mutation tool '{tool_name}'.",
                {},
            )
        if tool_name == 'exec_in_session' and not allow_hub_exec_in_session:
            return (
                False,
                "Role 'hub' is not permitted to call 'exec_in_session' by policy (allow_hub_exec_in_session=false).",
                {},
            )
        if (
            tool_name not in HUB_AND_ADMIN_TOOLS
            and tool_name not in SHARED_SESSION_AND_HUB_TOOLS
            and tool_name != 'exec_in_session'
        ):
            return (
                False,
                f"Role 'hub' is not authorized to call tool '{tool_name}'.",
                {},
            )

        # Verify hub cannot call session-scoped tools on behalf of a session it did not start
        target_sid = bound_args.get('session_id')
        if tool_name in HUB_SESSION_OWNERSHIP_CHECK_TOOLS and target_sid:
            session_meta_file = workspace_root.resolve() / 'sessions' / str(target_sid) / 'session.json'
            started_by_hub = False
            if session_meta_file.is_file():
                try:
                    meta = json.loads(session_meta_file.read_text(encoding='utf-8'))
                    if isinstance(meta, dict):
                        started_by_hub = bool(meta.get('started_by_hub'))
                except Exception:
                    started_by_hub = False
            if not started_by_hub:
                return (
                    False,
                    (
                        f"Role 'hub' cannot call session-scoped tool '{tool_name}' "
                        f"on behalf of session '{target_sid}' that it did not start."
                    ),
                    {},
                )
        return (True, '', overrides)

    if caller.is_session:
        bound_sid = caller.session_id or caller.role.split(':', 1)[1].strip()
        if not bound_sid:
            return (False, f"Invalid session role '{caller.role}'.", {})

        if tool_name in HUB_AND_ADMIN_TOOLS or tool_name == 'exec_in_session':
            return (
                False,
                f"Role '{caller.role}' is not permitted to call hub/admin tool '{tool_name}'.",
                {},
            )

        if tool_name in RELEASE_MUTATION_TOOLS and not allow_session_release_tools:
            return (
                False,
                (
                    f"Role '{caller.role}' is not permitted to call release tool '{tool_name}' "
                    "unless enabled in policy.yaml (server.allow_session_release_tools: true)."
                ),
                {},
            )

        if (
            tool_name not in SESSION_MUTATION_TOOLS
            and tool_name not in RELEASE_MUTATION_TOOLS
            and tool_name not in SHARED_SESSION_AND_HUB_TOOLS
        ):
            return (
                False,
                f"Role '{caller.role}' is not authorized to call tool '{tool_name}'.",
                {},
            )

        # Enforce session_id parameter matches the token's bound session_id (ignoring env claims)
        if 'session_id' in bound_args:
            provided_sid = bound_args.get('session_id')
            if provided_sid is not None and str(provided_sid).strip() != '':
                if str(provided_sid).strip() != bound_sid:
                    return (
                        False,
                        (
                            f"Role '{caller.role}' cannot act on another session "
                            f"(requested session_id='{provided_sid}', token is bound to '{bound_sid}')."
                        ),
                        {},
                    )
            overrides['session_id'] = bound_sid

        # Enforce repo_path (if given) stays within this session's directory
        if 'repo_path' in bound_args and bound_args.get('repo_path'):
            raw_repo_path = str(bound_args['repo_path']).strip()
            if raw_repo_path and not raw_repo_path.startswith('/workspace'):
                session_root = (workspace_root.resolve() / 'sessions' / bound_sid).resolve()
                cand = Path(raw_repo_path).expanduser()
                if not cand.is_absolute():
                    cand_resolved = (session_root / cand).resolve()
                else:
                    cand_resolved = cand.resolve()
                try:
                    cand_resolved.relative_to(session_root)
                except ValueError:
                    return (
                        False,
                        (
                            f"Role '{caller.role}' cannot access repository path '{raw_repo_path}' "
                            f"outside its own session directory '{session_root}'."
                        ),
                        {},
                    )

        return (True, '', overrides)

    return (False, f"Unrecognized caller role '{caller.role}'.", {})
