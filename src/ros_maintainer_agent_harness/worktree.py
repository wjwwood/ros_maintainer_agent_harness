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
from pathlib import Path
import shutil
import subprocess
from typing import Dict, List, Optional

from .devcontainer import write_devcontainer_config
from .timeline import TimelineLogger
from .workspace import WorkspaceLayout


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


class SessionManager:
    """Manages multi-task session environments and Git worktrees."""

    def __init__(self, workspace: WorkspaceLayout):
        self.workspace = workspace

    def get_session_dir(self, session_id: str) -> Path:
        return self.workspace.sessions_dir / session_id

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
        Create a linked Git worktree for repo_dir inside the session's src directory.
        """
        if not self.session_exists(session_id):
            self.create_session(session_id)

        session_dir = self.get_session_dir(session_id)
        repo_name = target_subfolder or repo_dir.name
        worktree_target = session_dir / 'src' / repo_name

        if worktree_target.exists():
            return worktree_target

        # Check if the branch already exists in the repo
        branch_check = subprocess.run(
            ['git', 'rev-parse', '--verify', f'refs/heads/{branch_name}'],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
        )

        if branch_check.returncode == 0:
            # Branch exists; checkout existing branch in worktree
            cmd = ['git', 'worktree', 'add', str(worktree_target), branch_name]
        else:
            # Create new branch based on base_ref
            cmd = ['git', 'worktree', 'add', '-b', branch_name, str(worktree_target), base_ref]

        res = subprocess.run(cmd, cwd=str(repo_dir), capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to create git worktree in {worktree_target}: {res.stderr}")

        timeline = TimelineLogger(session_id, session_dir, self.workspace.audit_log_path)
        timeline.log_status(f"Attached worktree for `{repo_name}` on branch `{branch_name}`.")

        return worktree_target

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
                for sub in src_dir.iterdir():
                    if sub.is_dir() and (sub / '.git').exists():
                        res = subprocess.run(
                            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
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

    def prune_session(self, session_id: str, force: bool = False) -> bool:
        """
        Prune a session by removing linked Git worktrees and deleting session directory.
        """
        session_dir = self.get_session_dir(session_id)
        if not session_dir.exists():
            return False

        src_dir = session_dir / 'src'
        if src_dir.exists():
            for sub in src_dir.iterdir():
                if sub.is_dir() and (sub / '.git').exists():
                    # Attempt clean git worktree remove
                    subprocess.run(
                        ['git', 'worktree', 'remove', str(sub)] + (['--force'] if force else []),
                        cwd=str(sub),
                        capture_output=True,
                    )

        shutil.rmtree(session_dir, ignore_errors=True)
        return True
