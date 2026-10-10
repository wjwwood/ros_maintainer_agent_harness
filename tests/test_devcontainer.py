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

from ros_maintainer_agent_harness.config import ContainerPolicy, HarnessPolicy
from ros_maintainer_agent_harness.devcontainer import (
    build_hub_launch_spec,
    build_session_launch_spec,
    check_token_and_environment,
    DEFAULT_DISTRO_IMAGES,
    DEFAULT_HUB_IMAGE,
    exec_in_session_container,
    generate_devcontainer_config,
    get_image_for_distro,
    HUB_CONTAINER_NAME,
    inspect_hub_container_mounts,
    inspect_session_container_mounts,
    load_workspace_env,
    MountSpec,
    PortPublishSpec,
    save_workspace_env_var,
    start_session_container,
    stop_session_container,
    validate_launch_spec,
    verify_container_mounts_against_spec,
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

    def test_launch_spec_and_argv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir).resolve()
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            session_dir = (ws_root / 'sessions' / 'session-pr-42').resolve()
            session_dir.mkdir(parents=True, exist_ok=True)

            sess_spec = build_session_launch_spec(
                session_id='session-pr-42',
                session_dir=session_dir,
                workspace_root=ws_root,
                distro='rolling',
                include_github_token=True,
                ports=[PortPublishSpec(host_ip='127.0.0.1', host_port=4101, container_port=4101)],
            )
            is_valid, errs = validate_launch_spec(sess_spec)
            self.assertTrue(is_valid, f"Expected valid session spec, got errors: {errs}")

            argv = sess_spec.to_docker_run_argv(runtime='docker')
            self.assertEqual(argv[:5], ['docker', 'run', '-d', '--name', 'ros-harness-session-pr-42'])
            self.assertIn('--workdir', argv)
            self.assertIn(str(session_dir), argv)
            self.assertIn('--security-opt=seccomp=unconfined', argv)
            self.assertIn('-p', argv)
            self.assertIn('127.0.0.1:4101:4101', argv)
            self.assertIn(f"{session_dir}:{session_dir}", argv)
            self.assertIn(f"{(ws_root / 'tools').resolve()}:{(ws_root / 'tools').resolve()}:ro", argv)
            self.assertIn(
                f"{(ws_root / 'shared_repos').resolve()}:{(ws_root / 'shared_repos').resolve()}:ro",
                argv,
            )
            self.assertIn(DEFAULT_DISTRO_IMAGES['rolling'], argv)

            # Build and validate Hub LaunchSpec
            hub_spec = build_hub_launch_spec(
                workspace_root=ws_root,
                ports=[PortPublishSpec(host_ip='127.0.0.1', host_port=4096, container_port=4096)],
            )
            hub_valid, hub_errs = validate_launch_spec(hub_spec)
            self.assertTrue(hub_valid, f"Expected valid hub spec, got errors: {hub_errs}")

            hub_argv = hub_spec.to_docker_run_argv(runtime='docker')
            self.assertEqual(hub_argv[:5], ['docker', 'run', '-d', '--name', HUB_CONTAINER_NAME])
            self.assertNotIn('--security-opt=seccomp=unconfined', hub_argv)
            self.assertIn('--restart', hub_argv)
            self.assertIn('unless-stopped', hub_argv)
            self.assertIn(f"{ws_root}:{ws_root}", hub_argv)
            self.assertIn(f"{(ws_root / 'config').resolve()}:{(ws_root / 'config').resolve()}:ro", hub_argv)
            self.assertIn(f"{(ws_root / 'audit').resolve()}:{(ws_root / 'audit').resolve()}:ro", hub_argv)
            self.assertIn(DEFAULT_HUB_IMAGE, hub_argv)

    def test_validate_launch_spec_rejections(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as outside_dir:
            ws_root = Path(temp_dir).resolve()
            outside_path = Path(outside_dir).resolve()
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            sess_a = (ws_root / 'sessions' / 'session-a').resolve()
            sess_b = (ws_root / 'sessions' / 'session-b').resolve()
            sess_a.mkdir(parents=True, exist_ok=True)
            sess_b.mkdir(parents=True, exist_ok=True)

            # 1. Disallowed image
            bad_img_spec = build_session_launch_spec('session-a', sess_a, ws_root, custom_image='evil/miner:v1')
            valid, errs = validate_launch_spec(bad_img_spec)
            self.assertFalse(valid)
            self.assertTrue(any('not in the allowed image list' in e for e in errs))

            # Allowed when added to policy.containers.allowed_images
            custom_policy = HarnessPolicy(containers=ContainerPolicy(allowed_images=['evil/miner:*']))
            valid_custom, _ = validate_launch_spec(bad_img_spec, policy=custom_policy)
            self.assertTrue(valid_custom)

            # 2. Mounting another session's directory
            cross_sess_spec = build_session_launch_spec('session-a', sess_a, ws_root)
            cross_sess_spec.mounts.append(MountSpec(source=str(sess_b), target=str(sess_b), read_only=False))
            valid, errs = validate_launch_spec(cross_sess_spec)
            self.assertFalse(valid)
            self.assertTrue(any("cannot mount another session's directory" in e for e in errs))

            # 3. Path traversal with '..'
            dotdot_spec = build_session_launch_spec('session-a', sess_a, ws_root)
            dotdot_spec.mounts.append(
                MountSpec(
                    source=f"{sess_a}/../session-b",
                    target=f"{sess_a}/../session-b",
                    read_only=True,
                )
            )
            valid, errs = validate_launch_spec(dotdot_spec)
            self.assertFalse(valid)
            self.assertTrue(any("'..' path traversal" in e for e in errs))

            # 4. Symlink escape outside workspace_root
            symlink_escape = sess_a / 'escape_link'
            try:
                symlink_escape.symlink_to(outside_path, target_is_directory=True)
                sym_spec = build_session_launch_spec('session-a', sess_a, ws_root)
                sym_spec.mounts.append(
                    MountSpec(source=str(symlink_escape), target=str(symlink_escape), read_only=True)
                )
                valid, errs = validate_launch_spec(sym_spec)
                self.assertFalse(valid)
                self.assertTrue(any('outside workspace root' in e for e in errs))
            except OSError:
                # Windows without developer mode may disallow symlinks
                pass

            # 5. Forbidden mounts: docker.sock, ~/.ssh, ~/.config/gh, ~/.local/share/opencode, gh binary
            for bad_mount in (
                '/var/run/docker.sock',
                '/Users/alice/.ssh',
                '/home/alice/.config/gh',
                '/home/alice/.local/share/opencode',
                '/usr/bin/gh',
            ):
                f_spec = build_session_launch_spec('session-a', sess_a, ws_root)
                f_spec.mounts.append(MountSpec(source=bad_mount, target=bad_mount, read_only=True))
                valid, errs = validate_launch_spec(f_spec)
                self.assertFalse(valid, f"Expected {bad_mount} to be rejected")
                self.assertTrue(any('forbidden' in e.lower() for e in errs))

            # 6. Forbidden flags: --privileged, --network=host, --pid=host, --cap-add, --device
            for bad_flags in (
                ['--privileged'],
                ['--network=host'],
                ['--network', 'host'],
                ['--pid=host'],
                ['--cap-add=SYS_ADMIN'],
                ['--device=/dev/kvm'],
            ):
                flag_spec = build_session_launch_spec('session-a', sess_a, ws_root)
                flag_spec.extra_flags = list(bad_flags)
                valid, errs = validate_launch_spec(flag_spec)
                self.assertFalse(valid, f"Expected {bad_flags} to be rejected")
                self.assertTrue(any('prohibited' in e.lower() for e in errs))

            # 7. seccomp=unconfined forbidden on hub role
            hub_seccomp = build_hub_launch_spec(ws_root)
            hub_seccomp.security_opts = ['seccomp=unconfined']
            valid, errs = validate_launch_spec(hub_seccomp)
            self.assertFalse(valid)
            self.assertTrue(any('seccomp=unconfined' in e for e in errs))

            # 8. Non-loopback port publish rejected
            wide_port_spec = build_session_launch_spec(
                'session-a',
                sess_a,
                ws_root,
                ports=[PortPublishSpec(host_ip='0.0.0.0', host_port=4096, container_port=4096)],
            )
            valid, errs = validate_launch_spec(wide_port_spec)
            self.assertFalse(valid)
            self.assertTrue(any("must bind strictly to '127.0.0.1'" in e for e in errs))

            # 9. Non-identical mount source and target rejected
            mismatch_spec = build_session_launch_spec('session-a', sess_a, ws_root)
            mismatch_spec.mounts = [MountSpec(source=str(sess_a), target='/workspace', read_only=False)]
            valid, errs = validate_launch_spec(mismatch_spec)
            self.assertFalse(valid)
            self.assertTrue(any('identical-path mount requirement' in e for e in errs))

    def test_verify_and_inspect_container_mounts(self):
        from ros_maintainer_agent_harness.runner import FakeCommandRunner

        with tempfile.TemporaryDirectory() as temp_dir:
            ws_root = Path(temp_dir).resolve()
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            sess_dir = (ws_root / 'sessions' / 'session-pr-77').resolve()
            sess_dir.mkdir(parents=True, exist_ok=True)

            expected_spec = build_session_launch_spec('session-pr-77', sess_dir, ws_root)

            matching_inspect_payload = [
                {
                    'Name': '/ros-harness-session-pr-77',
                    'HostConfig': {'Privileged': False, 'NetworkMode': 'default'},
                    'Mounts': [
                        {
                            'Type': 'bind',
                            'Source': m.source,
                            'Destination': m.target,
                            'RW': not m.read_only,
                        }
                        for m in expected_spec.mounts
                    ],
                }
            ]
            report = verify_container_mounts_against_spec(matching_inspect_payload, expected_spec)
            self.assertTrue(report['verified'], f"Unexpected drifts: {report['drifts']}")
            self.assertEqual(report['drifts'], [])

            # Drift: shared_repos mounted RW when RO was expected + unexpected extra mount + Privileged=True
            drifted_mounts = []
            for m in expected_spec.mounts:
                if 'shared_repos' in m.source:
                    drifted_mounts.append(
                        {'Type': 'bind', 'Source': m.source, 'Destination': m.target, 'RW': True}
                    )
                else:
                    drifted_mounts.append(
                        {'Type': 'bind', 'Source': m.source, 'Destination': m.target, 'RW': not m.read_only}
                    )
            drifted_mounts.append(
                {
                    'Type': 'bind',
                    'Source': str(ws_root / 'audit'),
                    'Destination': str(ws_root / 'audit'),
                    'RW': False,
                }
            )
            drifted_payload = [
                {
                    'Name': '/ros-harness-session-pr-77',
                    'HostConfig': {'Privileged': True, 'NetworkMode': 'host'},
                    'Mounts': drifted_mounts,
                }
            ]
            drift_report = verify_container_mounts_against_spec(drifted_payload, expected_spec)
            self.assertFalse(drift_report['verified'])
            self.assertTrue(any('Privileged=true' in d for d in drift_report['drifts']))
            self.assertTrue(any("NetworkMode='host'" in d for d in drift_report['drifts']))
            self.assertTrue(any('Mount mode drift' in d for d in drift_report['drifts']))
            self.assertTrue(any('Unexpected extra mount' in d for d in drift_report['drifts']))

            # Test inspect_session_container_mounts and inspect_hub_container_mounts via FakeCommandRunner
            runner = FakeCommandRunner(available_binaries={'docker': '/usr/bin/docker'})
            runner.add_prefix_response(
                ['docker', 'inspect', 'ros-harness-session-pr-77'],
                returncode=0,
                stdout=json.dumps(matching_inspect_payload),
            )
            sess_inspect_res = inspect_session_container_mounts(
                session_id='session-pr-77',
                workspace_root=ws_root,
                runner=runner,
            )
            self.assertTrue(sess_inspect_res['verified'])

            hub_spec = build_hub_launch_spec(ws_root)
            hub_payload = [
                {
                    'Name': f'/{HUB_CONTAINER_NAME}',
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
            runner.add_prefix_response(
                ['docker', 'inspect', HUB_CONTAINER_NAME],
                returncode=0,
                stdout=json.dumps(hub_payload),
            )
            hub_inspect_res = inspect_hub_container_mounts(workspace_root=ws_root, runner=runner)
            self.assertTrue(hub_inspect_res['verified'])

    def test_session_opencode_lifecycle_and_concurrency_limit(self):
        from ros_maintainer_agent_harness.devcontainer import (
            attach_to_session_opencode,
            get_session_attach_info,
            load_session_attach_state,
            start_session_container,
            stop_session_container,
        )
        from ros_maintainer_agent_harness.runner import FakeCommandRunner

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            ws_root = root / 'ws'
            state_dir = root / 'state'
            layout = WorkspaceLayout(ws_root)
            layout.initialize()

            # Set max_concurrent_sessions to 1 in policy.yaml
            import yaml

            policy_path = ws_root / 'config' / 'policy.yaml'
            policy_data = yaml.safe_load(policy_path.read_text(encoding='utf-8')) or {}
            policy_data.setdefault('policies', {}).setdefault('containers', {})['max_concurrent_sessions'] = 1
            policy_path.write_text(yaml.safe_dump(policy_data), encoding='utf-8')

            sess1_dir = ws_root / 'sessions' / 'sess-1'
            sess2_dir = ws_root / 'sessions' / 'sess-2'
            sess1_dir.mkdir(parents=True)
            sess2_dir.mkdir(parents=True)

            runner = FakeCommandRunner(
                available_binaries={'docker': '/usr/bin/docker', 'opencode': '/usr/bin/opencode'}
            )
            runner.add_canned(['inspect', 'ros-harness-sess-1'], returncode=1, stderr='No such object')
            runner.add_canned(['inspect', 'ros-harness-sess-2'], returncode=1, stderr='No such object')
            runner.add_canned(['docker', 'run', '-d'], returncode=0, stdout='cid-sess-1\n')

            start1 = start_session_container(
                session_id='sess-1',
                session_dir=sess1_dir,
                workspace_root=ws_root,
                runner=runner,
                state_dir=state_dir,
            )
            self.assertTrue(start1['success'])
            self.assertIsNotNone(start1.get('opencode_port'))
            self.assertTrue((sess1_dir / 'opencode.json').is_file())

            state1 = load_session_attach_state('sess-1', state_dir=state_dir)
            self.assertEqual(state1['port'], start1['opencode_port'])
            self.assertTrue(str(state1['password']).startswith('rmah_oc_'))

            # Mark sess-1 container as running
            runner.canned.clear()
            runner.add_canned(['inspect', 'ros-harness-sess-1'], returncode=0, stdout='true\n')
            runner.add_canned(['inspect', 'ros-harness-sess-2'], returncode=1, stderr='No such object')

            attach_info = get_session_attach_info(
                session_id='sess-1',
                workspace_root=ws_root,
                session_dir=sess1_dir,
                state_dir=state_dir,
                runner=runner,
            )
            self.assertTrue(attach_info['container_running'])
            self.assertTrue(attach_info['agent_running'])
            self.assertTrue(attach_info['password_configured'])
            # Password value must never be exposed in get_session_attach_info
            self.assertNotIn(state1['password'], json.dumps(attach_info))

            # Test attach_to_session_opencode passes password via env, not argv
            att_res = attach_to_session_opencode(
                session_id='sess-1',
                workspace_root=ws_root,
                runner=runner,
                state_dir=state_dir,
            )
            self.assertTrue(att_res['success'])
            att_call = runner.calls[-1]
            self.assertEqual(att_call.argv[:2], ['opencode', 'attach'])
            self.assertNotIn(state1['password'], ' '.join(att_call.argv))
            self.assertEqual(att_call.env.get('OPENCODE_SERVER_PASSWORD'), state1['password'])

            # Starting sess-2 while sess-1 is running and max_concurrent_sessions=1 must fail
            start2 = start_session_container(
                session_id='sess-2',
                session_dir=sess2_dir,
                workspace_root=ws_root,
                runner=runner,
                state_dir=state_dir,
            )
            self.assertFalse(start2['success'])
            self.assertEqual(start2['status'], 'max_concurrent_sessions_exceeded')

            # Stopping sess-1 revokes its token and frees its port
            stop1 = stop_session_container(
                session_id='sess-1',
                runner=runner,
                state_dir=state_dir,
            )
            self.assertTrue(stop1['success'])
            self.assertEqual(stop1['freed_port'], start1['opencode_port'])
            self.assertGreaterEqual(stop1['revoked_tokens'], 1)
            self.assertEqual(load_session_attach_state('sess-1', state_dir=state_dir), {})


if __name__ == '__main__':
    unittest.main()
