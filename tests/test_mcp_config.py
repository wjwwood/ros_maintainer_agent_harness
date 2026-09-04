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

from ros_maintainer_agent_harness.mcp_config import (
    generate_mcp_config_dict,
    generate_mcp_server_entry,
    get_agent_launch_info,
    write_session_mcp_configs,
)


class TestMCPConfig(unittest.TestCase):

    def test_generate_mcp_server_entry(self):
        ws_path = Path('/tmp/test_workspace')

        # Stdio entry
        stdio_entry = generate_mcp_server_entry(
            workspace_path=ws_path,
            transport='stdio',
            executable='/usr/bin/ros-maintainer-harness',
        )
        self.assertEqual(stdio_entry['command'], '/usr/bin/ros-maintainer-harness')
        self.assertIn('serve', stdio_entry['args'])
        self.assertIn('stdio', stdio_entry['args'])
        self.assertEqual(stdio_entry['env']['ROS_MAINTAINER_WS'], str(ws_path.resolve()))

        # SSE entry
        sse_entry = generate_mcp_server_entry(
            workspace_path=ws_path,
            transport='sse',
            host='127.0.0.1',
            port=8765,
        )
        self.assertEqual(sse_entry['url'], 'http://127.0.0.1:8765/sse')

        # Streamable HTTP entry
        http_entry = generate_mcp_server_entry(
            workspace_path=ws_path,
            transport='streamable-http',
            host='10.0.0.1',
            port=9000,
        )
        self.assertEqual(http_entry['url'], 'http://10.0.0.1:9000/mcp')

        # Full config dict
        cfg_dict = generate_mcp_config_dict(
            workspace_path=ws_path,
            transport='stdio',
        )
        self.assertIn('mcpServers', cfg_dict)
        self.assertIn('ros-maintainer-harness', cfg_dict['mcpServers'])

        # Unsupported transport
        with self.assertRaises(ValueError):
            generate_mcp_server_entry(ws_path, transport='invalid')

    def test_write_session_mcp_configs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_dir = Path(temp_dir) / 'session-1'
            session_dir.mkdir()
            ws_path = Path(temp_dir)

            written = write_session_mcp_configs(
                session_dir=session_dir,
                workspace_path=ws_path,
                transport='stdio',
            )

            self.assertIn('generic', written)
            self.assertIn('claude', written)
            self.assertIn('cursor', written)
            self.assertIn('vscode', written)
            self.assertIn('gemini', written)

            self.assertTrue((session_dir / 'mcp.json').exists())
            self.assertTrue((session_dir / '.mcp.json').exists())
            self.assertTrue((session_dir / '.cursor' / 'mcp.json').exists())
            self.assertTrue((session_dir / '.vscode' / 'mcp.json').exists())
            self.assertTrue((session_dir / '.gemini' / 'mcp_config.json').exists())

            # Verify JSON validity
            data = json.loads((session_dir / 'mcp.json').read_text(encoding='utf-8'))
            self.assertIn('mcpServers', data)
            self.assertIn('ros-maintainer-harness', data['mcpServers'])

    def test_get_agent_launch_info(self):
        session_dir = Path('/tmp/sessions/session-pr-100')
        ws_path = Path('/tmp/ws')

        # Claude launch info
        claude_info = get_agent_launch_info(
            session_id='session-pr-100',
            session_dir=session_dir,
            workspace_path=ws_path,
            distro='jazzy',
            agent='claude',
        )
        self.assertEqual(claude_info['agent'], 'claude')
        self.assertEqual(claude_info['command'], ['claude', '--cwd', str(session_dir.resolve())])
        self.assertEqual(claude_info['environment']['ROS_MAINTAINER_SESSION_ID'], 'session-pr-100')
        self.assertEqual(claude_info['environment']['ROS_DISTRO'], 'jazzy')

        # Cursor launch info
        cursor_info = get_agent_launch_info(
            session_id='session-pr-100',
            session_dir=session_dir,
            workspace_path=ws_path,
            distro='rolling',
            agent='cursor',
        )
        self.assertEqual(cursor_info['agent'], 'cursor')
        self.assertEqual(cursor_info['command'], ['cursor', str(session_dir.resolve())])

        # Gemini launch info
        gemini_info = get_agent_launch_info(
            session_id='session-pr-100',
            session_dir=session_dir,
            workspace_path=ws_path,
            distro='rolling',
            agent='gemini',
        )
        self.assertEqual(gemini_info['agent'], 'gemini')
        self.assertEqual(gemini_info['command'], ['gemini', str(session_dir.resolve())])

        # Antigravity launch info
        ag_info = get_agent_launch_info(
            session_id='session-pr-100',
            session_dir=session_dir,
            workspace_path=ws_path,
            distro='rolling',
            agent='antigravity',
        )
        self.assertEqual(ag_info['agent'], 'antigravity')
        self.assertEqual(ag_info['command'], ['antigravity', str(session_dir.resolve())])

        # Shell launch info
        shell_info = get_agent_launch_info(
            session_id='session-pr-100',
            session_dir=session_dir,
            workspace_path=ws_path,
            agent='shell',
        )
        self.assertEqual(shell_info['agent'], 'shell')
        self.assertEqual(shell_info['command'], ['bash'])


if __name__ == '__main__':
    unittest.main()
