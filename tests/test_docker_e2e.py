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
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

try:
    import pytest
except ImportError:
    pytest = None

from ros_maintainer_agent_harness.auth import caller_context, TokenStore
from ros_maintainer_agent_harness.devcontainer import (
    allocate_free_localhost_port,
    exec_in_session_container,
    inspect_session_container_mounts,
    stop_session_container,
)
from ros_maintainer_agent_harness.gateway import (
    start_gateway_service,
    stop_gateway_service,
)
from ros_maintainer_agent_harness.hub_container import (
    inspect_hub_container_mounts,
    start_hub_container,
    stop_hub_container,
)
from ros_maintainer_agent_harness.server import create_mcp_server
from ros_maintainer_agent_harness.workspace import WorkspaceLayout
from ros_maintainer_agent_harness.worktree import SessionManager


def _is_docker_e2e_enabled() -> bool:
    if os.environ.get('RMAH_RUN_DOCKER_E2E') == '1':
        return True
    for idx, arg in enumerate(sys.argv):
        if arg == '-m' and idx + 1 < len(sys.argv) and sys.argv[idx + 1].strip() == 'docker':
            return True
        if arg.startswith('-m=') and arg.split('=', 1)[1].strip() == 'docker':
            return True
    return False


def _docker_mark(obj):
    if pytest is not None:
        return pytest.mark.docker(obj)
    return obj


