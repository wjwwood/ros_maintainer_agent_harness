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
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from .devcontainer import stop_session_container, write_devcontainer_config
from .instructions import write_session_agent_instructions
from .mcp_config import write_session_mcp_configs
from .timeline import TimelineLogger
from .workspace import WorkspaceLayout

SESSION_ID_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
SUBFOLDER_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_.-]+$')


def _rmtree_force(target: Path) -> None:
    def _handle_readonly(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        except Exception:
            pass

    shutil.rmtree(target, onerror=_handle_readonly)


def validate_session_id(session_id: str) -> str:
    """Validate that session_id is 1-64 alphanumeric/hyphen/underscore chars with no path traversal."""
    if (
        not session_id
        or '..' in session_id
        or '/' in session_id
        or '\\' in session_id
        or not SESSION_ID_PATTERN.match(session_id)
    ):
        raise ValueError(
            f"Invalid session_id '{session_id}': "
            "must be 1-64 characters and consist only of alphanumeric characters, hyphens, and underscores."
        )
    return session_id


def get_session_metadata_path(session_dir: Path) -> Path:
    return session_dir / 'session.json'


def read_session_metadata(session_dir: Path) -> Dict[str, Any]:
    """Read structured session metadata from <session_dir>/session.json with fallback inference."""
    meta_path = get_session_metadata_path(session_dir)
    data: Dict[str, Any] = {}
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    session_id = session_dir.name
    data.setdefault('session_id', session_id)
    data.setdefault('status', 'active')
    data.setdefault('topic', None)
    data.setdefault('distro', 'rolling')
    data.setdefault('pr_ref', None)
    data.setdefault('pr_url', None)
    data.setdefault('pr_title', None)
    data.setdefault('pr_author', None)
    data.setdefault('conversation_id', None)
    data.setdefault('hub_conversation_id', None)
    data.setdefault('updated_at', None)

    dc_path = session_dir / '.devcontainer' / 'devcontainer.json'
    if dc_path.exists():
        try:
            dc = json.loads(dc_path.read_text(encoding='utf-8'))
            dc_distro = dc.get('containerEnv', {}).get('ROS_DISTRO')
            if dc_distro:
                data['distro'] = dc_distro
        except Exception:
            pass

    task_path = session_dir / 'TASK.md'
    if task_path.exists() and not data.get('pr_url'):
        try:
            task_text = task_path.read_text(encoding='utf-8')
            m_pr = re.search(r'\*\*Pull Request\*\*:\s*\[([^\]]+)\]\(([^)]+)\)', task_text)
            if m_pr:
                data['pr_ref'] = m_pr.group(1).strip()
                data['pr_url'] = m_pr.group(2).strip()
            m_title = re.search(r'\*\*Title\*\*:\s*(.+)', task_text)
            if m_title:
                data['pr_title'] = m_title.group(1).strip()
            m_author = re.search(r'\*\*Author\*\*:\s*`@?([^`\n]+)`', task_text)
            if m_author:
                data['pr_author'] = m_author.group(1).strip()
        except Exception:
            pass

    return data


def write_session_metadata(session_dir: Path, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge updates into <session_dir>/session.json and return the updated dictionary."""
    session_dir.mkdir(parents=True, exist_ok=True)
    current = read_session_metadata(session_dir)
    for k, v in updates.items():
        if v is not None:
            current[k] = v
    current['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    meta_path = get_session_metadata_path(session_dir)
    meta_path.write_text(json.dumps(current, indent=2) + '\n', encoding='utf-8')
    return current


@dataclasses.dataclass
class SessionInfo:
    session_id: str
    session_dir: Path
    src_dir: Path
    build_dir: Path
    install_dir: Path
    log_dir: Path
    scratch_dir: Path
    timeline_path: Path
    devcontainer_path: Optional[Path] = None
    distro: str = 'rolling'
    active_branches: Dict[str, str] = dataclasses.field(default_factory=dict)


def migrate_legacy_worktree_to_alternates(repo_path: Path) -> bool:
    """
    Migrate a legacy ``git worktree`` linked checkout (where ``<repo_path>/.git`` is a file
    pointing into ``<shared_repo>/.git/worktrees/<entry>``) in-place to a standalone
    ``<repo_path>/.git/`` directory backed by ``.git/objects/info/alternates``.

    Preserves working-tree files, symlinks, staged index state, remotes, local/remote/tag refs,
    and symbolic or detached ``HEAD`` without modifying working-tree files, and cleans up the
    stale ``worktrees/<entry>`` registration in the shared repo when writable.

    Returns ``True`` if ``repo_path`` was migrated, or ``False`` if it was already standalone
    or could not be migrated.
    """
    from .git_ops import git_safe_cmd

    repo_path = repo_path.resolve()
    git_entry = repo_path / '.git'
    if not git_entry.is_file() or git_entry.is_symlink():
        return False

    try:
        raw_content = git_entry.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return False
    if not raw_content.startswith('gitdir:'):
        return False

    raw_gitdir = raw_content[len('gitdir:'):].strip()
    if not raw_gitdir:
        return False
    cand_gitdir = Path(raw_gitdir)
    wt_gitdir = cand_gitdir.resolve() if cand_gitdir.is_absolute() else (repo_path / cand_gitdir).resolve()
    if not wt_gitdir.is_dir():
        return False

    common_git_dir: Optional[Path] = None
    commondir_file = wt_gitdir / 'commondir'
    if commondir_file.is_file():
        try:
            raw_common = commondir_file.read_text(encoding='utf-8', errors='replace').strip()
            if raw_common:
                cand_common = Path(raw_common)
                common_git_dir = (
                    cand_common.resolve()
                    if cand_common.is_absolute()
                    else (wt_gitdir / cand_common).resolve()
                )
        except OSError:
            common_git_dir = None

    if common_git_dir is None:
        common_res = subprocess.run(
            git_safe_cmd(repo_path, 'rev-parse', '--git-common-dir'),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if common_res.returncode == 0 and common_res.stdout.strip():
            cand = Path(common_res.stdout.strip())
            common_git_dir = cand.resolve() if cand.is_absolute() else (repo_path / cand).resolve()

    if common_git_dir is None or not (common_git_dir / 'objects').is_dir():
        return False
    source_objects_dir = (common_git_dir / 'objects').resolve()

    sym_res = subprocess.run(
        git_safe_cmd(repo_path, 'symbolic-ref', '-q', 'HEAD'),
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    sym_head: Optional[str] = None
    if sym_res.returncode == 0 and sym_res.stdout.strip():
        cand_sym = sym_res.stdout.strip()
        if cand_sym != 'refs/heads/.invalid':
            sym_head = cand_sym

    head_res = subprocess.run(
        git_safe_cmd(repo_path, 'rev-parse', '--verify', 'HEAD'),
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if head_res.returncode != 0 or not head_res.stdout.strip():
        return False
    head_sha = head_res.stdout.strip()

    remotes: List[Tuple[str, str]] = []
    remotes_res = subprocess.run(
        git_safe_cmd(repo_path, 'remote'),
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )
    if remotes_res.returncode == 0:
        for rem in remotes_res.stdout.splitlines():
            rem_name = rem.strip()
            if not rem_name:
                continue
            url_res = subprocess.run(
                git_safe_cmd(repo_path, 'config', '--local', '--get', f'remote.{rem_name}.url'),
                cwd=str(repo_path),
                capture_output=True,
                text=True,
            )
            if url_res.returncode != 0 or not url_res.stdout.strip():
                url_res = subprocess.run(
                    git_safe_cmd(repo_path, 'remote', 'get-url', '--', rem_name),
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                )
            if url_res.returncode == 0 and url_res.stdout.strip():
                remotes.append((rem_name, url_res.stdout.strip()))

    extra_configs: List[Tuple[str, str]] = []
    config_keys = ['user.name', 'user.email']
    if sym_head and sym_head.startswith('refs/heads/'):
        short_cur = sym_head[len('refs/heads/'):]
        config_keys.extend([f'branch.{short_cur}.remote', f'branch.{short_cur}.merge'])
    for cfg_key in config_keys:
        cfg_res = subprocess.run(
            git_safe_cmd(repo_path, 'config', '--local', '--get', cfg_key),
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if cfg_res.returncode == 0 and cfg_res.stdout.strip():
            extra_configs.append((cfg_key, cfg_res.stdout.strip()))

    refs_res = subprocess.run(
        git_safe_cmd(
            repo_path,
            'for-each-ref',
            '--format=%(refname) %(objectname)',
            'refs/heads/',
            'refs/remotes/',
            'refs/tags/',
        ),
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )

    tmp_git_dir = repo_path / f'.git.migrating-{os.getpid()}'
    if tmp_git_dir.exists():
        _rmtree_force(tmp_git_dir)

    try:
        init_res = subprocess.run(
            ['git', '-c', 'init.defaultRefFormat=files', 'init', '--bare', str(tmp_git_dir)],
            capture_output=True,
            text=True,
        )
        if init_res.returncode != 0:
            raise RuntimeError(f"git init --bare failed for {tmp_git_dir}: {init_res.stderr}")

        subprocess.run(
            ['git', f'--git-dir={tmp_git_dir}', 'config', '--bool', 'core.logallrefupdates', 'true'],
            check=True,
            capture_output=True,
            text=True,
        )

        alternates_file = tmp_git_dir / 'objects' / 'info' / 'alternates'
        alternates_file.parent.mkdir(parents=True, exist_ok=True)
        alternates_file.write_bytes(f"{source_objects_dir.as_posix()}\n".encode('utf-8'))

        wt_objects_dir = wt_gitdir / 'objects'
        if wt_objects_dir.is_dir():
            for src_obj in wt_objects_dir.rglob('*'):
                if not src_obj.is_file():
                    continue
                rel_obj = src_obj.relative_to(wt_objects_dir)
                if rel_obj.as_posix() == 'info/alternates':
                    continue
                dst_obj = tmp_git_dir / 'objects' / rel_obj
                if not dst_obj.exists():
                    dst_obj.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_obj, dst_obj)

        for rem_name, rem_url in remotes:
            subprocess.run(
                ['git', f'--git-dir={tmp_git_dir}', 'remote', 'add', '--', rem_name, rem_url],
                capture_output=True,
                text=True,
            )

        for cfg_key, cfg_val in extra_configs:
            subprocess.run(
                ['git', f'--git-dir={tmp_git_dir}', 'config', cfg_key, cfg_val],
                capture_output=True,
                text=True,
            )

        if refs_res.returncode == 0 and refs_res.stdout.strip():
            seen_refs = set()
            update_cmds: List[str] = []
            head_branches: List[Tuple[str, str]] = []
            for line in refs_res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) != 2:
                    continue
                ref_name, obj_sha = parts
                seen_refs.add(ref_name)
                update_cmds.append(f"update {ref_name} {obj_sha}")
                if ref_name.startswith('refs/heads/'):
                    head_branches.append((ref_name[len('refs/heads/'):], obj_sha))
            for short_b, obj_sha in head_branches:
                fallback_remote_ref = f"refs/remotes/origin/{short_b}"
                if fallback_remote_ref not in seen_refs:
                    update_cmds.append(f"update {fallback_remote_ref} {obj_sha}")
            if update_cmds:
                upd_res = subprocess.run(
                    ['git', f'--git-dir={tmp_git_dir}', 'update-ref', '--stdin'],
                    input=('\n'.join(update_cmds) + '\n').encode('utf-8'),
                    capture_output=True,
                )
                if upd_res.returncode != 0:
                    err_txt = upd_res.stderr.decode('utf-8', errors='replace')
                    raise RuntimeError(f"git update-ref --stdin failed for {tmp_git_dir}: {err_txt}")

        if sym_head:
            subprocess.run(
                ['git', f'--git-dir={tmp_git_dir}', 'symbolic-ref', 'HEAD', sym_head],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            (tmp_git_dir / 'HEAD').write_bytes(f"{head_sha}\n".encode('utf-8'))

        wt_index = wt_gitdir / 'index'
        if wt_index.is_file():
            shutil.copy2(wt_index, tmp_git_dir / 'index')
        else:
            subprocess.run(
                ['git', f'--git-dir={tmp_git_dir}', 'read-tree', head_sha],
                check=True,
                capture_output=True,
                text=True,
            )

        subprocess.run(
            ['git', f'--git-dir={tmp_git_dir}', 'config', '--bool', 'core.bare', 'false'],
            check=True,
            capture_output=True,
            text=True,
        )

        bak_git = repo_path / f'.git.legacy-bak-{os.getpid()}'
        if bak_git.exists():
            bak_git.unlink()
        git_entry.rename(bak_git)
        try:
            tmp_git_dir.rename(git_entry)
        except Exception:
            if bak_git.exists() and not git_entry.exists():
                bak_git.rename(git_entry)
            raise
        bak_git.unlink(missing_ok=True)
    except Exception:
        if tmp_git_dir.exists():
            _rmtree_force(tmp_git_dir)
        raise

    subprocess.run(
        git_safe_cmd(repo_path, 'update-index', '--refresh', '-q'),
        cwd=str(repo_path),
        capture_output=True,
        text=True,
    )

    if (
        wt_gitdir.is_dir()
        and wt_gitdir.parent.name == 'worktrees'
        and wt_gitdir.parent.parent.resolve() == common_git_dir.resolve()
    ):
        try:
            if os.access(wt_gitdir.parent, os.W_OK):
                _rmtree_force(wt_gitdir)
                shared_repo_root = common_git_dir.parent
                if shared_repo_root.is_dir() and os.access(common_git_dir, os.W_OK):
                    subprocess.run(
                        git_safe_cmd(shared_repo_root, 'worktree', 'prune'),
                        cwd=str(shared_repo_root),
                        capture_output=True,
                        text=True,
                    )
        except Exception:
            pass

    return True


def migrate_session_legacy_worktrees(
    session_dir: Path,
    audit_log_path: Optional[Path] = None,
) -> List[Path]:
    """
    Scan ``<session_dir>/src/*`` for legacy linked Git worktrees (``.git`` file) and migrate
    each in-place to a standalone ``.git/`` directory backed by ``.git/objects/info/alternates``.
    """
    src_dir = session_dir / 'src'
    if not src_dir.is_dir():
        return []

    migrated: List[Path] = []
    for sub in sorted(src_dir.iterdir()):
        if not sub.is_dir() or sub.is_symlink():
            continue
        git_entry = sub / '.git'
        if not git_entry.is_file() or git_entry.is_symlink():
            continue
        if migrate_legacy_worktree_to_alternates(sub):
            migrated.append(sub)

    if migrated and audit_log_path is not None:
        try:
            timeline = TimelineLogger(session_dir.name, session_dir, audit_log_path)
            names = ', '.join(f"`{p.name}`" for p in migrated)
            timeline.log_status(
                f"Migrated legacy linked Git worktree(s) ({names}) to standalone session `.git/` "
                "with `objects/info/alternates` for read-only `shared_repos/` compatibility."
            )
        except Exception:
            pass

    return migrated


class SessionManager:
    """Manages multi-task session environments and Git worktrees."""

    def __init__(self, workspace: WorkspaceLayout):
        self.workspace = workspace

    def get_session_dir(self, session_id: str) -> Path:
        valid_id = validate_session_id(session_id)
        target = (self.workspace.sessions_dir / valid_id).resolve()
        if not target.is_relative_to(self.workspace.sessions_dir.resolve()):
            raise ValueError(f"Path traversal detected in session_id '{session_id}'")
        return target

    def session_exists(self, session_id: str) -> bool:
        return self.get_session_dir(session_id).exists()

    def create_session(
        self,
        session_id: str,
        topic: Optional[str] = None,
        distro: str = 'rolling',
        custom_image: Optional[str] = None,
        gateway_url: Optional[str] = None,
    ) -> SessionInfo:
        """
        Create a new session workspace with src, build, install, log, scratch,
        and .devcontainer configuration.
        """
        session_dir = self.get_session_dir(session_id)
        src_dir = session_dir / 'src'
        build_dir = session_dir / 'build'
        install_dir = session_dir / 'install'
        log_dir = session_dir / 'log'
        scratch_dir = session_dir / 'scratch'

        src_dir.mkdir(parents=True, exist_ok=True)
        build_dir.mkdir(parents=True, exist_ok=True)
        install_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)

        # Write .devcontainer/devcontainer.json
        devcontainer_file = write_devcontainer_config(
            session_dir=session_dir,
            workspace_root=self.workspace.root,
            distro=distro,
            custom_image=custom_image,
            gateway_url=gateway_url,
        )

        # Write editor/agent MCP configs (mcp.json, .cursor/mcp.json, .vscode/mcp.json, .mcp.json)
        write_session_mcp_configs(
            session_dir=session_dir,
            workspace_path=self.workspace.root,
        )

        # Write auto-discovered agent instructions (AGENTS.md, CLAUDE.md)
        write_session_agent_instructions(
            session_id=session_id,
            session_dir=session_dir,
            workspace_root=self.workspace.root,
            distro=distro,
        )

        # Install session-level PreToolUse container hooks (.agents/hooks.json, .claude/settings.json)
        from .hooks import install_hooks_config
        install_hooks_config(
            target_dir=session_dir,
            workspace_root=self.workspace.root,
            session_id=session_id,
        )

        write_session_metadata(
            session_dir,
            {
                'session_id': session_id,
                'topic': topic,
                'distro': distro,
                'status': 'active',
            },
        )

        timeline = TimelineLogger(
            session_id=session_id,
            session_dir=session_dir,
            audit_log_path=self.workspace.audit_log_path,
        )
        timeline.init_timeline(topic=topic)

        return SessionInfo(
            session_id=session_id,
            session_dir=session_dir,
            src_dir=src_dir,
            build_dir=build_dir,
            install_dir=install_dir,
            log_dir=log_dir,
            scratch_dir=scratch_dir,
            timeline_path=session_dir / 'timeline.md',
            devcontainer_path=devcontainer_file,
            distro=distro,
        )

    def attach_worktree(
        self,
        session_id: str,
        repo_dir: Path,
        branch_name: str,
        base_ref: str = 'HEAD',
        target_subfolder: Optional[str] = None,
    ) -> Path:
        """
        Create a session checkout for ``repo_dir`` inside the session's ``src/`` directory
        using a session-local ``.git/`` directory and ``.git/objects/info/alternates``
        pointing to ``repo_dir/.git/objects``.

        This keeps ``shared_repos/`` strictly read-only inside the session container while
        allowing ``git add`` and ``git commit`` to write index, HEAD, refs, and new objects
        inside ``<session_dir>/src/<repo>/.git/``.
        """
        if not self.session_exists(session_id):
            self.create_session(session_id)

        session_dir = self.get_session_dir(session_id)
        if target_subfolder:
            if not SUBFOLDER_NAME_PATTERN.match(target_subfolder) or '..' in target_subfolder:
                raise ValueError(f"Invalid target_subfolder '{target_subfolder}'")
            repo_name = target_subfolder
        else:
            repo_name = repo_dir.name

        worktree_target = (session_dir / 'src' / repo_name).resolve()
        if not worktree_target.is_relative_to((session_dir / 'src').resolve()):
            raise ValueError(f"Path traversal detected in target_subfolder '{target_subfolder}'")

        if worktree_target.exists():
            return worktree_target

        from .git_ops import git_safe_cmd

        repo_dir = repo_dir.resolve()
        common_res = subprocess.run(
            git_safe_cmd(repo_dir, 'rev-parse', '--git-common-dir'),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if common_res.returncode == 0 and common_res.stdout.strip():
            cand = Path(common_res.stdout.strip())
            source_git_dir = cand if cand.is_absolute() else (repo_dir / cand).resolve()
        else:
            source_git_dir = (repo_dir / '.git').resolve()
        source_objects_dir = (source_git_dir / 'objects').resolve()

        # Resolve target commit SHA in repo_dir (prefer branch_name if present, else base_ref)
        branch_check = subprocess.run(
            git_safe_cmd(repo_dir, 'rev-parse', '--verify', f'refs/heads/{branch_name}'),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if branch_check.returncode == 0 and branch_check.stdout.strip():
            target_sha = branch_check.stdout.strip()
        else:
            base_check = subprocess.run(
                git_safe_cmd(repo_dir, 'rev-parse', '--verify', base_ref),
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            if base_check.returncode != 0 or not base_check.stdout.strip():
                raise RuntimeError(
                    f"Failed to resolve ref '{branch_name}' or '{base_ref}' in {repo_dir}: {base_check.stderr}"
                )
            target_sha = base_check.stdout.strip()

        worktree_target.mkdir(parents=True, exist_ok=True)
        init_res = subprocess.run(
            ['git', '-c', 'init.defaultRefFormat=files', 'init'],
            cwd=str(worktree_target),
            capture_output=True,
            text=True,
        )
        if init_res.returncode != 0:
            shutil.rmtree(worktree_target, ignore_errors=True)
            raise RuntimeError(f"Failed to initialize checkout in {worktree_target}: {init_res.stderr}")

        alternates_file = worktree_target / '.git' / 'objects' / 'info' / 'alternates'
        alternates_file.parent.mkdir(parents=True, exist_ok=True)
        alternates_file.write_bytes(f"{source_objects_dir.as_posix()}\n".encode('utf-8'))

        # Copy remotes from repo_dir into worktree_target (preferring local config to avoid global url.*.insteadOf)
        remotes_res = subprocess.run(
            git_safe_cmd(repo_dir, 'remote'),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if remotes_res.returncode == 0:
            for rem in remotes_res.stdout.splitlines():
                rem_name = rem.strip()
                if not rem_name:
                    continue
                url_res = subprocess.run(
                    git_safe_cmd(repo_dir, 'config', '--local', '--get', f'remote.{rem_name}.url'),
                    cwd=str(repo_dir),
                    capture_output=True,
                    text=True,
                )
                if url_res.returncode != 0 or not url_res.stdout.strip():
                    url_res = subprocess.run(
                        git_safe_cmd(repo_dir, 'remote', 'get-url', '--', rem_name),
                        cwd=str(repo_dir),
                        capture_output=True,
                        text=True,
                    )
                if url_res.returncode == 0 and url_res.stdout.strip():
                    subprocess.run(
                        git_safe_cmd(worktree_target, 'remote', 'add', '--', rem_name, url_res.stdout.strip()),
                        cwd=str(worktree_target),
                        capture_output=True,
                        text=True,
                    )

        # Copy local git user.name and user.email if configured on repo_dir
        for cfg_key in ('user.name', 'user.email'):
            cfg_res = subprocess.run(
                git_safe_cmd(repo_dir, 'config', '--local', '--get', cfg_key),
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            if cfg_res.returncode == 0 and cfg_res.stdout.strip():
                subprocess.run(
                    git_safe_cmd(worktree_target, 'config', cfg_key, cfg_res.stdout.strip()),
                    cwd=str(worktree_target),
                    capture_output=True,
                    text=True,
                )

        # Copy remote-tracking refs and local branch refs (as origin/<branch> fallback) from repo_dir
        refs_res = subprocess.run(
            git_safe_cmd(
                repo_dir,
                'for-each-ref',
                '--format=%(refname) %(objectname)',
                'refs/remotes/',
                'refs/heads/',
            ),
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )
        if refs_res.returncode == 0:
            for line in refs_res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) != 2:
                    continue
                ref_name, obj_sha = parts
                if ref_name.startswith('refs/remotes/'):
                    subprocess.run(
                        git_safe_cmd(worktree_target, 'update-ref', ref_name, obj_sha),
                        cwd=str(worktree_target),
                        capture_output=True,
                        text=True,
                    )
                elif ref_name.startswith('refs/heads/'):
                    short_branch = ref_name[len('refs/heads/'):]
                    subprocess.run(
                        git_safe_cmd(
                            worktree_target, 'update-ref', f'refs/remotes/origin/{short_branch}', obj_sha
                        ),
                        cwd=str(worktree_target),
                        capture_output=True,
                        text=True,
                    )

        checkout_res = subprocess.run(
            git_safe_cmd(worktree_target, 'checkout', '-B', branch_name, target_sha),
            cwd=str(worktree_target),
            capture_output=True,
            text=True,
        )
        if checkout_res.returncode != 0:
            shutil.rmtree(worktree_target, ignore_errors=True)
            raise RuntimeError(
                f"Failed to checkout branch '{branch_name}' in {worktree_target}: {checkout_res.stderr}"
            )

        timeline = TimelineLogger(session_id, session_dir, self.workspace.audit_log_path)
        timeline.log_status(f"Attached worktree for `{repo_name}` on branch `{branch_name}`.")

        return worktree_target

    def migrate_legacy_worktrees(self, session_id: str) -> List[Path]:
        """
        Migrate any legacy linked Git worktrees in ``<session_dir>/src/*`` to standalone
        ``.git/`` directories backed by ``.git/objects/info/alternates``.
        """
        if not self.session_exists(session_id):
            return []
        session_dir = self.get_session_dir(session_id)
        return migrate_session_legacy_worktrees(
            session_dir=session_dir,
            audit_log_path=self.workspace.audit_log_path,
        )

    def list_sessions(self) -> List[SessionInfo]:
        """List all active sessions in the workspace."""
        if not self.workspace.sessions_dir.exists():
            return []

        sessions = []
        for s_dir in sorted(self.workspace.sessions_dir.iterdir()):
            if not s_dir.is_dir():
                continue

            session_id = s_dir.name
            src_dir = s_dir / 'src'
            build_dir = s_dir / 'build'
            install_dir = s_dir / 'install'
            log_dir = s_dir / 'log'
            scratch_dir = s_dir / 'scratch'
            timeline_path = s_dir / 'timeline.md'

            # Inspect active worktree branches in src
            active_branches = {}
            if src_dir.exists():
                from .git_ops import git_safe_cmd
                for sub in src_dir.iterdir():
                    if sub.is_dir() and (sub / '.git').exists():
                        res = subprocess.run(
                            git_safe_cmd(sub, 'rev-parse', '--abbrev-ref', 'HEAD'),
                            cwd=str(sub),
                            capture_output=True,
                            text=True,
                        )
                        if res.returncode == 0:
                            active_branches[sub.name] = res.stdout.strip()

            sessions.append(SessionInfo(
                session_id=session_id,
                session_dir=s_dir,
                src_dir=src_dir,
                build_dir=build_dir,
                install_dir=install_dir,
                log_dir=log_dir,
                scratch_dir=scratch_dir,
                timeline_path=timeline_path,
                active_branches=active_branches,
            ))

        return sessions

    def get_session_info(self, session_id: str) -> Optional[SessionInfo]:
        """Retrieve SessionInfo for a specific session."""
        if not self.session_exists(session_id):
            return None

        s_dir = self.get_session_dir(session_id)
        src_dir = s_dir / 'src'
        build_dir = s_dir / 'build'
        install_dir = s_dir / 'install'
        log_dir = s_dir / 'log'
        scratch_dir = s_dir / 'scratch'
        timeline_path = s_dir / 'timeline.md'
        devcontainer_path = s_dir / '.devcontainer' / 'devcontainer.json'

        active_branches = {}
        if src_dir.exists():
            from .git_ops import git_safe_cmd
            for sub in src_dir.iterdir():
                if sub.is_dir() and (sub / '.git').exists():
                    res = subprocess.run(
                        git_safe_cmd(sub, 'rev-parse', '--abbrev-ref', 'HEAD'),
                        cwd=str(sub),
                        capture_output=True,
                        text=True,
                    )
                    if res.returncode == 0:
                        active_branches[sub.name] = res.stdout.strip()

        distro = None
        if devcontainer_path.exists():
            try:
                import json
                with open(devcontainer_path, 'r', encoding='utf-8') as f:
                    dc = json.load(f)
                    distro = dc.get('containerEnv', {}).get('ROS_DISTRO')
            except Exception:
                pass

        return SessionInfo(
            session_id=session_id,
            session_dir=s_dir,
            src_dir=src_dir,
            build_dir=build_dir,
            install_dir=install_dir,
            log_dir=log_dir,
            scratch_dir=scratch_dir,
            timeline_path=timeline_path,
            devcontainer_path=devcontainer_path if devcontainer_path.exists() else None,
            distro=distro,
            active_branches=active_branches,
        )

    def prune_session(self, session_id: str, force: bool = False) -> bool:
        """
        Prune a session by stopping any running container, removing linked Git worktrees,
        and deleting the session directory.
        """
        session_dir = self.get_session_dir(session_id)
        if not session_dir.exists():
            return False

        try:
            stop_session_container(session_id)
        except Exception:
            pass

        from .git_ops import git_safe_cmd

        src_dir = session_dir / 'src'
        if src_dir.exists():
            for sub in src_dir.iterdir():
                if sub.is_dir() and (sub / '.git').is_file():
                    # Attempt clean git worktree remove for legacy linked worktrees
                    subprocess.run(
                        git_safe_cmd(sub, 'worktree', 'remove', str(sub)) + (['--force'] if force else []),
                        cwd=str(sub),
                        capture_output=True,
                    )

        _rmtree_force(session_dir)

        if self.workspace.shared_repos_dir.exists():
            for repo_dir in self.workspace.shared_repos_dir.iterdir():
                if repo_dir.is_dir() and (repo_dir / '.git').exists():
                    subprocess.run(
                        git_safe_cmd(repo_dir, 'worktree', 'prune'),
                        cwd=str(repo_dir),
                        capture_output=True,
                    )

        return True
