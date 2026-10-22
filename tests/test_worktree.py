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
            with self.assertRaises(ValueError):
                mgr.create_session('a' * 65)

    def test_commit_with_readonly_shared_repo(self):
        import os
        import stat

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            ws_root = temp_path / 'ws'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            shared_repo = layout.shared_repos_dir / 'sample_pkg'
            self._init_git_repo(shared_repo)

            mgr = SessionManager(layout)
            mgr.create_session('session-ro-commit', topic='Test RO shared_repos commit')
            wt_path = mgr.attach_worktree(
                session_id='session-ro-commit',
                repo_dir=shared_repo,
                branch_name='maintainer/ro_commit_test',
            )

            # Verify session-local .git directory and alternates file
            self.assertTrue((wt_path / '.git').is_dir())
            alternates_file = wt_path / '.git' / 'objects' / 'info' / 'alternates'
            self.assertTrue(alternates_file.is_file())
            self.assertIn(
                (shared_repo / '.git' / 'objects').resolve().as_posix(),
                alternates_file.read_text(encoding='utf-8'),
            )

            # Make shared_repo recursively read-only to simulate :ro container mount
            original_modes = []
            for root, dirs, files in os.walk(shared_repo):
                root_p = Path(root)
                for f_name in files:
                    fp = root_p / f_name
                    mode = fp.stat().st_mode
                    original_modes.append((fp, mode))
                    fp.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
                for d_name in dirs:
                    dp = root_p / d_name
                    mode = dp.stat().st_mode
                    original_modes.append((dp, mode))
                    dp.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            root_mode = shared_repo.stat().st_mode
            original_modes.append((shared_repo, root_mode))
            shared_repo.chmod(root_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

            try:
                (wt_path / 'README.md').write_text('# Updated while shared_repo is read-only\n')
                subprocess.run(['git', 'add', 'README.md'], cwd=str(wt_path), check=True)
                subprocess.run(
                    ['git', 'commit', '-m', 'Commit with RO shared_repos'],
                    cwd=str(wt_path),
                    check=True,
                )
                log_res = subprocess.run(
                    ['git', 'log', '-n', '1', '--pretty=%s'],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(log_res.stdout.strip(), 'Commit with RO shared_repos')
            finally:
                for p, mode in reversed(original_modes):
                    try:
                        p.chmod(mode)
                    except OSError:
                        pass

    def test_migrate_legacy_linked_worktree_to_alternates(self):
        import os
        import stat

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            ws_root = temp_path / 'ws'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            shared_repo = layout.shared_repos_dir / 'legacy_pkg'
            self._init_git_repo(shared_repo)
            subprocess.run(
                ['git', 'remote', 'add', 'origin', 'https://github.com/ros2/legacy_pkg.git'],
                cwd=str(shared_repo),
                check=True,
            )
            subprocess.run(['git', 'tag', '1.0.0'], cwd=str(shared_repo), check=True)

            mgr = SessionManager(layout)
            session = mgr.create_session('session-legacy', topic='Legacy linked worktree')
            wt_path = session.src_dir / 'legacy_pkg'

            # Simulate a pre-#31 linked git worktree pointing into shared_repos/legacy_pkg/.git/worktrees/...
            subprocess.run(
                ['git', 'worktree', 'add', '-b', 'maintainer/legacy_fix', str(wt_path), 'HEAD'],
                cwd=str(shared_repo),
                check=True,
                capture_output=True,
            )
            self.assertTrue((wt_path / '.git').is_file())
            self.assertTrue((shared_repo / '.git' / 'worktrees').is_dir())

            # Create a local commit, a staged file, an unstaged edit, and an untracked file
            (wt_path / 'local_commit.txt').write_text('committed on legacy worktree\n')
            subprocess.run(['git', 'add', 'local_commit.txt'], cwd=str(wt_path), check=True)
            subprocess.run(
                ['git', 'commit', '-m', 'Local commit before migration'],
                cwd=str(wt_path),
                check=True,
            )

            (wt_path / 'staged.txt').write_text('staged content\n')
            subprocess.run(['git', 'add', 'staged.txt'], cwd=str(wt_path), check=True)
            (wt_path / 'README.md').write_text('# Unstaged modification\n')
            (wt_path / 'untracked.txt').write_text('untracked notes\n')

            migrated = mgr.migrate_legacy_worktrees('session-legacy')
            self.assertEqual(migrated, [wt_path.resolve()])
            # Second call is a no-op
            self.assertEqual(mgr.migrate_legacy_worktrees('session-legacy'), [])

            self.assertTrue((wt_path / '.git').is_dir())
            cfg_text = (wt_path / '.git' / 'config').read_text(encoding='utf-8')
            self.assertNotIn('refstorage = reftable', cfg_text)
            self.assertIn('https://github.com/ros2/legacy_pkg.git', cfg_text)

            # Verify shared_repo worktree lock was cleaned up
            wt_list = subprocess.run(
                ['git', 'worktree', 'list'],
                cwd=str(shared_repo),
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertNotIn(str(wt_path), wt_list.stdout)

            # Make shared_repo recursively read-only to simulate :ro container mount
            original_modes = []
            for root, dirs, files in os.walk(shared_repo):
                root_p = Path(root)
                for f_name in files:
                    fp = root_p / f_name
                    mode = fp.stat().st_mode
                    original_modes.append((fp, mode))
                    fp.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
                for d_name in dirs:
                    dp = root_p / d_name
                    mode = dp.stat().st_mode
                    original_modes.append((dp, mode))
                    dp.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            root_mode = shared_repo.stat().st_mode
            original_modes.append((shared_repo, root_mode))
            shared_repo.chmod(root_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))

            try:
                status_res = subprocess.run(
                    ['git', 'status', '--porcelain'],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn('A  staged.txt', status_res.stdout)
                self.assertIn(' M README.md', status_res.stdout)
                self.assertIn('?? untracked.txt', status_res.stdout)

                # Commit staged + unstaged tracked changes and create a branch while shared_repo is RO
                subprocess.run(
                    ['git', 'commit', '-a', '-m', 'Commit after migration with RO shared_repo'],
                    cwd=str(wt_path),
                    check=True,
                )
                subprocess.run(
                    ['git', 'branch', '-f', 'wjwwood/migrated-branch', 'HEAD'],
                    cwd=str(wt_path),
                    check=True,
                )
                tag_res = subprocess.run(
                    ['git', 'rev-parse', '--verify', 'refs/tags/1.0.0'],
                    cwd=str(wt_path),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertEqual(len(tag_res.stdout.strip()), 40)
                self.assertEqual((wt_path / 'untracked.txt').read_text(), 'untracked notes\n')
            finally:
                for p, mode in reversed(original_modes):
                    try:
                        p.chmod(mode)
                    except OSError:
                        pass

    def test_migrate_detached_head_legacy_worktree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            layout = WorkspaceLayout(temp_path / 'ws')
            layout.initialize()

            shared_repo = layout.shared_repos_dir / 'detached_pkg'
            self._init_git_repo(shared_repo)
            expected_sha = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=str(shared_repo),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

            mgr = SessionManager(layout)
            session = mgr.create_session('session-detached')
            wt_path = session.src_dir / 'detached_pkg'
            subprocess.run(
                ['git', 'worktree', 'add', '--detach', str(wt_path), expected_sha],
                cwd=str(shared_repo),
                check=True,
                capture_output=True,
            )

            migrated = mgr.migrate_legacy_worktrees('session-detached')
            self.assertEqual(migrated, [wt_path.resolve()])
            self.assertTrue((wt_path / '.git').is_dir())
            actual_sha = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(actual_sha, expected_sha)


if __name__ == '__main__':
    unittest.main()
