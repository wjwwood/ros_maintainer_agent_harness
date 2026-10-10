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

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple, Union


@dataclass
class CommandResult:
    """Result of executing an external command via a CommandRunner."""

    args: List[str]
    returncode: int = 0
    stdout: str = ''
    stderr: str = ''


@dataclass
class RecordedCall:
    """Recorded invocation on a FakeCommandRunner."""

    args: List[str]
    env: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    timeout: Optional[float] = None
    input_data: Optional[str] = None


class CommandRunner(Protocol):
    """Injectable command execution seam for container runtime and host CLI invocations."""

    def which(self, binary: str) -> Optional[str]:
        ...

    def run(
        self,
        cmd: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[Union[str, Path]] = None,
        timeout: Optional[float] = None,
        input_data: Optional[str] = None,
    ) -> CommandResult:
        ...


class SubprocessRunner:
    """Default CommandRunner backed by subprocess.run and shutil.which."""

    def which(self, binary: str) -> Optional[str]:
        return shutil.which(binary)

    def run(
        self,
        cmd: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[Union[str, Path]] = None,
        timeout: Optional[float] = None,
        input_data: Optional[str] = None,
    ) -> CommandResult:
        argv = [str(part) for part in cmd]
        run_kwargs: Dict[str, Any] = {
            'capture_output': True,
            'text': True,
        }
        if env is not None:
            run_kwargs['env'] = dict(env)
        if cwd is not None:
            run_kwargs['cwd'] = str(cwd)
        if timeout is not None:
            run_kwargs['timeout'] = timeout
        if input_data is not None:
            run_kwargs['input'] = input_data

        completed = subprocess.run(argv, **run_kwargs)
        return CommandResult(
            args=argv,
            returncode=int(completed.returncode),
            stdout=completed.stdout or '',
            stderr=completed.stderr or '',
        )


ResponseHandler = Union[
    CommandResult,
    Exception,
    Callable[[List[str], RecordedCall], Union[CommandResult, Exception]],
]


@dataclass
class FakeCommandRunner:
    """
    Deterministic in-memory CommandRunner for unit tests.

    Allows tests to register canned responses (or exceptions such as subprocess.TimeoutExpired)
    matched by predicate or command prefix, and inspect `calls` afterward.
    """

    available_binaries: Dict[str, Optional[str]] = field(
        default_factory=lambda: {'docker': '/usr/bin/docker', 'gh': '/usr/bin/gh'}
    )
    default_returncode: int = 0
    default_stdout: str = ''
    default_stderr: str = ''
    calls: List[RecordedCall] = field(default_factory=list)
    _handlers: List[Tuple[Callable[[List[str]], bool], ResponseHandler]] = field(default_factory=list)

    def which(self, binary: str) -> Optional[str]:
        if binary in self.available_binaries:
            return self.available_binaries[binary]
        return None

    def add_handler(
        self,
        predicate: Callable[[List[str]], bool],
        response: ResponseHandler,
    ) -> None:
        """Register a response or callable handler for commands matching `predicate`."""
        self._handlers.append((predicate, response))

    def add_prefix_response(
        self,
        prefix: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str = '',
        stderr: str = '',
    ) -> None:
        """Register a canned CommandResult for any command starting with `prefix`."""
        prefix_list = [str(p) for p in prefix]

        def _matches(argv: List[str]) -> bool:
            return len(argv) >= len(prefix_list) and argv[:len(prefix_list)] == prefix_list

        self.add_handler(
            _matches,
            CommandResult(
                args=prefix_list,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            ),
        )

    def run(
        self,
        cmd: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[Union[str, Path]] = None,
        timeout: Optional[float] = None,
        input_data: Optional[str] = None,
    ) -> CommandResult:
        argv = [str(part) for part in cmd]
        recorded = RecordedCall(
            args=argv,
            env=dict(env) if env is not None else None,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
            input_data=input_data,
        )
        self.calls.append(recorded)

        for predicate, handler in self._handlers:
            if predicate(argv):
                if isinstance(handler, Exception):
                    raise handler
                if callable(handler):
                    outcome = handler(argv, recorded)
                    if isinstance(outcome, Exception):
                        raise outcome
                    return CommandResult(
                        args=argv,
                        returncode=outcome.returncode,
                        stdout=outcome.stdout,
                        stderr=outcome.stderr,
                    )
                return CommandResult(
                    args=argv,
                    returncode=handler.returncode,
                    stdout=handler.stdout,
                    stderr=handler.stderr,
                )

        return CommandResult(
            args=argv,
            returncode=self.default_returncode,
            stdout=self.default_stdout,
            stderr=self.default_stderr,
        )


_DEFAULT_RUNNER: CommandRunner = SubprocessRunner()


def get_default_runner() -> CommandRunner:
    """Return the active default CommandRunner."""
    return _DEFAULT_RUNNER


def set_default_runner(runner: Optional[CommandRunner]) -> CommandRunner:
    """Set or reset the default CommandRunner (pass None to restore SubprocessRunner)."""
    global _DEFAULT_RUNNER
    _DEFAULT_RUNNER = runner if runner is not None else SubprocessRunner()
    return _DEFAULT_RUNNER
