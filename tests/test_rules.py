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

from ros_maintainer_agent_harness.rules import MaintainerRules


class TestMaintainerRules(unittest.TestCase):

    def test_rules_loading_and_recording(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            rules_file = Path(temp_dir) / 'maintainer_rules.md'
            rules = MaintainerRules(rules_file)

            # 1. Default content when file doesn't exist
            content = rules.load_content()
            self.assertIn('Signed-off-by:', content)
            self.assertIn('<username>/<topic_name>', content)

            # 2. Record rule in existing category
            rules.record_preference('Git & Commits', 'Always rebase on main before testing.')
            updated = rules.load_content()
            self.assertIn('Always rebase on main before testing.', updated)

            # 3. Record rule in new category
            rules.record_preference('Documentation', 'Always include mermaid diagrams in design docs.')
            updated_new_cat = rules.load_content()
            self.assertIn('## Documentation', updated_new_cat)
            self.assertIn('Always include mermaid diagrams in design docs.', updated_new_cat)


if __name__ == '__main__':
    unittest.main()
