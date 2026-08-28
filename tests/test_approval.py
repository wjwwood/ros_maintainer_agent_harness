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

from ros_maintainer_agent_harness.approval import ApprovalManager, ApprovalStatus


class TestApprovalManager(unittest.TestCase):

    def test_approval_lifecycle_and_persistence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            approvals_file = Path(temp_dir) / 'approvals.json'
            mgr = ApprovalManager(approvals_file)

            # 1. Create request
            req = mgr.create_request(
                session_id='session-pr-160',
                action='git_push',
                target='external_user/rclcpp:fix_bug',
                reason='Pushing fix to contributor fork',
                details={'branch': 'fix_bug'},
            )
            self.assertTrue(req.ticket_id.startswith('req-'))
            self.assertEqual(req.status, ApprovalStatus.PENDING)
            self.assertFalse(mgr.is_approved(req.ticket_id))

            # 2. List requests
            pending = mgr.list_requests(status=ApprovalStatus.PENDING)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].ticket_id, req.ticket_id)

            # 3. Reload new manager from disk to verify persistence
            mgr2 = ApprovalManager(approvals_file)
            fetched = mgr2.get_request(req.ticket_id)
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.reason, 'Pushing fix to contributor fork')

            # 4. Approve request
            approved_req = mgr2.approve_request(
                req.ticket_id, maintainer='wjwwood', comment='Looks good to push'
            )
            self.assertEqual(approved_req.status, ApprovalStatus.APPROVED)
            self.assertEqual(approved_req.resolved_by, 'wjwwood')
            self.assertTrue(mgr2.is_approved(req.ticket_id))

            # 5. Reject request
            req2 = mgr2.create_request(
                session_id='session-pr-99',
                action='create_pull_request',
                target='ros2/rclcpp',
                reason='Open unvetted PR',
            )
            rejected = mgr2.reject_request(req2.ticket_id, maintainer='wjwwood', comment='Not ready')
            self.assertEqual(rejected.status, ApprovalStatus.REJECTED)
            self.assertFalse(mgr2.is_approved(req2.ticket_id))


if __name__ == '__main__':
    unittest.main()
