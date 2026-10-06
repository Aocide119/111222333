"""Fresh-process execution of file-based harness implementations.

Workspace separation and a scrubbed environment prevent accidental state sharing;
they are not an OS security sandbox. Python components execute with the host
user's filesystem permissions and must be treated as trusted code.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from evog.core.errors import DeadlineExceeded, EvoGError

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_CAPABILITY_BYTES = 64 * 1024 * 1024
MAX_RPC_CALLS = 4096
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
ENTRYPOINT = re.compile(
    r"python:((?:tools/implementations|middleware)/[a-zA-Z0-9_./-]+\.py):([a-zA-Z_]\w*)\Z"
)


class ComponentExecutionError(EvoGError):
    """A bounded error without candidate exception text or host path disclosure."""


def _encode(value: Any) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError, OverflowError) as exc:
        raise ComponentExecutionError("Component data must be finite JSON") from exc
    if len(encoded) > MAX_FRAME_BYTES + 1:
        raise ComponentExecutionError("Component frame exceeds the size limit")
    return encoded


def _entrypoint(value: str) -> str:
    match = ENTRYPOINT.fullmatch(value)
    if match is None:
        raise ComponentExecutionError("Invalid component entrypoint")
    relative = match.group(1)
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ComponentExecutionError("Invalid component entrypoint")
    return relative


def _copy_bundle(contents: dict[str, str], bundle: Path) -> None:
    total = 0
    if len(contents) > 256:
        raise ComponentExecutionError("Component bundle exceeds the file limit")
    for relative, content in contents.items():
        if not isinstance(relative, str):
            raise ComponentExecutionError("Invalid component bundle path")
        path = PurePosixPath(relative)
        if (
            not isinstance(content, str)
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or "\\" in relative
        ):
            raise ComponentExecutionError("Invalid component bundle path")
        encoded = content.encode("utf-8")
        total += len(encoded)
        if total > MAX_BUNDLE_BYTES:
            raise ComponentExecutionError("Component bundle exceeds the size limit")
        target = bundle.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        target.chmod(0o444)
    for directory in sorted(bundle.rglob("*"), reverse=True):
        if directory.is_dir():
            directory.chmod(0o555)
    bundle.chmod(0o555)


def _terminate(process: subprocess.Popen) -> None:
    # A component can exit before its descendants; kill the group in either case.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=2)


def _bundle_unchanged(contents: dict[str, str], bundle: Path) -> bool:
    actual = {str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file()}
    if actual != set(contents):
        return False
    return all(
        not bundle.joinpath(relative).is_symlink()
        and bundle.joinpath(relative).read_bytes() == content.encode("utf-8")
        for relative, content in contents.items()
    )


def execute_component(
    contents: dict[str, str],
    entrypoint: str,
    payload: dict[str, Any],
    capabilities: Callable[[str, dict[str, Any]], Any] | None = None,
    timeout_seconds: float = 10,
    *,
    validate_only: bool = False,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    max_capability_bytes: int = MAX_CAPABILITY_BYTES,
) -> Any:
    """Run a candidate in a fresh process and private temporary workspace.

    The callback must enforce its own scope for every supported capability. The
    worker receives no provider credentials or inherited project imports. This
    is directory/process isolation, not an OS security boundary against hostile
    Python: the worker still has the invoking user's filesystem permissions.
    """
    relative = _entrypoint(entrypoint)
    if relative not in contents:
        raise ComponentExecutionError("Component entrypoint is missing")
    if not isinstance(payload, dict):
        raise ComponentExecutionError("Component payload must be an object")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
        raise ComponentExecutionError("Invalid component timeout")
    if (
        type(max_output_bytes) is not int
        or type(max_capability_bytes) is not int
        or not 1024 <= max_output_bytes <= 256 * 1024 * 1024
        or not 1024 <= max_capability_bytes <= 256 * 1024 * 1024
    ):
        raise ComponentExecutionError("Invalid component transfer limit")
    deadline = time.monotonic() + timeout_seconds
    with tempfile.TemporaryDirectory(prefix="evog-component-") as temporary:
        work = Path(temporary).resolve()
        bundle, scratch = work / "bundle", work / "scratch"
        bundle.mkdir()
        scratch.mkdir()
        _copy_bundle(contents, bundle)
        shutil.copyfile(Path(__file__).with_name("worker.py"), work / "component_worker.py")
        (work / "component_worker.py").chmod(0o444)
        command = [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-B",
            "-u",
            str(work / "component_worker.py"),
        ]
        request = _encode(
            {
                "bundle": str(bundle),
                "entrypoint": entrypoint,
                "payload": payload,
                "timeout": timeout_seconds,
                "validate_only": validate_only,
            }
        )
        environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": str(scratch)}
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=scratch,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise ComponentExecutionError("Component worker could not start") from exc
        assert process.stdin and process.stdout and process.stderr
        selector = selectors.DefaultSelector()
        for stream, tag in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, tag)
        os.set_blocking(process.stdin.fileno(), False)
        pending = bytearray(request)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        buffered = bytearray()
        total_output = capability_bytes = calls = 0
        terminal = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ComponentExecutionError("Component execution timed out")
                for key, _ in selector.select(min(remaining, 0.1)):
                    stream, tag = key.fileobj, key.data
                    if tag == "stdin":
                        try:
                            sent = os.write(stream.fileno(), pending[:65536])
                        except BrokenPipeError as exc:
                            raise ComponentExecutionError(
                                "Component worker exited unexpectedly"
                            ) from exc
                        del pending[:sent]
                        if not pending:
                            selector.unregister(stream)
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    total_output += len(chunk)
                    if total_output > max_output_bytes:
                        raise ComponentExecutionError("Component output exceeds the size limit")
                    if tag == "stderr":
                        continue
                    buffered.extend(chunk)
                    while b"\n" in buffered:
                        raw, _, buffered = buffered.partition(b"\n")
                        if len(raw) > MAX_FRAME_BYTES or terminal is not None:
                            raise ComponentExecutionError("Invalid component protocol")
                        try:
                            frame = json.loads(
                                raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                            )
                        except (ValueError, UnicodeDecodeError) as exc:
                            raise ComponentExecutionError("Invalid component protocol") from exc
                        if not isinstance(frame, dict):
                            raise ComponentExecutionError("Invalid component protocol")
                        if frame.get("type") == "rpc":
                            calls += 1
                            if (
                                calls > MAX_RPC_CALLS
                                or type(frame.get("id")) is not int
                                or frame["id"] != calls
                                or not isinstance(frame.get("name"), str)
                                or len(frame["name"]) > 80
                                or not isinstance(frame.get("arguments"), dict)
                            ):
                                raise ComponentExecutionError(
                                    "Invalid component capability request"
                                )
                            response = {"type": "response", "id": calls, "ok": False}
                            if capabilities is not None:
                                try:
                                    response.update(
                                        ok=True,
                                        value=capabilities(frame["name"], frame["arguments"]),
                                    )
                                except Exception:
                                    pass
                            response_bytes = _encode(response)
                            capability_bytes += len(response_bytes)
                            if capability_bytes > max_capability_bytes:
                                raise ComponentExecutionError(
                                    "Component capabilities exceed the transfer limit"
                                )
                            pending.extend(response_bytes)
                            if process.stdin not in [
                                item.fileobj for item in selector.get_map().values()
                            ]:
                                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                        elif frame.get("type") in {"result", "error"}:
                            terminal = frame
                        else:
                            raise ComponentExecutionError("Invalid component protocol")
                    if len(buffered) > MAX_FRAME_BYTES:
                        raise ComponentExecutionError("Component output exceeds the frame limit")
                if process.poll() is not None and not selector.get_map():
                    break
            if buffered or terminal is None:
                raise ComponentExecutionError("Component worker exited without a valid result")
            process.wait(timeout=max(deadline - time.monotonic(), 0.01))
            if process.returncode != 0 or terminal["type"] != "result" or "value" not in terminal:
                raise ComponentExecutionError("Component execution failed")
            if not _bundle_unchanged(contents, bundle):
                raise ComponentExecutionError("Component modified its execution bundle")
            return terminal["value"]
        except subprocess.TimeoutExpired as exc:
            raise ComponentExecutionError("Component execution timed out") from exc
        finally:
            selector.close()
            _terminate(process)
            for stream in [process.stdin, process.stdout, process.stderr]:
                stream.close()
            # Restore directory permissions so cleanup works on all supported hosts.
            bundle.chmod(0o755)
            for directory in bundle.rglob("*"):
                if directory.is_dir() and not directory.is_symlink():
                    directory.chmod(0o755)


def validate_component(
    contents: dict[str, str], entrypoint: str, timeout_seconds: float = 10
) -> None:
    """Import and check the entrypoint in a fresh worker, without calling it."""
    execute_component(contents, entrypoint, {}, timeout_seconds=timeout_seconds, validate_only=True)


class MiddlewarePipeline:
    def __init__(self, harness: Any, capabilities=None, timeout_seconds: float = 10):
        self.contents = harness.contents
        self.registry = harness.middleware_registry
        self.capabilities = capabilities
        self.timeout_seconds = timeout_seconds

    def run(
        self, hook: str, payload: dict[str, Any], *, deadline: float | None = None
    ) -> dict[str, Any]:
        current = dict(payload)
        changes = {}
        for spec in self.registry:
            if not spec.get("enabled", True) or spec["hook"] != hook:
                continue
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise DeadlineExceeded("Question deadline reached during middleware")
            timeout = (
                min(self.timeout_seconds, remaining)
                if remaining is not None
                else self.timeout_seconds
            )
            try:
                result = execute_component(
                    self.contents,
                    spec["handler"],
                    {**current, "hook": hook, "config": spec.get("config", {})},
                    self.capabilities,
                    timeout,
                )
            except ComponentExecutionError as exc:
                if deadline is not None and time.monotonic() >= deadline:
                    raise DeadlineExceeded("Question deadline reached during middleware") from exc
                raise
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("Question deadline reached during middleware")
            if not isinstance(result, dict):
                raise ComponentExecutionError("Middleware must return an object")
            changes.update(result)
            current.update(result)
        return changes
