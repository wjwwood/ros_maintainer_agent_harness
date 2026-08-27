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
import tempfile
import unittest

from ros2_maintainer_agent_harness.workspace import WorkspaceLayout


class TestWorkspaceLayout(unittest.TestCase):

    def test_workspace_initialization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'test_ws'
            layout = WorkspaceLayout(ws_root)

            self.assertFalse(layout.is_initialized())
            layout.initialize()
            self.assertTrue(layout.is_initialized())

            # Check directory creation
            self.assertTrue(layout.config_dir.is_dir())
            self.assertTrue(layout.tools_bin_dir.is_dir())
            self.assertTrue(layout.shared_repos_dir.is_dir())
            self.assertTrue(layout.sessions_dir.is_dir())
            self.assertTrue(layout.audit_dir.is_dir())

            # Check default files
            self.assertTrue(layout.policy_path.is_file())
            self.assertTrue(layout.rules_path.is_file())
            self.assertTrue(layout.tools_readme_path.is_file())
            self.assertTrue(layout.tools_requirements_path.is_file())

            # Verify policy loading
            policy = layout.get_policy()
            self.assertTrue(policy.is_branch_push_allowed('wjwwood/test_branch'))


if __name__ == '__main__':
    unittest.main()