@_docker_mark
class TestDockerEndToEndIsolation(unittest.TestCase):
    """
    Opt-in end-to-end Docker isolation test (Issue #40).
    Run with: `pytest -m docker` or `RMAH_RUN_DOCKER_E2E=1 pytest -m docker`.
    """

    def setUp(self):
        if not _is_docker_e2e_enabled():
            self.skipTest(
                "Opt-in Docker E2E test skipped by default; run with 'pytest -m docker' or RMAH_RUN_DOCKER_E2E=1."
            )
        if not shutil.which('docker'):
            self.skipTest("Docker CLI not available on PATH.")
        info_res = subprocess.run(['docker', 'info'], capture_output=True, text=True, timeout=15)
        if info_res.returncode != 0:
            self.skipTest(f"Docker daemon is not reachable: {info_res.stderr.strip()}")

        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name).resolve()
        self.ws_root = self.root / 'ws'
        self.state_dir = self.root / 'state'
        self.prev_state_env = os.environ.get('ROS_MAINTAINER_STATE_DIR')
        os.environ['ROS_MAINTAINER_STATE_DIR'] = str(self.state_dir)

        self.layout = WorkspaceLayout(self.ws_root)
        self.layout.initialize()
        (self.ws_root / '.env').write_text("ROS_CONTAINER_GITHUB_TOKEN=none\n", encoding='utf-8')

        self.test_image = os.environ.get('RMAH_E2E_TINY_IMAGE', 'debian:stable-slim')
        self.gw_port = allocate_free_localhost_port(state_dir=self.state_dir)
        self.hub_port = allocate_free_localhost_port(state_dir=self.state_dir)
        self.session_a = 'e2e-sess-a'
        self.session_b = 'e2e-sess-b'

    def tearDown(self):
        try:
            stop_session_container(self.session_a, state_dir=self.state_dir)
        except Exception:
            pass
        try:
            stop_session_container(self.session_b, state_dir=self.state_dir)
        except Exception:
            pass
        try:
            stop_hub_container(workspace_root=self.ws_root, state_dir=self.state_dir)
        except Exception:
            pass
        try:
            stop_gateway_service(state_dir=self.state_dir)
        except Exception:
            pass
        if self.prev_state_env is None:
            os.environ.pop('ROS_MAINTAINER_STATE_DIR', None)
        else:
            os.environ['ROS_MAINTAINER_STATE_DIR'] = self.prev_state_env
        self.tmpdir.cleanup()

    def test_hub_and_session_docker_isolation_e2e(self):
        # 1. Start the launch service on a test port and start the Hub container
        gw_res = start_gateway_service(
            workspace_path=self.ws_root,
            host='127.0.0.1',
            port=self.gw_port,
            state_dir=self.state_dir,
        )
        self.assertTrue(gw_res.get('success'), f"Failed to start gateway: {gw_res}")

        hub_res = start_hub_container(
            workspace_root=self.ws_root,
            custom_image=self.test_image,
            port=self.hub_port,
            state_dir=self.state_dir,
        )
        self.assertTrue(hub_res.get('success'), f"Failed to start hub container: {hub_res}")

        # 2. Create two sessions (A and sibling B) and start session A through the MCP server as Hub
        mgr = SessionManager(self.layout)
        mgr.create_session(self.session_a, distro='rolling', custom_image=self.test_image)
        mgr.create_session(self.session_b, distro='rolling', custom_image=self.test_image)
        sibling_secret = self.layout.sessions_dir / self.session_b / 'sibling_secret.txt'
        sibling_secret.write_text('secret-from-b', encoding='utf-8')

        store = TokenStore(state_dir=self.state_dir)
        hub_tok = store.issue_token(role='hub', container_name='ros-harness-hub', replace_existing=True)
        hub_caller = store.verify_token(hub_tok.token)

        mcp_srv = create_mcp_server(self.layout, require_auth=True, state_dir=self.state_dir)
        start_tool = mcp_srv._tools['start_session_container']
        with caller_context(hub_caller):
            start_a = start_tool(
                session_id=self.session_a,
                distro='rolling',
                custom_image=self.test_image,
            )
        self.assertTrue(start_a.get('success'), f"Failed to start session A via service: {start_a}")

        # 3. Inspect Hub and Session A mounts and port bindings via `docker inspect`
        hub_mounts = inspect_hub_container_mounts(workspace_root=self.ws_root)
        self.assertTrue(hub_mounts['verified'], f"Hub mount drift: {hub_mounts['drifts']}")
        for m in hub_mounts['actual_mounts']:
            self.assertNotIn('docker.sock', m['source'])
            if m['destination'].endswith('/config') or m['destination'].endswith('/audit'):
                self.assertFalse(m['rw'], f"Expected {m['destination']} to be read-only in Hub")

        sess_mounts = inspect_session_container_mounts(
            session_id=self.session_a,
            workspace_root=self.ws_root,
            session_dir=self.layout.sessions_dir / self.session_a,
        )
        self.assertTrue(sess_mounts['verified'], f"Session mount drift: {sess_mounts['drifts']}")
        for m in sess_mounts['actual_mounts']:
            self.assertNotIn('docker.sock', m['source'])

        for cname in ('ros-harness-hub', f'ros-harness-{self.session_a}'):
            insp = subprocess.run(
                ['docker', 'inspect', '--format', '{{json .HostConfig.PortBindings}}', cname],
                capture_output=True,
                text=True,
                check=True,
            )
            bindings = json.loads(insp.stdout.strip() or '{}')
            for port_key, host_list in bindings.items():
                for entry in host_list or []:
                    self.assertEqual(entry.get('HostIp'), '127.0.0.1')

        # 4. From inside session A, verify sibling session B and audit/ are not visible
        sibling_path = str(self.layout.sessions_dir / self.session_b)
        audit_path = str(self.layout.audit_dir)
        probe = exec_in_session_container(
            session_id=self.session_a,
            command=f"test ! -e {sibling_path} && test ! -e {audit_path} && echo ISOLATED_OK",
            session_dir=self.layout.sessions_dir / self.session_a,
            workspace_root=self.ws_root,
        )
        self.assertTrue(probe.get('success'), f"In-container isolation check failed: {probe}")
        self.assertIn('ISOLATED_OK', probe.get('stdout', ''))

        # Verify the launch service rejects a call from session A that targets session B
        sess_a_tok = store.issue_token(
            role=f'session:{self.session_a}',
            session_id=self.session_a,
            replace_existing=True,
        )
        sess_a_caller = store.verify_token(sess_a_tok.token)
        log_tool = mcp_srv._tools['log_status']
        with caller_context(sess_a_caller):
            cross_res = log_tool(session_id=self.session_b, message='cross-session attempt')
        self.assertFalse(cross_res.get('success', True))
        self.assertEqual(cross_res.get('status'), 'UNAUTHORIZED')


if __name__ == '__main__':
    unittest.main()
