"""Standalone stdlib-only worker for process-separated harness components.

This file is copied into a private execution directory. It must not import EvoGroup
or inherit the host interpreter's site packages.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import os
import resource
import sys
import tempfile
from pathlib import Path

MAX_LINE = 8 * 1024 * 1024
MAX_RPC = 4096
PROTOCOL_OUT = sys.stdout
PROTOCOL_IN = sys.stdin


def emit(frame):
    encoded = json.dumps(frame, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_LINE:
        raise ValueError("frame_limit")
    PROTOCOL_OUT.write(encoded + "\n")
    PROTOCOL_OUT.flush()


class DiscardOutput(io.TextIOBase):
    """Ignore component prints without accumulating candidate-controlled strings."""

    def write(self, value):
        return len(value)

    def flush(self):
        pass


class Context:
    def __init__(self):
        self.calls = 0

    def call(self, name, arguments):
        self.calls += 1
        if self.calls > MAX_RPC or not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("invalid_capability_request")
        emit({"type": "rpc", "id": self.calls, "name": name, "arguments": arguments})
        raw = PROTOCOL_IN.readline(MAX_LINE + 2)
        if not raw.endswith("\n") or len(raw.encode("utf-8")) > MAX_LINE + 1:
            raise ValueError("invalid_capability_response")
        response = json.loads(raw)
        if response.get("type") != "response" or response.get("id") != self.calls:
            raise ValueError("invalid_capability_response")
        if not response.get("ok"):
            raise PermissionError("capability_denied")
        return response["value"]


def limits(timeout):
    # Hard and soft limits are equal so candidate code cannot raise them.
    values = [
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, 8 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_CPU, max(int(timeout) + 1, 1)),
    ]
    if sys.platform != "darwin":
        values.append((resource.RLIMIT_AS, 256 * 1024 * 1024))
    for kind, requested in values:
        _, hard = resource.getrlimit(kind)
        bound = requested if hard == resource.RLIM_INFINITY else min(requested, hard)
        resource.setrlimit(kind, (bound, bound))


def main():
    raw = PROTOCOL_IN.readline(MAX_LINE + 2)
    if not raw.endswith("\n") or len(raw.encode("utf-8")) > MAX_LINE + 1:
        emit({"type": "error", "code": "invalid_request"})
        return 1
    try:
        request = json.loads(raw)
        limits(request["timeout"])
        # Keep ordinary tempfile helpers within this execution's scratch even
        # after removing all inherited environment variables.
        tempfile.tempdir = os.getcwd()
        bundle = Path(request["bundle"]).resolve()
        sys.path.insert(0, str(bundle))
        entrypoint = request["entrypoint"]
        _, relative, symbol = entrypoint.split(":")
        path = (bundle / relative).resolve()
        if not path.is_relative_to(bundle) or not path.is_file():
            raise ValueError("invalid_entrypoint")
        # Modules and package state live only in this worker process.
        module_name = relative.removesuffix(".py").replace("/", ".")
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ValueError("invalid_entrypoint")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        with (
            contextlib.redirect_stdout(DiscardOutput()),
            contextlib.redirect_stderr(DiscardOutput()),
        ):
            spec.loader.exec_module(module)
            handler = getattr(module, symbol)
            if not callable(handler) or inspect.iscoroutinefunction(handler):
                raise TypeError("invalid_handler")
            inspect.signature(handler).bind({}, Context())
            if request.get("validate_only"):
                result = {"valid": True}
            else:
                result = handler(request["payload"], Context())
        emit({"type": "result", "value": result})
        return 0
    except BaseException:
        # Candidate exceptions may contain private host-returned source data.
        # Only a fixed code crosses the boundary.
        emit({"type": "error", "code": "component_failed"})
        return 1


if __name__ == "__main__":
    os.environ.clear()
    raise SystemExit(main())
