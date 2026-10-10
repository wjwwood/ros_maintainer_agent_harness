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

from ros_maintainer_agent_harness.audit import format_audit_record, read_audit_records
from ros_maintainer_agent_harness.timeline import TimelineLogger


class TestAuditRecords(unittest.TestCase):

    def test_audit_logging_and_querying(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            audit_log = ws_root / 'audit' / 'audit.jsonl'
            session_dir = ws_root / 'sessions' / 'session-1'

            timeline = TimelineLogger('session-1', session_dir, audit_log)
            timeline.log_action(
                action='git_push',
                target='ros2/rclcpp:fix_bug',
                reason='Cherry-pick commit',
                status='APPROVED',
            )
            timeline.log_action(
                action='launch_jenkins_ci',
                target='ros2/rclcpp#160',
                reason='Test fix on Rolling',
                status='APPROVED',
            )

            # Query all
            records = read_audit_records(audit_log)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]['action'], 'git_push')
            self.assertEqual(records[1]['action'], 'launch_jenkins_ci')

            # Query filtered by action
            ci_records = read_audit_records(audit_log, action='launch_jenkins_ci')
            self.assertEqual(len(ci_records), 1)

            # Format record
            formatted = format_audit_record(records[0])
            self.assertIn('git_push', formatted)
            self.assertIn('Cherry-pick commit', formatted)

    def test_redact_llm_keys_and_opencode_password(self):
        import os
        from unittest.mock import patch
        from ros_maintainer_agent_harness.audit import redact_credentials

        fake_gemini_key = 'AIza' + 'SyA1234567890abcdefghijklmnopqrstuv'
        raw = (
            "ANTHROPIC_API_KEY=sk-ant-api03-1234567890abcdef "
            "OPENAI_API_KEY=sk-proj-1234567890abcdefghijklmn "
            f"GEMINI_API_KEY={fake_gemini_key} "
            "OPENROUTER_API_KEY=sk-or-v1-1234567890abcdef "
            "OPENCODE_SERVER_PASSWORD=rmah_oc_secretpass123456 "
            "Bearer rmah_tok_gatewaysecret7890"
        )
        scrubbed = redact_credentials(raw)
        self.assertNotIn('sk-ant-api03-1234567890abcdef', scrubbed)
        self.assertNotIn('sk-proj-1234567890abcdefghijklmn', scrubbed)
        self.assertNotIn(fake_gemini_key, scrubbed)
        self.assertNotIn('sk-or-v1-1234567890abcdef', scrubbed)
        self.assertNotIn('rmah_oc_secretpass123456', scrubbed)
        self.assertNotIn('rmah_tok_gatewaysecret7890', scrubbed)
        self.assertIn('***REDACTED_TOKEN***', scrubbed)

        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'custom_anthropic_secret_value_xyz'}):
            self.assertNotIn(
                'custom_anthropic_secret_value_xyz',
                redact_credentials("Using key custom_anthropic_secret_value_xyz in request"),
            )


if __name__ == '__main__':
    unittest.main()
