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

from pathlib import Path
import subprocess
import tempfile
import unittest

from ros_maintainer_agent_harness.workspace import WorkspaceLayout
from ros_maintainer_agent_harness.worktree import SessionManager


class TestSessionManager(unittest.TestCase):

    def _init_git_repo(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init'], cwd=str(path), check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=str(path), check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=str(path), check=True)
        (path / 'README.md').write_text('# Test Repo\n')
        subprocess.run(['git', 'add', 'README.md'], cwd=str(path), check=True)
        subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=str(path), check=True)

    def test_session_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            ws_root = temp_path / 'ws'
            source_repo = temp_path / 'source_repo'

            # 1. Initialize sample git repository
            self._init_git_repo(source_repo)

            # 2. Initialize workspace
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            # 3. Create session & attach worktree
            mgr = SessionManager(layout)
            session = mgr.create_session('session-pr-100', topic='Fixing CI')
            self.assertTrue(session.session_dir.is_dir())
            self.assertTrue(session.src_dir.is_dir())
            self.assertTrue(session.build_dir.is_dir())
            self.assertTrue(session.install_dir.is_dir())
            self.assertTrue(session.log_dir.is_dir())
            self.assertTrue(session.timeline_path.is_file())

            wt_path = mgr.attach_worktree(
                session_id='session-pr-100',
                repo_dir=source_repo,
                branch_name='maintainer/test_branch',
            )

            self.assertTrue(wt_path.is_dir())
            self.assertTrue((wt_path / 'README.md').is_file())

            # Verify branch name in worktree
            res = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res.stdout.strip(), 'maintainer/test_branch')

            # 4. List sessions
            sessions = mgr.list_sessions()
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].session_id, 'session-pr-100')
            self.assertIn('source_repo', sessions[0].active_branches)
            self.assertEqual(sessions[0].active_branches['source_repo'], 'maintainer/test_branch')

            # 5. Prune session
            pruned = mgr.prune_session('session-pr-100')
            self.assertTrue(pruned)
            self.assertFalse(session.session_dir.exists())
            self.assertEqual(len(mgr.list_sessions()), 0)

    def test_session_id_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            layout = WorkspaceLayout(Path(temp_dir) / 'ws')
            layout.initialize()
            mgr = SessionManager(layout)

            # Test invalid characters and path traversal attempts
            with self.assertRaises(ValueError):
                mgr.create_session('../invalid_traversal')
            with self.assertRaises(ValueError):
                mgr.create_session('session with spaces')
            with self.assertRaises(ValueError):
                mgr.create_session('session/subfolder')
            with self.assertRaises(ValueError):
                mgr.create_session('')


if __name__ == '__main__':
    unittest.main()
