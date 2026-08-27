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
from pathlib import Path
import tempfile
import unittest

from ros2_maintainer_agent_harness.timeline import TimelineLogger


class TestTimelineLogger(unittest.TestCase):

    def test_timeline_and_audit_logging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / 'sessions' / 'session-test'
            audit_file = Path(temp_dir) / 'audit' / 'audit.jsonl'

            logger = TimelineLogger(
                session_id='session-test',
                session_dir=session_dir,
                audit_log_path=audit_file,
            )

            # 1. Initialize timeline
            logger.init_timeline(topic='Test Topic')
            self.assertTrue(logger.timeline_path.is_file())

            # 2. Log status
            logger.log_status('Compiling rclcpp package')
            logger.log_status('Fixed linter errors', milestone='Lint Clean')

            with open(logger.timeline_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertIn('Session Timeline: session-test (Test Topic)', content)
            self.assertIn('Compiling rclcpp package', content)
            self.assertIn('🏆 **Milestone**: Lint Clean — Fixed linter errors', content)

            # 3. Log action
            logger.log_action(
                action='git_push',
                target='wjwwood/rclcpp:wjwwood/fix_linter',
                reason='Fixed cppcheck warning on lyrical',
                status='APPROVED',
                details={'commit_sha': '1234567'},
            )

            self.assertTrue(audit_file.is_file())
            with open(audit_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)

            record = json.loads(lines[0])
            self.assertEqual(record['session_id'], 'session-test')
            self.assertEqual(record['action'], 'git_push')
            self.assertEqual(record['status'], 'APPROVED')
            self.assertEqual(record['reason'], 'Fixed cppcheck warning on lyrical')
            self.assertEqual(record['details']['commit_sha'], '1234567')


if __name__ == '__main__':
    unittest.main()
