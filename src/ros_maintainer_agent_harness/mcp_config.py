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

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_harness_executable() -> str:
    """Resolve the path or command name for ros-maintainer-harness."""
    # Check if currently running within a python script or binary
    local_bin = Path.home() / '.local' / 'bin' / 'ros-maintainer-harness'
    if local_bin.exists():
        return str(local_bin)
    return 'ros-maintainer-harness'


def generate_mcp_server_entry(
    workspace_path: Path,
    transport: str = 'stdio',
    host: str = '127.0.0.1',
    port: int = 8765,
    executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate configuration block for the Host MCP Server Gateway."""
    exe = executable or get_harness_executable()
    ws_str = str(workspace_path.resolve())

    if transport == 'stdio':
        return {
            'command': exe,
            'args': ['serve', '--transport', 'stdio', '-w', ws_str],
            'env': {
                'ROS_MAINTAINER_WS': ws_str,
                'PYTHONUNBUFFERED': '1',
            },
        }
    elif transport in ('sse', 'streamable-http'):
        url = f"http://{host}:{port}/sse" if transport == 'sse' else f"http://{host}:{port}/mcp"
        return {
            'url': url,
            'type': transport,
        }
    else:
        raise ValueError(f"Unsupported transport: '{transport}'. Choose 'stdio', 'sse', or 'streamable-http'.")


def generate_mcp_config_dict(
    workspace_path: Path,
    transport: str = 'stdio',
    host: str = '127.0.0.1',
    port: int = 8765,
    executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a standard mcpServers wrapper dictionary."""
    server_entry = generate_mcp_server_entry(
        workspace_path=workspace_path,
        transport=transport,
        host=host,
        port=port,
        executable=executable,
    )
    return {
        'mcpServers': {
            'ros-maintainer-harness': server_entry,
        }
    }


def write_session_mcp_configs(
    session_dir: Path,
    workspace_path: Path,
    transport: str = 'stdio',
    host: str = '127.0.0.1',
    port: int = 8765,
    formats: Optional[List[str]] = None,
) -> Dict[str, Path]:
    """
    Write MCP client config files for various editors and agents inside a session directory.

    Supported formats: 'generic' (mcp.json), 'claude' (.mcp.json), 'cursor' (.cursor/mcp.json),
    'vscode' (.vscode/mcp.json).
    """
    selected_formats = formats or ['generic', 'claude', 'cursor', 'vscode']
    config_data = generate_mcp_config_dict(
        workspace_path=workspace_path,
        transport=transport,
        host=host,
        port=port,
    )
    config_json = json.dumps(config_data, indent=2) + '\n'

    written_files: Dict[str, Path] = {}

    if 'generic' in selected_formats:
        p = session_dir / 'mcp.json'
        p.write_text(config_json, encoding='utf-8')
        written_files['generic'] = p

    if 'claude' in selected_formats:
        p = session_dir / '.mcp.json'
        p.write_text(config_json, encoding='utf-8')
        written_files['claude'] = p

    if 'cursor' in selected_formats:
        cursor_dir = session_dir / '.cursor'
        cursor_dir.mkdir(parents=True, exist_ok=True)
        p = cursor_dir / 'mcp.json'
        p.write_text(config_json, encoding='utf-8')
        written_files['cursor'] = p

    if 'vscode' in selected_formats:
        vscode_dir = session_dir / '.vscode'
        vscode_dir.mkdir(parents=True, exist_ok=True)
        p = vscode_dir / 'mcp.json'
        p.write_text(config_json, encoding='utf-8')
        written_files['vscode'] = p

    return written_files


def get_agent_launch_info(
    session_id: str,
    session_dir: Path,
    workspace_path: Path,
    distro: str = 'rolling',
    agent: str = 'claude',
) -> Dict[str, Any]:
    """
    Build environment variables, MCP options, and CLI command line for launching an AI agent in a session.
    """
    env_vars = {
        'ROS_MAINTAINER_SESSION_ID': session_id,
        'ROS_MAINTAINER_WS': str(workspace_path.resolve()),
        'ROS_DISTRO': distro,
        'ROS_MAINTAINER_GATEWAY_URL': 'http://127.0.0.1:8765',
    }

    if agent == 'claude':
        cmd = ['claude', '--cwd', str(session_dir.resolve())]
        description = f"Launch Claude Code agent in session '{session_id}'"
    elif agent == 'cursor':
        cmd = ['cursor', str(session_dir.resolve())]
        description = f"Open Cursor IDE in session '{session_id}'"
    elif agent == 'code' or agent == 'vscode':
        cmd = ['code', str(session_dir.resolve())]
        description = f"Open VS Code in session '{session_id}'"
    elif agent == 'shell':
        cmd = ['bash']
        description = f"Interactive shell in session '{session_id}' workspace"
    else:
        cmd = [agent, str(session_dir.resolve())]
        description = f"Launch '{agent}' in session '{session_id}'"

    return {
        'session_id': session_id,
        'session_dir': str(session_dir.resolve()),
        'distro': distro,
        'agent': agent,
        'command': cmd,
        'environment': env_vars,
        'description': description,
    }
