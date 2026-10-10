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
    install_global_mcp_config,
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

    def test_install_global_mcp_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_home = Path(temp_dir) / 'home'
            ws_path = Path(temp_dir) / 'ws'
            ws_path.mkdir(parents=True)

            # Pre-create an empty 0-byte ~/.gemini/config/mcp_config.json to verify resilient merge
            gemini_cfg = fake_home / '.gemini' / 'config' / 'mcp_config.json'
            gemini_cfg.parent.mkdir(parents=True, exist_ok=True)
            gemini_cfg.write_text('', encoding='utf-8')

            updated = install_global_mcp_config(
                workspace_path=ws_path,
                targets=['gemini', 'claude'],
                home_dir=fake_home,
            )
            self.assertIn('gemini', updated)
            self.assertIn('claude', updated)

            g_data = json.loads(gemini_cfg.read_text(encoding='utf-8'))
            self.assertIn('ros-maintainer-harness', g_data['mcpServers'])
            c_data = json.loads((fake_home / '.claude.json').read_text(encoding='utf-8'))
            self.assertIn('ros-maintainer-harness', c_data['mcpServers'])

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

        # OpenCode launch info
        oc_info = get_agent_launch_info(
            session_id='session-pr-100',
            session_dir=session_dir,
            workspace_path=ws_path,
            agent='opencode',
        )
        self.assertEqual(oc_info['agent'], 'opencode')
        self.assertEqual(
            oc_info['command'],
            ['ros-maintainer-harness', '-w', str(ws_path.resolve()), 'session', 'attach', 'session-pr-100'],
        )

    def test_opencode_config_and_instructions_without_token_leakage(self):
        import os
        from unittest.mock import patch
        from ros_maintainer_agent_harness.instructions import (
            get_opencode_hub_instructions,
            get_opencode_session_instructions,
        )
        from ros_maintainer_agent_harness.mcp_config import (
            generate_opencode_config,
            write_opencode_config,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir) / 'ws'
            session_dir = ws_root / 'sessions' / 'sess-1'
            fake_home = Path(temp_dir) / 'home'
            ws_root.mkdir(parents=True)
            session_dir.mkdir(parents=True)

            with patch.dict(os.environ, {'ROS_MAINTAINER_GATEWAY_TOKEN': 'rmah_tok_super_secret_should_never_write'}):
                hub_cfg = generate_opencode_config(workspace_root=ws_root, role='hub', port=8765)
                self.assertEqual(hub_cfg['mcp']['ros-maintainer-harness']['type'], 'remote')
                self.assertEqual(
                    hub_cfg['mcp']['ros-maintainer-harness']['url'],
                    'http://host.docker.internal:8765/mcp',
                )
                self.assertEqual(
                    hub_cfg['mcp']['ros-maintainer-harness']['headers']['Authorization'],
                    'Bearer {env:ROS_MAINTAINER_GATEWAY_TOKEN}',
                )
                self.assertEqual(hub_cfg['permission']['edit']['config/*'], 'deny')
                self.assertEqual(hub_cfg['permission']['edit']['audit/*'], 'deny')

                sess_cfg = generate_opencode_config(workspace_root=ws_root, role='session', port=8765)
                self.assertEqual(sess_cfg['permission']['edit'], 'allow')

                hub_file = write_opencode_config(target_dir=ws_root, workspace_root=ws_root, role='hub')
                sess_files = write_session_mcp_configs(session_dir=session_dir, workspace_path=ws_root)
                self.assertIn('opencode', sess_files)
                self.assertTrue((session_dir / 'opencode.json').is_file())

                for p in [hub_file] + list(sess_files.values()):
                    text = p.read_text(encoding='utf-8')
                    self.assertNotIn('rmah_tok_super_secret_should_never_write', text)

                global_written = install_global_mcp_config(
                    workspace_path=ws_root,
                    targets=['opencode'],
                    home_dir=fake_home,
                )
                self.assertIn('opencode', global_written)
                self.assertTrue((fake_home / '.config' / 'opencode' / 'opencode.json').is_file())

            hub_md = get_opencode_hub_instructions(ws_root)
            self.assertIn('Maintainer Hub', hub_md)
            self.assertIn('NEVER', hub_md.upper())
            self.assertIn('config/policy.yaml', hub_md)

            sess_md = get_opencode_session_instructions(
                session_id='sess-1',
                session_dir=session_dir,
                workspace_root=ws_root,
            )
            self.assertIn('sess-1', sess_md)
            self.assertIn('colcon build', sess_md)


if __name__ == '__main__':
    unittest.main()
