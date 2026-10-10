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

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

try:
    import pytest
except ImportError:
    pytest = None

from ros_maintainer_agent_harness.audit import read_audit_records, redact_credentials
from ros_maintainer_agent_harness.auth import (
    CallerIdentity,
    caller_context,
    TokenStore,
)
from ros_maintainer_agent_harness.cli import parse_args
from ros_maintainer_agent_harness.devcontainer import (
    load_workspace_env,
    save_workspace_env_var,
    start_session_container,
    stop_session_container,
)
from ros_maintainer_agent_harness.gateway import (
    check_gateway_health,
    get_gateway_status,
    install_launchd_service,
    migrate_workspace_credentials,
    read_gateway_logs,
    start_gateway_service,
    stop_gateway_service,
    validate_gateway_bind_host,
)
from ros_maintainer_agent_harness.runner import FakeCommandRunner
from ros_maintainer_agent_harness.server import create_mcp_server
from ros_maintainer_agent_harness.workspace import WorkspaceLayout
from ros_maintainer_agent_harness.worktree import (
    read_session_metadata,
    SessionManager,
    write_session_metadata,
)


def _docker_mark(func):
    if pytest is not None:
        return pytest.mark.docker(func)
    return func


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def _call_mcp_tool(mcp_server, tool_name: str, **kwargs) -> dict:
    res = asyncio.run(mcp_server.call_tool(tool_name, kwargs))
    if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], dict):
        if 'result' not in res[1]:
            return res[1]
    content_list = getattr(res, 'content', None)
    if content_list is None:
        content_list = res[0] if isinstance(res, tuple) else res
    for item in content_list:
        text = getattr(item, 'text', None)
        if text is not None:
            stripped = text.strip()
            if stripped.startswith('{'):
                return json.loads(stripped)
            return {'success': True, 'text': text}
    raise AssertionError(f"Unexpected call_tool response for {tool_name}: {res!r}")


