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

from ros_maintainer_agent_harness.auth import TokenStore
from ros_maintainer_agent_harness.cli import main as cli_main, parse_args
from ros_maintainer_agent_harness.devcontainer import (
    build_hub_launch_spec,
    check_token_and_environment,
    DEFAULT_HUB_IMAGE,
    HUB_CONTAINER_NAME,
    save_workspace_env_var,
)
from ros_maintainer_agent_harness.hub_container import (
    attach_to_hub_opencode,
    build_hub_image,
    get_hub_container_logs,
    get_hub_dockerfile_content,
    redeploy_snapshot,
    run_hub_shell,
    start_hub_container,
    stop_hub_container,
)
from ros_maintainer_agent_harness.runner import FakeCommandRunner
from ros_maintainer_agent_harness.workspace import WorkspaceLayout


class TestHubRedeployAndDoctor(unittest.TestCase):

    def test_packaged_hub_dockerfile_properties(self):
        content = get_hub_dockerfile_content()
        self.assertIn('FROM debian:bookworm-slim', content)
        self.assertIn('ARG TARGETARCH', content)
        self.assertIn('COPY dist/*.whl', content)
        self.assertIn('io.ros-maintainer-harness.version', content)
        self.assertIn('io.ros-maintainer-harness.git-sha', content)
        self.assertIn('USER ${USER_UID}:${USER_GID}', content)
        self.assertNotIn('docker-ce-cli', content)
        self.assertNotIn('docker.sock', content)

    def test_build_hub_image_argv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir).resolve()
            (repo_dir / 'pyproject.toml').write_text(
                '[project]\nname = "ros_maintainer_agent_harness"\nversion = "0.2.0"\n',
                encoding='utf-8',
            )

            runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
            runner.add_prefix_response(
                ['git', '-C', str(repo_dir), 'rev-parse', '--short', 'HEAD'],
                returncode=0,
                stdout='abc1234\n',
            )
            runner.add_handler(
                lambda argv: 'pip' in argv and 'wheel' in argv,
                (0, 'Built wheel\n', ''),
            )
            runner.add_prefix_response(['docker', 'build'], returncode=0, stdout='Built image\n')

            res = build_hub_image(
                source_repo=repo_dir,
                tag=DEFAULT_HUB_IMAGE,
                platform='linux/arm64',
                no_cache=True,
                runner=runner,
            )
            self.assertTrue(res['success'])
            self.assertEqual(res['version'], '0.2.0')
            self.assertEqual(res['git_sha'], 'abc1234')
            self.assertEqual(res['version_tag'], 'ros-maintainer-harness-hub:0.2.0-abc1234')

            build_calls = [c for c in runner.calls if len(c.args) >= 2 and c.args[:2] == ['docker', 'build']]
            self.assertEqual(len(build_calls), 1)
            b_argv = build_calls[0].args
            self.assertIn('--build-arg', b_argv)
            self.assertIn('HARNESS_VERSION=0.2.0', b_argv)
            self.assertIn('HARNESS_GIT_SHA=abc1234', b_argv)
            self.assertIn('io.ros-maintainer-harness.version=0.2.0', b_argv)
            self.assertIn('io.ros-maintainer-harness.git-sha=abc1234', b_argv)
            self.assertIn('ros-maintainer-harness-hub:latest', b_argv)
            self.assertIn('ros-maintainer-harness-hub:0.2.0-abc1234', b_argv)
            self.assertIn('--no-cache', b_argv)
            self.assertIn('--platform', b_argv)
            self.assertIn('linux/arm64', b_argv)

    def test_hub_lifecycle_and_idempotency(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as state_tmp:
            ws_root = Path(temp_dir).resolve()
            state_dir = Path(state_tmp).resolve()
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            runner = FakeCommandRunner(
                available_binaries={'docker': '/usr/bin/docker', 'opencode': '/usr/local/bin/opencode'}
            )
            runner.add_prefix_response(
                ['docker', 'inspect', '-f', '{{.State.Running}}', HUB_CONTAINER_NAME],
                returncode=1,
                stdout='false\n',
            )
            runner.add_prefix_response(['docker', 'rm', '-f', HUB_CONTAINER_NAME], returncode=0)
            runner.add_prefix_response(['docker', 'run'], returncode=0, stdout='hub_cid_123\n')
            runner.add_prefix_response(['docker', 'exec'], returncode=0, stdout='ok\n')

            # 1. Refuses to start when gateway is not running and start_gateway=False
            with patch(
                'ros_maintainer_agent_harness.hub_container.get_gateway_status',
                return_value={'running': False, 'port': 8765},
            ):
                fail_res = start_hub_container(
                    workspace_root=ws_root,
                    start_gateway=False,
                    runner=runner,
                    state_dir=state_dir,
                )
                self.assertFalse(fail_res['success'])
                self.assertIn('Gateway launch service is not running', fail_res['error'])

            # 2. Starts when gateway is running
            with patch(
                'ros_maintainer_agent_harness.hub_container.get_gateway_status',
                return_value={'running': True, 'port': 8765, 'version': '0.1.0'},
            ):
                start_res = start_hub_container(
                    workspace_root=ws_root,
                    port=4096,
                    runner=runner,
                    state_dir=state_dir,
                )
                self.assertTrue(start_res['success'])
                self.assertEqual(start_res['status'], 'started')
                self.assertEqual(start_res['container_name'], HUB_CONTAINER_NAME)

                run_calls = [c for c in runner.calls if len(c.args) >= 2 and c.args[:2] == ['docker', 'run']]
                self.assertEqual(len(run_calls), 1)
                run_argv = run_calls[0].args
                run_env = run_calls[0].env

                # Verify no docker.sock, config/ and audit/ mounted :ro, and tokens passed via env only
                self.assertFalse(any('docker.sock' in a for a in run_argv))
                self.assertIn(f"{ws_root}:{ws_root}", run_argv)
                self.assertIn(f"{(ws_root / 'config').resolve()}:{(ws_root / 'config').resolve()}:ro", run_argv)
                self.assertIn(f"{(ws_root / 'audit').resolve()}:{(ws_root / 'audit').resolve()}:ro", run_argv)
                self.assertIn('127.0.0.1:4096:4096', run_argv)
                self.assertIn('ROS_MAINTAINER_GATEWAY_TOKEN', run_argv)
                self.assertIn('OPENCODE_SERVER_PASSWORD', run_argv)
                self.assertTrue(run_env.get('ROS_MAINTAINER_GATEWAY_TOKEN', '').startswith('rmah_tok_'))
                self.assertTrue(run_env.get('OPENCODE_SERVER_PASSWORD', '').startswith('rmah_oc_'))
                self.assertFalse(any('rmah_tok_' in a for a in run_argv))
                self.assertFalse(any('rmah_oc_' in a for a in run_argv))

                # 3. Idempotent when already running
                idemp_runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
                idemp_runner.add_prefix_response(
                    ['docker', 'inspect', '-f', '{{.State.Running}}', HUB_CONTAINER_NAME],
                    returncode=0,
                    stdout='true\n',
                )
                idemp_res = start_hub_container(
                    workspace_root=ws_root,
                    runner=idemp_runner,
                    state_dir=state_dir,
                )
                self.assertTrue(idemp_res['success'])
                self.assertEqual(idemp_res['status'], 'already_running')
                self.assertEqual(
                    [c for c in idemp_runner.calls if len(c.args) >= 2 and c.args[1] == 'run'],
                    [],
                )

                # 4. attach_to_hub_opencode passes OPENCODE_SERVER_PASSWORD via env, not argv
                runner.add_prefix_response(['opencode', 'attach'], returncode=0)
                attach_res = attach_to_hub_opencode(ws_root, runner=runner, state_dir=state_dir)
                self.assertTrue(attach_res['success'])
                attach_call = [c for c in runner.calls if c.args[:2] == ['opencode', 'attach']][0]
                self.assertEqual(
                    attach_call.args,
                    ['opencode', 'attach', 'http://127.0.0.1:4096', '--dir', str(ws_root)],
                )
                self.assertTrue(attach_call.env.get('OPENCODE_SERVER_PASSWORD', '').startswith('rmah_oc_'))

                # 5. logs and shell
                runner.add_prefix_response(
                    ['docker', 'logs', '--tail', '25', HUB_CONTAINER_NAME],
                    returncode=0,
                    stdout='hub log line\n',
                )
                logs_res = get_hub_container_logs(tail=25, runner=runner)
                self.assertTrue(logs_res['success'])
                self.assertIn('hub log line', logs_res['logs'])

                shell_res = run_hub_shell(command='whoami', runner=runner)
                self.assertTrue(shell_res['success'])

                # 6. stop_hub_container stops only ros-harness-hub and revokes the hub token
                stop_runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
                stop_runner.add_prefix_response(['docker', 'rm', '-f', HUB_CONTAINER_NAME], returncode=0)
                stop_res = stop_hub_container(
                    workspace_root=ws_root,
                    runner=stop_runner,
                    state_dir=state_dir,
                )
                self.assertTrue(stop_res['success'])
                self.assertEqual(stop_res['revoked_tokens'], 1)
                self.assertEqual(len(stop_runner.calls), 1)
                self.assertEqual(stop_runner.calls[0].args, ['docker', 'rm', '-f', HUB_CONTAINER_NAME])
                self.assertEqual(TokenStore(state_dir=state_dir).list_active_tokens(), [])

    def test_redeploy_sequence_dirty_refusal_and_session_preservation(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as state_tmp:
            ws_root = (Path(temp_dir) / 'ws').resolve()
            repo_dir = (Path(temp_dir) / 'repo').resolve()
            state_dir = Path(state_tmp).resolve()
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / 'pyproject.toml').write_text(
                '[project]\nname = "ros_maintainer_agent_harness"\nversion = "0.3.0"\n',
                encoding='utf-8',
            )
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            # 1. Dirty working tree is refused when allow_dirty=False
            dirty_runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
            dirty_runner.add_prefix_response(
                ['git', '-C', str(repo_dir), 'status', '--porcelain'],
                returncode=0,
                stdout=' M src/ros_maintainer_agent_harness/cli.py\n',
            )
            dirty_runner.add_prefix_response(
                ['docker', 'inspect', HUB_CONTAINER_NAME],
                returncode=1,
                stdout='',
            )
            with patch(
                'ros_maintainer_agent_harness.hub_container.get_gateway_status',
                return_value={'running': True, 'port': 8765, 'version': '0.1.0'},
            ):
                refused = redeploy_snapshot(
                    workspace_root=ws_root,
                    source_repo=repo_dir,
                    allow_dirty=False,
                    runner=dirty_runner,
                    state_dir=state_dir,
                )
                self.assertFalse(refused['success'])
                self.assertIn('Refusing to redeploy from dirty working tree', refused['error'])

            # 2. Clean working tree builds wheel, builds hub image, installs host wheel,
            #    restarts gateway & hub, and never touches session containers
            clean_runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
            clean_runner.add_prefix_response(
                ['git', '-C', str(repo_dir), 'status', '--porcelain'],
                returncode=0,
                stdout='',
            )
            clean_runner.add_prefix_response(
                ['git', '-C', str(repo_dir), 'rev-parse', '--short', 'HEAD'],
                returncode=0,
                stdout='deadbeef\n',
            )
            clean_runner.add_handler(
                lambda argv: 'pip' in argv and 'wheel' in argv,
                (0, 'Built wheel\n', ''),
            )
            clean_runner.add_handler(
                lambda argv: 'pip' in argv and 'install' in argv,
                (0, 'Installed wheel\n', ''),
            )
            clean_runner.add_prefix_response(['docker', 'build'], returncode=0, stdout='Built image\n')
            clean_runner.add_prefix_response(['docker', 'rm', '-f', HUB_CONTAINER_NAME], returncode=0)
            clean_runner.add_prefix_response(['docker', 'run'], returncode=0, stdout='new_hub_cid\n')
            clean_runner.add_prefix_response(['docker', 'exec'], returncode=0, stdout='ok\n')

            hub_spec = build_hub_launch_spec(ws_root)
            inspect_payload = [
                {
                    'Name': f'/{HUB_CONTAINER_NAME}',
                    'State': {'Running': True},
                    'Config': {
                        'Image': DEFAULT_HUB_IMAGE,
                        'Labels': {
                            'io.ros-maintainer-harness.version': '0.3.0',
                            'io.ros-maintainer-harness.git-sha': 'deadbeef',
                        },
                    },
                    'HostConfig': {'Privileged': False, 'NetworkMode': 'default'},
                    'Mounts': [
                        {
                            'Type': 'bind',
                            'Source': m.source,
                            'Destination': m.target,
                            'RW': not m.read_only,
                        }
                        for m in hub_spec.mounts
                    ],
                }
            ]
            clean_runner.add_prefix_response(
                ['docker', 'inspect', HUB_CONTAINER_NAME],
                returncode=0,
                stdout=json.dumps(inspect_payload),
            )

            with patch(
                'ros_maintainer_agent_harness.hub_container.get_gateway_status',
                return_value={'running': True, 'port': 8765, 'version': '0.3.0'},
            ), patch(
                'ros_maintainer_agent_harness.hub_container.restart_gateway_service',
                return_value={'success': True, 'status': 'started', 'port': 8765},
            ) as mock_gw_restart:
                ok_res = redeploy_snapshot(
                    workspace_root=ws_root,
                    source_repo=repo_dir,
                    allow_dirty=False,
                    also_host=True,
                    runner=clean_runner,
                    state_dir=state_dir,
                )
                self.assertTrue(ok_res['success'], f"Redeploy failed: {ok_res}")
                self.assertTrue(mock_gw_restart.called)
                self.assertEqual(ok_res['new_versions']['version'], '0.3.0')
                self.assertEqual(ok_res['new_versions']['git_sha'], 'deadbeef')
                self.assertTrue(ok_res['new_versions']['host_installed'])
                self.assertTrue(ok_res['session_containers_preserved'])

                # Verify no docker command ever targeted a session container (ros-harness-session-*)
                for c in clean_runner.calls:
                    for arg in c.args:
                        if arg.startswith('ros-harness-') and arg != HUB_CONTAINER_NAME:
                            self.fail(f"Redeploy touched session container '{arg}' in call {c.args}")

    def test_doctor_diagnostics_and_remediations(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as state_tmp:
            ws_root = Path(temp_dir).resolve()
            state_dir = Path(state_tmp).resolve()
            layout = WorkspaceLayout(ws_root)
            layout.initialize()
            save_workspace_env_var(ws_root, 'ROS_CONTAINER_GITHUB_TOKEN', 'none')

            # Case 1: Engine down, gateway down, opencode missing, LLM key missing
            down_runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
            down_runner.add_prefix_response(['docker', 'context', 'show'], returncode=0, stdout='orbstack\n')
            down_runner.add_prefix_response(
                ['docker', 'info', '--format', '{{json .}}'],
                returncode=1,
                stderr='Cannot connect to the Docker daemon\n',
            )
            with patch.dict('os.environ', {'ROS_MAINTAINER_STATE_DIR': str(state_dir)}, clear=True), patch(
                'ros_maintainer_agent_harness.gateway.get_gateway_status',
                return_value={'running': False, 'host': '127.0.0.1', 'port': 8765},
            ):
                rep_down = check_token_and_environment(
                    ws_root,
                    runner=down_runner,
                    state_dir=state_dir,
                    host_arch_override='arm64',
                )
                self.assertFalse(rep_down['engine']['reachable'])
                self.assertFalse(rep_down['gateway_service']['running'])
                self.assertFalse(rep_down['opencode']['installed'])
                self.assertFalse(rep_down['opencode']['llm_credential_configured'])
                self.assertIn('noetic', rep_down['session_images']['unsupported_arm64_distros'])
                components = [r['component'] for r in rep_down['remediations']]
                self.assertIn('engine', components)
                self.assertIn('gateway_service', components)
                self.assertIn('opencode', components)

            # Case 2: Engine up (OrbStack), wrong architecture (arm64 host vs amd64 daemon),
            #         hub running with mount drift (missing audit ro mount), secret LLM key never printed
            drift_runner = FakeCommandRunner(
                available_binaries={'docker': '/usr/bin/docker', 'opencode': '/opt/homebrew/bin/opencode'}
            )
            drift_runner.add_prefix_response(['docker', 'context', 'show'], returncode=0, stdout='orbstack\n')
            drift_runner.add_prefix_response(
                ['docker', 'info', '--format', '{{json .}}'],
                returncode=0,
                stdout=json.dumps({'OperatingSystem': 'OrbStack', 'Architecture': 'x86_64'}),
            )
            drift_runner.add_prefix_response(
                ['docker', 'image', 'inspect', DEFAULT_HUB_IMAGE],
                returncode=0,
                stdout=json.dumps([{'Config': {'Labels': {'io.ros-maintainer-harness.version': '0.0.1'}}}]),
            )
            # Hub container missing the read-only audit/ mount and mounting docker.sock
            drift_runner.add_prefix_response(
                ['docker', 'inspect', HUB_CONTAINER_NAME],
                returncode=0,
                stdout=json.dumps([
                    {
                        'Name': f'/{HUB_CONTAINER_NAME}',
                        'State': {'Running': True},
                        'Config': {'Labels': {'io.ros-maintainer-harness.version': '0.0.1'}},
                        'HostConfig': {'Privileged': False, 'NetworkMode': 'default'},
                        'Mounts': [
                            {'Type': 'bind', 'Source': str(ws_root), 'Destination': str(ws_root), 'RW': True},
                            {
                                'Type': 'bind',
                                'Source': '/var/run/docker.sock',
                                'Destination': '/var/run/docker.sock',
                                'RW': True,
                            },
                        ],
                    }
                ]),
            )
            drift_runner.add_prefix_response(['docker', 'exec'], returncode=0, stdout='{"status":"ok"}\n')
            drift_runner.add_prefix_response(['opencode', '--version'], returncode=0, stdout='1.4.3\n')

            with patch.dict('os.environ', {'ROS_MAINTAINER_STATE_DIR': str(state_dir)}, clear=True), patch(
                'ros_maintainer_agent_harness.gateway.get_gateway_status',
                return_value={'running': True, 'host': '127.0.0.1', 'port': 8765, 'version': '0.1.0'},
            ):
                save_workspace_env_var(ws_root, 'ANTHROPIC_API_KEY', 'sk-ant-api03-supersecret999')
                rep_drift = check_token_and_environment(
                    ws_root,
                    runner=drift_runner,
                    state_dir=state_dir,
                    host_arch_override='arm64',
                )
                self.assertTrue(rep_drift['engine']['reachable'])
                self.assertEqual(rep_drift['engine']['kind'], 'orbstack')
                self.assertTrue(rep_drift['engine']['arch_mismatch'])
                self.assertFalse(rep_drift['hub']['mounts_verified'])
                self.assertFalse(rep_drift['hub']['no_docker_socket'])
                self.assertFalse(rep_drift['hub']['version_matches_host'])
                self.assertTrue(rep_drift['opencode']['installed'])
                self.assertEqual(rep_drift['opencode']['version'], '1.4.3')
                self.assertTrue(rep_drift['opencode']['llm_credential_configured'])
                self.assertIn('ANTHROPIC_API_KEY', rep_drift['opencode']['configured_llm_providers'])
                # Ensure secret value is never exposed in the JSON report
                serialized = json.dumps(rep_drift)
                self.assertNotIn('sk-ant-api03-supersecret999', serialized)

    def test_cli_hub_redeploy_and_doctor_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir).resolve()
            layout = WorkspaceLayout(ws_root)
            layout.initialize()
            save_workspace_env_var(ws_root, 'ROS_CONTAINER_GITHUB_TOKEN', 'none')

            # Verify -w works before and after hub and redeploy subcommands
            a1 = parse_args(['-w', str(ws_root), 'hub', 'status', '--json'])
            a2 = parse_args(['hub', 'status', '-w', str(ws_root), '--json'])
            self.assertEqual(a1.workspace, str(ws_root))
            self.assertEqual(a2.workspace, str(ws_root))

            r1 = parse_args(['-w', str(ws_root), 'redeploy', '--allow-dirty', '--json'])
            r2 = parse_args(['redeploy', '--allow-dirty', '-w', str(ws_root), '--json'])
            self.assertEqual(r1.workspace, str(ws_root))
            self.assertEqual(r2.workspace, str(ws_root))

            buf = io.StringIO()
            with patch('sys.argv', ['ros-maintainer-harness', 'doctor', '-w', str(ws_root), '--json']), patch(
                'sys.stdout', buf
            ):
                cli_main()
            parsed = json.loads(buf.getvalue())
            self.assertEqual(Path(parsed['workspace_root']).resolve(), ws_root)
            self.assertIn('engine', parsed)
            self.assertIn('gateway_service', parsed)
            self.assertIn('hub', parsed)
            self.assertIn('opencode', parsed)


if __name__ == '__main__':
    unittest.main()
