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

from ros_maintainer_agent_harness.instructions import (
    get_agent_system_prompt,
    get_in_container_session_instructions,
    get_opencode_session_instructions,
    get_session_agent_instructions,
    get_workspace_coordinator_instructions,
)
from ros_maintainer_agent_harness.rules import (
    dump_default_rules_md,
    get_default_maintainer_rules_template,
)
from ros_maintainer_agent_harness.workspace import WorkspaceLayout


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
            self.assertTrue((ws_root / 'AGENTS.md').is_file())
            self.assertFalse((ws_root / 'GEMINI.md').exists())
            self.assertTrue((ws_root / 'CLAUDE.md').is_file())
            self.assertEqual((ws_root / 'CLAUDE.md').read_text(encoding='utf-8').strip(), '@AGENTS.md')

            # Verify policy loading
            policy = layout.get_policy()
            self.assertTrue(policy.is_branch_push_allowed('maintainer/test_branch'))

            # Verify default maintainer rules include writing style & copy-pasteable snippet rules
            rules_content = layout.rules_path.read_text(encoding='utf-8')
            self.assertEqual(rules_content, dump_default_rules_md())
            self.assertEqual(rules_content, get_default_maintainer_rules_template())
            self.assertIn('## Writing Style & Copy-Pasteable Snippets', rules_content)
            self.assertIn('````markdown', rules_content)
            self.assertIn('raw GitHub permalink URLs', rules_content)

    def test_instructions_reference_maintainer_writing_style_rules(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'test_ws'
            sess_dir = ws_root / 'sessions' / 'pr-rclcpp-160'
            for content in (
                get_agent_system_prompt(),
                get_workspace_coordinator_instructions(ws_root),
                get_session_agent_instructions('pr-rclcpp-160', sess_dir, ws_root),
                get_opencode_session_instructions('pr-rclcpp-160', sess_dir, ws_root),
                get_in_container_session_instructions('pr-rclcpp-160', sess_dir, ws_root),
            ):
                self.assertIn('Writing Style & Copy-Pasteable Snippets', content)
                self.assertIn('maintainer_rules.md', content)


if __name__ == '__main__':
    unittest.main()
