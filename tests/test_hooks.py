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
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ros_maintainer_agent_harness.cli import main
from ros_maintainer_agent_harness.hooks import (
    evaluate_pre_tool_use,
    install_hooks_config,
)
from ros_maintainer_agent_harness.workspace import WorkspaceLayout
from ros_maintainer_agent_harness.worktree import SessionManager, write_session_metadata


class TestContainerHooks(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.ws_root = Path(self.temp_dir.name)
        self.layout = WorkspaceLayout(self.ws_root)
        self.layout.initialize()
        self.mgr = SessionManager(self.layout)
        self.session = self.mgr.create_session('pr-rclcpp-160', topic='Test PR')
        write_session_metadata(
            self.session.session_dir,
            {
                'conversation_id': 'spoke-conv-1234',
                'hub_conversation_id': 'hub-conv-9999',
            },
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_install_hooks_config_on_init_and_create_session(self):
        self.assertTrue((self.ws_root / '.agents' / 'hooks.json').exists())
        self.assertFalse((self.ws_root / '_agents' / 'hooks.json').exists())
        self.assertTrue((self.ws_root / '.claude' / 'settings.json').exists())

        self.assertTrue((self.session.session_dir / '.agents' / 'hooks.json').exists())
        self.assertFalse((self.session.session_dir / '_agents' / 'hooks.json').exists())
        self.assertTrue((self.session.session_dir / '.claude' / 'settings.json').exists())

        hooks_data = json.loads((self.session.session_dir / '.agents' / 'hooks.json').read_text(encoding='utf-8'))
        self.assertIn('ros-maintainer-container-exec', hooks_data)
        self.assertIn('PreToolUse', hooks_data['ros-maintainer-container-exec'])
        cmd = hooks_data['ros-maintainer-container-exec']['PreToolUse'][0]['hooks'][0]['command']
        self.assertIn('hook pre-tool-use', cmd)
        self.assertIn('--session pr-rclcpp-160', cmd)

    def test_antigravity_hook_rewrites_command_when_cwd_in_session(self):
        src_repo = self.session.src_dir / 'rclcpp'
        src_repo.mkdir(parents=True, exist_ok=True)
        payload = {
            'conversationId': 'any-conv-id',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'colcon build --symlink-install',
                    'Cwd': str(src_repo),
                },
            },
        }
        res = evaluate_pre_tool_use(payload, workspace_root=self.ws_root)
        self.assertEqual(res['decision'], 'allow')
        self.assertIn('overwrite', res)
        self.assertIn('session exec -d /workspace/src/rclcpp pr-rclcpp-160', res['overwrite']['CommandLine'])
        self.assertIn('colcon build --symlink-install', res['overwrite']['CommandLine'])
        self.assertEqual(res['overwrite']['Cwd'], str(self.session.session_dir.resolve()))

    def test_antigravity_hook_rewrites_command_by_linked_conversation_id(self):
        payload = {
            'conversationId': 'spoke-conv-1234',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'gh pr view 160 --repo ros2/rclcpp',
                    'Cwd': str(self.ws_root),
                },
            },
        }
        res = evaluate_pre_tool_use(payload, workspace_root=self.ws_root)
        self.assertEqual(res['decision'], 'allow')
        self.assertIn('overwrite', res)
        self.assertIn('session exec -d /workspace pr-rclcpp-160', res['overwrite']['CommandLine'])
        self.assertIn('gh pr view 160 --repo ros2/rclcpp', res['overwrite']['CommandLine'])

    def test_hub_conversation_commands_not_rewritten_when_outside_session(self):
        payload = {
            'conversationId': 'hub-conv-9999',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'git status',
                    'Cwd': str(self.ws_root),
                },
            },
        }
        res = evaluate_pre_tool_use(
            payload,
            workspace_root=self.ws_root,
            explicit_session_id='pr-rclcpp-160',
        )
        self.assertEqual(res, {})

    def test_passthrough_commands_not_double_wrapped(self):
        payload = {
            'conversationId': 'spoke-conv-1234',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'ros-maintainer-harness session exec pr-rclcpp-160 -- "colcon test"',
                    'Cwd': str(self.session.session_dir),
                },
            },
        }
        res = evaluate_pre_tool_use(payload, workspace_root=self.ws_root)
        self.assertEqual(res, {})

        # Simulate Hook #1 rewriting a compound/multiline command, followed by Hook #2 running on the output
        compound_payload = {
            'conversationId': 'spoke-conv-1234',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'git status && git diff origin/rolling\npython3 -c "import os\nprint(1)"',
                    'Cwd': str(self.session.session_dir),
                },
            },
        }
        first_res = evaluate_pre_tool_use(compound_payload, workspace_root=self.ws_root)
        self.assertIn('overwrite', first_res)
        second_payload = {
            'conversationId': 'spoke-conv-1234',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': first_res['overwrite']['CommandLine'],
                    'Cwd': first_res['overwrite']['Cwd'],
                },
            },
        }
        second_res = evaluate_pre_tool_use(
            second_payload,
            workspace_root=self.ws_root,
            explicit_session_id='pr-rclcpp-160',
        )
        self.assertEqual(second_res, {})

        # Windows-style .EXE path should also be recognized as passthrough
        win_payload = {
            'conversationId': 'spoke-conv-1234',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': (
                        "'D:\\a\\Scripts\\ros-maintainer-harness.EXE' -w 'D:\\ws' "
                        "session exec -d /workspace pr-rclcpp-160 -- 'git status && git diff'"
                    ),
                    'Cwd': str(self.session.session_dir),
                },
            },
        }
        self.assertEqual(evaluate_pre_tool_use(win_payload, workspace_root=self.ws_root), {})

        # Multiline docker exec command should also be recognized as passthrough
        docker_multiline_payload = {
            'conversationId': 'spoke-conv-1234',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'docker exec ros-harness-pr-rclcpp-160 python3 -c "import sys\nprint(sys.version)"',
                    'Cwd': str(self.session.session_dir),
                },
            },
        }
        self.assertEqual(evaluate_pre_tool_use(docker_multiline_payload, workspace_root=self.ws_root), {})

        env_payload = {
            'conversationId': 'spoke-conv-1234',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'env -u ANTIGRAVITY_PROJECT_ID agentapi send-message hub-conv-9999 "hello"',
                    'Cwd': str(self.session.session_dir),
                },
            },
        }
        self.assertEqual(evaluate_pre_tool_use(env_payload, workspace_root=self.ws_root), {})

    def test_uncontainerized_host_colcon_denied_outside_session(self):
        payload = {
            'conversationId': 'hub-conv-9999',
            'workspacePaths': [str(self.ws_root)],
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'colcon build --packages-select rclcpp',
                    'Cwd': str(self.ws_root),
                },
            },
        }
        res = evaluate_pre_tool_use(payload, workspace_root=self.ws_root)
        self.assertEqual(res['decision'], 'deny')
        self.assertIn('Direct host execution', res['reason'])

    def test_claude_code_bash_hook_format(self):
        payload = {
            'session_id': 'claude-sess-1',
            'cwd': str(self.session.session_dir),
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {
                'command': 'pytest -v',
            },
        }
        res = evaluate_pre_tool_use(payload, workspace_root=self.ws_root)
        self.assertIn('hookSpecificOutput', res)
        out = res['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'allow')
        self.assertIn('session exec -d /workspace pr-rclcpp-160', out['updatedInput']['command'])
        self.assertIn('pytest -v', out['updatedInput']['command'])

    def test_cli_hook_pre_tool_use(self):
        payload = {
            'conversationId': 'spoke-conv-1234',
            'toolCall': {
                'name': 'run_command',
                'arguments': {
                    'CommandLine': 'colcon test',
                    'Cwd': str(self.session.session_dir),
                },
            },
        }
        stdin_buf = io.StringIO(json.dumps(payload))
        stdout_buf = io.StringIO()
        with patch('sys.argv', ['ros-maintainer-harness', '-w', str(self.ws_root), 'hook', 'pre-tool-use']):
            with patch('sys.stdin', stdin_buf), patch('sys.stdout', stdout_buf):
                rc = main()
                self.assertEqual(rc, 0)
        out = json.loads(stdout_buf.getvalue())
        self.assertEqual(out['decision'], 'allow')
        self.assertIn('session exec -d /workspace pr-rclcpp-160', out['overwrite']['CommandLine'])

    def test_install_hooks_merges_existing_config_without_duplicating(self):
        first = install_hooks_config(target_dir=self.ws_root, workspace_root=self.ws_root)
        second = install_hooks_config(target_dir=self.ws_root, workspace_root=self.ws_root)
        self.assertEqual(first['antigravity'], second['antigravity'])
        data = json.loads(second['antigravity'].read_text(encoding='utf-8'))
        self.assertEqual(len(data['ros-maintainer-container-exec']['PreToolUse']), 1)
        claude_data = json.loads(second['claude'].read_text(encoding='utf-8'))
        self.assertEqual(len(claude_data['hooks']['PreToolUse']), 1)


if __name__ == '__main__':
    unittest.main()
