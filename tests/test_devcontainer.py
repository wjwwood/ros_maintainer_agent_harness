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
from unittest.mock import MagicMock, patch

from ros_maintainer_agent_harness.devcontainer import (
    check_token_and_environment,
    DEFAULT_DISTRO_IMAGES,
    exec_in_session_container,
    generate_devcontainer_config,
    get_image_for_distro,
    load_workspace_env,
    save_workspace_env_var,
    start_session_container,
    stop_session_container,
    write_devcontainer_config,
)
from ros_maintainer_agent_harness.workspace import WorkspaceLayout


class TestDevcontainer(unittest.TestCase):

    def test_get_image_for_distro(self):
        self.assertEqual(get_image_for_distro('rolling'), DEFAULT_DISTRO_IMAGES['rolling'])
        self.assertEqual(get_image_for_distro('jazzy'), DEFAULT_DISTRO_IMAGES['jazzy'])
        self.assertEqual(get_image_for_distro('humble'), DEFAULT_DISTRO_IMAGES['humble'])
        self.assertEqual(
            get_image_for_distro('custom_distro', custom_image='myorg/ros:custom'),
            'myorg/ros:custom',
        )

    def test_generate_devcontainer_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            session_dir = ws_root / 'sessions' / 'session-pr-160'
            session_dir.mkdir(parents=True, exist_ok=True)

            config = generate_devcontainer_config(
                session_dir=session_dir,
                workspace_root=ws_root,
                distro='jazzy',
            )

            self.assertIn('ROS 2 Maintainer Sandbox (session-pr-160)', config['name'])
            self.assertEqual(config['image'], DEFAULT_DISTRO_IMAGES['jazzy'])
            self.assertEqual(config['workspaceFolder'], '/workspace')
            self.assertIn(str(session_dir.resolve()), config['workspaceMount'])
            self.assertEqual(config['containerEnv']['ROS_DISTRO'], 'jazzy')
            self.assertEqual(config['containerEnv']['ROS_MAINTAINER_SESSION_ID'], 'session-pr-160')
            self.assertIn('--add-host=host.docker.internal:host-gateway', config['runArgs'])

            # Verify mount of maintainer_rules.md and shared_repos (read-only by default, writable when requested)
            mounts = config['mounts']
            rules_mount = any('MAINTAINER_RULES.md' in m for m in mounts)
            shared_ro_mount = any('shared_repos' in m and m.endswith(',readonly') for m in mounts)
            self.assertTrue(rules_mount)
            self.assertTrue(shared_ro_mount)

            config_rw = generate_devcontainer_config(
                session_dir=session_dir,
                workspace_root=ws_root,
                distro='jazzy',
                writable_shared_repos=True,
            )
            shared_rw_mount = any(
                'shared_repos' in m and not m.endswith(',readonly') for m in config_rw['mounts']
            )
            self.assertTrue(shared_rw_mount)

    def test_write_devcontainer_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            session_dir = ws_root / 'sessions' / 'session-pr-99'
            session_dir.mkdir(parents=True, exist_ok=True)

            out_file = write_devcontainer_config(
                session_dir=session_dir,
                workspace_root=ws_root,
                distro='rolling',
                gateway_url='http://192.168.1.5:8765',
            )

            self.assertTrue(out_file.exists())
            self.assertEqual(out_file.name, 'devcontainer.json')

            with open(out_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.assertEqual(data['containerEnv']['ROS_MAINTAINER_GATEWAY_URL'], 'http://192.168.1.5:8765')
            self.assertEqual(data['containerEnv']['ROS_DISTRO'], 'rolling')

    def test_env_and_doctor_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            with patch.dict('os.environ', {}, clear=True):
                report = check_token_and_environment(ws_root)
                self.assertFalse(report['container_token_configured'])
                self.assertEqual(report['container_token_mode'], 'unconfigured')

                # Save explicit no-token mode
                save_workspace_env_var(ws_root, 'ROS_CONTAINER_GITHUB_TOKEN', 'none')
                loaded = load_workspace_env(ws_root)
                self.assertEqual(loaded.get('ROS_CONTAINER_GITHUB_TOKEN'), 'none')

                report2 = check_token_and_environment(ws_root)
                self.assertTrue(report2['container_token_configured'])
                self.assertEqual(report2['container_token_mode'], 'none')

                # Save a read-only token
                save_workspace_env_var(ws_root, 'ROS_CONTAINER_GITHUB_TOKEN', 'github_pat_readonly123')
                report3 = check_token_and_environment(ws_root)
                self.assertTrue(report3['container_token_configured'])
                self.assertEqual(report3['container_token_mode'], 'token')

    def test_container_lifecycle_helpers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()
            session_dir = ws_root / 'sessions' / 'session-pr-10'
            session_dir.mkdir(parents=True, exist_ok=True)
            save_workspace_env_var(ws_root, 'ROS_CONTAINER_GITHUB_TOKEN', 'github_pat_ro')

            captured_calls = []

            def fake_run(cmd, **kwargs):
                captured_calls.append((cmd, kwargs))
                res = MagicMock()
                res.returncode = 0
                if 'inspect' in cmd:
                    res.stdout = 'false\n'
                else:
                    res.stdout = 'ok\n'
                res.stderr = ''
                return res

            with patch('subprocess.run', side_effect=fake_run) as mock_run:
                res = start_session_container(
                    session_id='session-pr-10',
                    session_dir=session_dir,
                    workspace_root=ws_root,
                    distro='rolling',
                    runtime='docker',
                )
                self.assertTrue(res['success'])
                self.assertEqual(res['status'], 'started')
                self.assertEqual(res['container_name'], 'ros-harness-session-pr-10')
                self.assertTrue(mock_run.called)
                run_cmd, run_kwargs = [c for c in captured_calls if 'run' in c[0]][0]
                self.assertNotIn('GITHUB_TOKEN=github_pat_ro', run_cmd)
                self.assertIn('GITHUB_TOKEN', run_cmd)
                self.assertEqual(run_kwargs['env']['GITHUB_TOKEN'], 'github_pat_ro')

            def fake_running(cmd, **kwargs):
                res = MagicMock()
                res.returncode = 0
                if 'inspect' in cmd:
                    res.stdout = 'true\n'
                else:
                    res.stdout = 'build output\n'
                res.stderr = ''
                return res

            with patch('subprocess.run', side_effect=fake_running):
                exec_res = exec_in_session_container(
                    session_id='session-pr-10',
                    command='colcon build',
                    runtime='docker',
                    auto_start=False,
                )
                self.assertTrue(exec_res['success'])
                self.assertIn('build output', exec_res['stdout'])

                stop_res = stop_session_container('session-pr-10', runtime='docker')
                self.assertTrue(stop_res['success'])

    def test_fake_command_runner_seam(self):
        import subprocess
        from ros_maintainer_agent_harness.runner import FakeCommandRunner

        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir)
            layout = WorkspaceLayout(ws_root)
            layout.initialize()
            session_dir = ws_root / 'sessions' / 'session-runner-1'
            session_dir.mkdir(parents=True, exist_ok=True)
            save_workspace_env_var(ws_root, 'ROS_CONTAINER_GITHUB_TOKEN', 'github_pat_fake_ro')

            fake_runner = FakeCommandRunner(
                available_binaries={'docker': '/usr/bin/docker', 'gh': None},
            )
            fake_runner.add_prefix_response(
                ['docker', 'inspect'],
                returncode=1,
                stdout='false\n',
            )
            fake_runner.add_prefix_response(
                ['docker', 'run'],
                returncode=0,
                stdout='container_id_123\n',
            )
            fake_runner.add_prefix_response(
                ['docker', 'exec'],
                returncode=0,
                stdout='runner exec output\n',
            )

            res = start_session_container(
                session_id='session-runner-1',
                session_dir=session_dir,
                workspace_root=ws_root,
                distro='jazzy',
                runner=fake_runner,
            )
            self.assertTrue(res['success'])
            self.assertEqual(res['status'], 'started')
            self.assertEqual(res['runtime'], 'docker')

            run_calls = [c for c in fake_runner.calls if len(c.args) >= 2 and c.args[1] == 'run']
            self.assertEqual(len(run_calls), 1)
            self.assertIn('docker.io/osrf/ros:jazzy-desktop', run_calls[0].args)
            self.assertEqual(run_calls[0].env.get('GITHUB_TOKEN'), 'github_pat_fake_ro')

            # Simulate running state and timeout on exec
            running_runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
            running_runner.add_prefix_response(['docker', 'inspect'], returncode=0, stdout='true\n')
            running_runner.add_handler(
                lambda argv: len(argv) >= 2 and argv[1] == 'exec',
                subprocess.TimeoutExpired(cmd=['docker', 'exec'], timeout=5, output='partial log'),
            )
            timeout_res = exec_in_session_container(
                session_id='session-runner-1',
                command='sleep 60',
                timeout=5,
                auto_start=False,
                runner=running_runner,
            )
            self.assertFalse(timeout_res['success'])
            self.assertEqual(timeout_res['returncode'], 124)
            self.assertIn('partial log', timeout_res['stdout'])

            # Simulate no container runtime available
            no_rt_runner = FakeCommandRunner(available_binaries={})
            no_rt_res = start_session_container(
                session_id='session-runner-1',
                session_dir=session_dir,
                workspace_root=ws_root,
                runner=no_rt_runner,
            )
            self.assertFalse(no_rt_res['success'])
            self.assertIn('No container runtime', no_rt_res['error'])