class TestGatewayAndAuth(unittest.TestCase):

    def test_cli_gateway_workspace_flag_positions(self):
        args1 = parse_args(['-w', '/tmp/ws1', 'gateway', 'status', '--json'])
        self.assertEqual(args1.command, 'gateway')
        self.assertEqual(args1.gateway_action, 'status')
        self.assertEqual(args1.workspace, '/tmp/ws1')

        args2 = parse_args(['gateway', '-w', '/tmp/ws2', 'start', '--port', '9001'])
        self.assertEqual(args2.command, 'gateway')
        self.assertEqual(args2.gateway_action, 'start')
        self.assertEqual(args2.workspace, '/tmp/ws2')
        self.assertEqual(args2.port, 9001)

        args3 = parse_args(['gateway', 'start', '--port', '9002', '-w', '/tmp/ws3'])
        self.assertEqual(args3.command, 'gateway')
        self.assertEqual(args3.gateway_action, 'start')
        self.assertEqual(args3.workspace, '/tmp/ws3')
        self.assertEqual(args3.port, 9002)

    def test_validate_gateway_bind_host(self):
        self.assertEqual(validate_gateway_bind_host('127.0.0.1', allow_wide_bind=False), (True, ''))
        self.assertEqual(validate_gateway_bind_host('localhost', allow_wide_bind=False), (True, ''))
        ok, err = validate_gateway_bind_host('0.0.0.0', allow_wide_bind=False)
        self.assertFalse(ok)
        self.assertIn('Refusing to bind', err)
        self.assertEqual(validate_gateway_bind_host('0.0.0.0', allow_wide_bind=True), (True, ''))

    def test_credential_migration_out_of_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / 'state'
            ws_dir = tmp_path / 'ws'
            with patch.dict(os.environ, {'ROS_MAINTAINER_STATE_DIR': str(state_dir)}):
                layout = WorkspaceLayout(ws_dir)
                layout.initialize()

                legacy_env = ws_dir / '.env'
                legacy_env.write_text('ROS_CONTAINER_GITHUB_TOKEN=ghp_legacy123\n', encoding='utf-8')

                migrated_keys = migrate_workspace_credentials(ws_dir, state_dir=state_dir)
                self.assertEqual(migrated_keys, ['ROS_CONTAINER_GITHUB_TOKEN'])
                self.assertFalse(legacy_env.exists())
                cred_path = state_dir / 'credentials.env'
                self.assertTrue(cred_path.exists())
                if os.name != 'nt':
                    self.assertEqual(cred_path.stat().st_mode & 0o777, 0o600)

                loaded = load_workspace_env(ws_dir)
                self.assertEqual(loaded.get('ROS_CONTAINER_GITHUB_TOKEN'), 'ghp_legacy123')

                # Saving new credentials writes to state_dir/credentials.env, never ws_dir/.env
                saved_path = save_workspace_env_var(ws_dir, 'ROS_HOST_GITHUB_TOKEN', 'ghp_host456')
                self.assertEqual(saved_path, state_dir / 'credentials.env')
                self.assertFalse((ws_dir / '.env').exists())
                self.assertEqual(load_workspace_env(ws_dir).get('ROS_HOST_GITHUB_TOKEN'), 'ghp_host456')

    def test_token_store_lifecycle_and_container_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / 'state'
            ws_dir = tmp_path / 'ws'
            with patch.dict(os.environ, {'ROS_MAINTAINER_STATE_DIR': str(state_dir)}):
                layout = WorkspaceLayout(ws_dir)
                layout.initialize()
                save_workspace_env_var(ws_dir, 'ROS_CONTAINER_GITHUB_TOKEN', 'none')

                store = TokenStore(state_dir=state_dir)
                issued1 = store.issue_token(role='session:s1', container_name='ros-harness-s1')
                self.assertTrue(issued1.token.startswith('rmah_tok_'))
                self.assertEqual(issued1.role, 'session:s1')

                # Raw token must never appear in tokens.json; only SHA-256 hash is stored
                raw_file = (state_dir / 'tokens.json').read_text(encoding='utf-8')
                self.assertNotIn(issued1.token, raw_file)
                self.assertIn(issued1.token_hash, raw_file)
                if os.name != 'nt':
                    self.assertEqual((state_dir / 'tokens.json').stat().st_mode & 0o777, 0o600)

                caller = store.verify_token(f'Bearer {issued1.token}')
                self.assertIsNotNone(caller)
                self.assertEqual(caller.role, 'session:s1')
                self.assertEqual(caller.session_id, 's1')
                self.assertEqual(caller.token_id, issued1.token_id)

                # Issuing a replacement token for the same role revokes the old one
                issued2 = store.issue_token(role='session:s1', container_name='ros-harness-s1')
                self.assertIsNone(store.verify_token(issued1.token))
                self.assertIsNotNone(store.verify_token(issued2.token))

                # Starting a session container via FakeCommandRunner issues a token via env (not cmdline)
                mgr = SessionManager(layout)
                session = mgr.create_session('s1', distro='rolling')

                fake = FakeCommandRunner(default_stdout='container123\n')
                fake.add_prefix_response(['docker', 'inspect'], returncode=1, stderr='No such object')

                res = start_session_container(
                    session_id='s1',
                    session_dir=session.session_dir,
                    workspace_root=layout.root,
                    distro='rolling',
                    runner=fake,
                )

                self.assertTrue(res['success'])
                run_calls = [c for c in fake.calls if 'run' in c.args]
                self.assertEqual(len(run_calls), 1)
                run_call = run_calls[0]
                # Ensure `-e ROS_MAINTAINER_GATEWAY_TOKEN` has no `=rmah_tok_...` in argv
                cmd_joined = ' '.join(run_call.args)
                self.assertNotIn('rmah_tok_', cmd_joined)
                self.assertIn('ROS_MAINTAINER_GATEWAY_TOKEN', run_call.args)
                self.assertIsNotNone(run_call.env)
                container_tok = run_call.env.get('ROS_MAINTAINER_GATEWAY_TOKEN', '')
                self.assertTrue(container_tok.startswith('rmah_tok_'))
                self.assertIsNotNone(store.verify_token(container_tok))

                # Ensure no file in workspace contains rmah_tok_
                for p in ws_dir.rglob('*'):
                    if p.is_file():
                        self.assertNotIn('rmah_tok_', p.read_text(encoding='utf-8', errors='ignore'))

                # Stopping the container revokes the token
                fake_stop = FakeCommandRunner()
                stop_res = stop_session_container('s1', runner=fake_stop)
                self.assertTrue(stop_res['success'])
                self.assertIsNone(store.verify_token(container_tok))

    def test_audit_redacts_gateway_tokens(self):
        text = (
            'Header Authorization: Bearer rmah_tok_ABCDEFGHIJKLMNOPQRSTUV123456 '
            'and ROS_MAINTAINER_GATEWAY_TOKEN=rmah_tok_9876543210abcdefABCDEF '
            'and OPENCODE_SERVER_PASSWORD=supersecretpw'
        )
        sanitized = redact_credentials(text)
        self.assertNotIn('rmah_tok_', sanitized)
        self.assertNotIn('supersecretpw', sanitized)
        self.assertIn('REDACTED', sanitized)

    def test_role_authorization_and_two_session_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / 'state'
            ws_dir = tmp_path / 'ws'
            with patch.dict(os.environ, {'ROS_MAINTAINER_STATE_DIR': str(state_dir)}):
                layout = WorkspaceLayout(ws_dir)
                layout.initialize()
                save_workspace_env_var(ws_dir, 'ROS_CONTAINER_GITHUB_TOKEN', 'none')

                mgr = SessionManager(layout)
                s1 = mgr.create_session('s1', distro='rolling')
                s2 = mgr.create_session('s2', distro='rolling')
                # Mark s1 as started by hub, leave s2 as admin-created (started_by_hub not set)
                write_session_metadata(s1.session_dir, {'started_by_hub': True})
                write_session_metadata(s2.session_dir, {'started_by_hub': False})

                # Record a CI run on s2
                from ros_maintainer_agent_harness.ci import CITracker
                tracker = CITracker(layout.ci_runs_path)
                s2_run = tracker.record_run(
                    pr_url='https://github.com/ros2/rclcpp/pull/200',
                    session_id='s2',
                    job_name='ci_launcher',
                    build_num=999,
                    job_url='https://ci.ros2.org/job/ci_launcher/999/',
                    status='RUNNING',
                    parameters={},
                )

                mcp = create_mcp_server(layout, require_auth=True)

                # 1. Unauthenticated caller is denied on every tool and logged to audit.jsonl
                unauth_res = _call_mcp_tool(mcp, 'get_maintainer_rules')
                self.assertFalse(unauth_res['authorized'])
                self.assertIn('Authentication required', unauth_res['error'])

                # 2. Session s1 caller: can access own session tools, denied on s2 and denied on hub tools
                s1_caller = CallerIdentity(role='session:s1', token_id='tok_s1')
                with caller_context(s1_caller):
                    # Spoof ROS_MAINTAINER_SESSION_ID=s2 in environment; server must ignore it!
                    with patch.dict(os.environ, {'ROS_MAINTAINER_SESSION_ID': 's2'}):
                        # Allowed: own log_status and update_session_status
                        ok_log = _call_mcp_tool(mcp, 'log_status', session_id='s1', message='s1 progress')
                        self.assertTrue(ok_log['success'])

                        # Denied: targeting s2 in log_status or update_session_status
                        bad_log = _call_mcp_tool(mcp, 'log_status', session_id='s2', message='tamper s2')
                        self.assertFalse(bad_log['authorized'])
                        self.assertIn('cannot act on another session', bad_log['error'])
                        self.assertEqual(read_session_metadata(s2.session_dir).get('status'), 'active')

                        bad_update = _call_mcp_tool(mcp, 'update_session_status', session_id='s2', status='done')
                        self.assertFalse(bad_update['authorized'])
                        self.assertEqual(read_session_metadata(s2.session_dir).get('status'), 'active')

                        # Denied: git_push targeting s2 or path inside s2
                        bad_push_id = _call_mcp_tool(
                            mcp, 'git_push', session_id='s2', repo_path='/workspace', branch='fix', reason='push s2'
                        )
                        self.assertFalse(bad_push_id['authorized'])

                        bad_push_path = _call_mcp_tool(
                            mcp,
                            'git_push',
                            session_id='s1',
                            repo_path=str(s2.session_dir / 'src' / 'pkg'),
                            branch='fix',
                            reason='push s2 path',
                        )
                        self.assertFalse(bad_push_path['authorized'])
                        self.assertIn('outside its own session directory', bad_push_path['error'])

                        # Denied: querying or cancelling s2's CI run
                        bad_ci_status = _call_mcp_tool(mcp, 'get_ci_status', job_url_or_id=s2_run.job_url)
                        self.assertFalse(bad_ci_status['success'])
                        self.assertIn("another session ('s2')", bad_ci_status['error'])

                        bad_ci_cancel = _call_mcp_tool(
                            mcp, 'cancel_ci_run', job_url_or_id='999', reason='abort s2'
                        )
                        self.assertFalse(bad_ci_cancel['success'])
                        self.assertIn("another session ('s2')", bad_ci_cancel['error'])

                        # Denied: hub/lifecycle tools
                        for hub_tool, kwargs in [
                            ('get_workspace_status', {}),
                            ('get_next_actions', {}),
                            ('list_sessions', {}),
                            ('list_approval_requests', {}),
                            ('create_session', {'session_id': 's3'}),
                            ('prune_session', {'session_id': 's1'}),
                            ('start_session_container', {'session_id': 's1'}),
                            ('stop_session_container', {'session_id': 's1'}),
                            ('exec_in_session', {'session_id': 's1', 'command': 'echo hi'}),
                        ]:
                            denied = _call_mcp_tool(mcp, hub_tool, **kwargs)
                            self.assertFalse(
                                denied['authorized'],
                                f"Expected {hub_tool} to be denied for session:s1",
                            )

                        # Denied: release tools when allow_session_release_tools is False (default)
                        rel_denied = _call_mcp_tool(
                            mcp,
                            'push_release',
                            session_id='s1',
                            target_branch='rolling',
                            tag='1.0.0',
                            reason='release',
                        )
                        self.assertFalse(rel_denied['authorized'])

                # 3. Hub caller: allowed on hub tools & s1 (started_by_hub=True), denied on s2 (started_by_hub=False)
                # and denied on session git/PR/release mutation tools
                hub_caller = CallerIdentity(role='hub', token_id='tok_hub')
                with caller_context(hub_caller):
                    ws_status = _call_mcp_tool(mcp, 'get_workspace_status', check_containers=False)
                    self.assertIn('sessions', ws_status)

                    # Hub can update status on s1 (started_by_hub=True)
                    hub_s1 = _call_mcp_tool(
                        mcp, 'update_session_status', session_id='s1', status='investigating'
                    )
                    self.assertTrue(hub_s1['success'])
                    self.assertEqual(hub_s1['metadata']['status'], 'investigating')

                    # Hub cannot update status, stop, prune, or exec on s2 (started_by_hub=False)
                    hub_s2_upd = _call_mcp_tool(mcp, 'update_session_status', session_id='s2', status='done')
                    self.assertFalse(hub_s2_upd['authorized'])
                    self.assertIn('that it did not start', hub_s2_upd['error'])

                    hub_s2_stop = _call_mcp_tool(mcp, 'stop_session_container', session_id='s2')
                    self.assertFalse(hub_s2_stop['authorized'])

                    hub_s2_exec = _call_mcp_tool(mcp, 'exec_in_session', session_id='s2', command='echo hi')
                    self.assertFalse(hub_s2_exec['authorized'])

                    # Hub cannot call git_push, create_pull_request, edit_pull_request, push_release, run_bloom_release
                    for mut_tool, kwargs in [
                        (
                            'git_push',
                            {'session_id': 's1', 'repo_path': '/workspace', 'branch': 'b', 'reason': 'r'},
                        ),
                        (
                            'create_pull_request',
                            {
                                'session_id': 's1',
                                'repo': 'o/r',
                                'title': 't',
                                'body': 'b',
                                'head': 'h',
                                'reason': 'r',
                            },
                        ),
                        ('edit_pull_request', {'session_id': 's1', 'pr_target': 'o/r#1', 'reason': 'r'}),
                        (
                            'push_release',
                            {'session_id': 's1', 'target_branch': 'rolling', 'tag': '1.0.0', 'reason': 'r'},
                        ),
                        ('run_bloom_release', {'session_id': 's1', 'repository': 'rclcpp', 'reason': 'r'}),
                    ]:
                        denied = _call_mcp_tool(mcp, mut_tool, **kwargs)
                        self.assertFalse(denied['authorized'], f"Expected {mut_tool} to be denied for hub")

                    # Toggle allow_hub_exec_in_session = False in policy.yaml -> hub is denied exec_in_session on s1
                    policy_text = layout.policy_path.read_text(encoding='utf-8')
                    layout.policy_path.write_text(
                        policy_text.replace('allow_hub_exec_in_session: true', 'allow_hub_exec_in_session: false'),
                        encoding='utf-8',
                    )
                    hub_s1_exec_disabled = _call_mcp_tool(
                        mcp, 'exec_in_session', session_id='s1', command='echo hi'
                    )
                    self.assertFalse(hub_s1_exec_disabled['authorized'])
                    self.assertIn('allow_hub_exec_in_session', hub_s1_exec_disabled['error'])

                # Verify audit records include caller_role and token_id
                records = read_audit_records(layout.audit_log_path, limit=50)
                self.assertTrue(
                    any(r.get('caller_role') == 'session:s1' and r.get('token_id') == 'tok_s1' for r in records)
                )
                self.assertTrue(
                    any(r.get('caller_role') == 'hub' and r.get('token_id') == 'tok_hub' for r in records)
                )

    def test_gateway_daemon_lifecycle_and_http_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / 'state'
            ws_dir = tmp_path / 'ws'
            with patch.dict(os.environ, {'ROS_MAINTAINER_STATE_DIR': str(state_dir)}):
                layout = WorkspaceLayout(ws_dir)
                layout.initialize()

                # Test launchd plist generation without loading
                plist_res = install_launchd_service(
                    workspace_path=ws_dir,
                    host='127.0.0.1',
                    port=8765,
                    plist_path=state_dir / 'org.osrf.ros_maintainer_harness.gateway.plist',
                    load=False,
                )
                self.assertTrue(plist_res['success'])
                self.assertTrue(Path(plist_res['plist_path']).exists())

                port = _get_free_port()

                # Write a stale/dead PID first to verify stale pidfile recovery
                state_dir.mkdir(parents=True, exist_ok=True)
                (state_dir / 'gateway.pid').write_text('99999999\n', encoding='utf-8')

                start_res = start_gateway_service(
                    workspace_path=ws_dir,
                    host='127.0.0.1',
                    port=port,
                    state_dir=state_dir,
                    startup_timeout=10.0,
                )
                try:
                    self.assertTrue(
                        start_res['success'],
                        f"Gateway failed to start: {start_res} / logs: {read_gateway_logs(state_dir=state_dir)}",
                    )
                    self.assertFalse(start_res['already_running'])
                    pid = start_res['pid']
                    self.assertTrue(pid and pid > 0)

                    # Health endpoint responds 200 without auth
                    self.assertTrue(check_gateway_health('127.0.0.1', port))

                    # Status reports running & healthy
                    st = get_gateway_status(host='127.0.0.1', port=port, state_dir=state_dir)
                    self.assertTrue(st['running'])
                    self.assertTrue(st['healthy'])
                    self.assertEqual(st['pid'], pid)

                    # Second start returns already_running without spawning duplicate process
                    start2 = start_gateway_service(
                        workspace_path=ws_dir,
                        host='127.0.0.1',
                        port=port,
                        state_dir=state_dir,
                    )
                    self.assertTrue(start2['success'])
                    self.assertTrue(start2['already_running'])
                    self.assertEqual(start2['pid'], pid)

                    # Unauthenticated request to /mcp returns HTTP 401
                    req_unauth = urllib.request.Request(
                        f'http://127.0.0.1:{port}/mcp', method='POST', data=b'{}'
                    )
                    with self.assertRaises(urllib.error.HTTPError) as exc_info:
                        urllib.request.urlopen(req_unauth, timeout=3.0)
                    self.assertEqual(exc_info.exception.code, 401)

                    # Request with invalid bearer token returns HTTP 401
                    req_bad = urllib.request.Request(
                        f'http://127.0.0.1:{port}/mcp',
                        method='POST',
                        data=b'{}',
                        headers={'Authorization': 'Bearer rmah_tok_invalid_token_value'},
                    )
                    with self.assertRaises(urllib.error.HTTPError) as exc_info2:
                        urllib.request.urlopen(req_bad, timeout=3.0)
                    self.assertEqual(exc_info2.exception.code, 401)

                finally:
                    stop_res = stop_gateway_service(state_dir=state_dir)
                    self.assertTrue(stop_res['success'])
                    self.assertFalse((state_dir / 'gateway.pid').exists())

    @_docker_mark
    def test_container_to_host_gateway_reachability(self):
        if 'PYTEST_CURRENT_TEST' not in os.environ and os.environ.get('ROS_HARNESS_RUN_DOCKER_TESTS') != '1':
            self.skipTest('Docker test skipped under unittest unless ROS_HARNESS_RUN_DOCKER_TESTS=1')
        if not shutil.which('docker'):
            self.skipTest('docker CLI not available')
        info = subprocess.run(['docker', 'info'], capture_output=True, text=True, timeout=10)
        if info.returncode != 0:
            self.skipTest('docker daemon not reachable')

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_dir = tmp_path / 'state'
            ws_dir = tmp_path / 'ws'
            with patch.dict(os.environ, {'ROS_MAINTAINER_STATE_DIR': str(state_dir)}):
                layout = WorkspaceLayout(ws_dir)
                layout.initialize()

                port = _get_free_port()
                start_res = start_gateway_service(
                    workspace_path=ws_dir,
                    host='127.0.0.1',
                    port=port,
                    state_dir=state_dir,
                    startup_timeout=10.0,
                )
                try:
                    self.assertTrue(start_res['success'])
                    images_proc = subprocess.run(
                        ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    candidates = [
                        line.strip()
                        for line in images_proc.stdout.splitlines()
                        if line.strip() and '<none>' not in line
                    ]
                    image = candidates[0] if candidates else 'alpine:latest'

                    health_url = f'http://host.docker.internal:{port}/health'
                    probe_script = (
                        f"if command -v curl >/dev/null 2>&1; then "
                        f"curl -fsS {health_url}; "
                        f"elif command -v python3 >/dev/null 2>&1; then "
                        f"python3 -c \"import urllib.request; "
                        f"print(urllib.request.urlopen('{health_url}').read().decode())\"; "
                        f"elif command -v wget >/dev/null 2>&1; then "
                        f"wget -qO- {health_url}; "
                        f"fi"
                    )
                    c_res = subprocess.run(
                        [
                            'docker', 'run', '--rm',
                            '--add-host=host.docker.internal:host-gateway',
                            image,
                            'sh', '-c', probe_script,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(c_res.returncode, 0, f"Container probe failed: {c_res.stderr}")
                    payload = json.loads(c_res.stdout.strip())
                    self.assertEqual(payload.get('status'), 'ok')
                finally:
                    stop_gateway_service(state_dir=state_dir)


if __name__ == '__main__':
    unittest.main()
