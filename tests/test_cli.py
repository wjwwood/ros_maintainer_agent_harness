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

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from ros_maintainer_agent_harness.cli import main


class TestCLI(unittest.TestCase):

    def test_cli_init_and_session_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = str(Path(temp_dir) / 'cli_ws')

            # 1. Test init
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'init']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('Successfully initialized maintainer workspace', fake_out.getvalue())

            # 2. Test rules add & show
            add_args = [
                'ros-maintainer-harness', '-w', ws_root, 'rules', 'add', 'Testing', 'Run pytest with -v'
            ]
            with patch.object(sys, 'argv', add_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Added rule to category 'Testing'", fake_out.getvalue())

            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'rules', 'show']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('Run pytest with -v', fake_out.getvalue())

            # 3. Test session create
            create_args = [
                'ros-maintainer-harness', '-w', ws_root, 'session', 'create', 'session-pr-42',
                '--topic', 'PR 42 Fix'
            ]
            with patch.object(sys, 'argv', create_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Created session 'session-pr-42'", fake_out.getvalue())

            # 4. Test session list
            with patch.object(sys, 'argv', ['ros-maintainer-harness', '-w', ws_root, 'session', 'list']):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn('session-pr-42', fake_out.getvalue())

            # 5. Test session prune
            prune_args = [
                'ros-maintainer-harness', '-w', ws_root, 'session', 'prune', 'session-pr-42'
            ]
            with patch.object(sys, 'argv', prune_args):
                with patch('sys.stdout', new=io.StringIO()) as fake_out:
                    ret = main()
                    self.assertEqual(ret, 0)
                    self.assertIn("Successfully pruned session 'session-pr-42'", fake_out.getvalue())


if __name__ == '__main__':
    unittest.main()
