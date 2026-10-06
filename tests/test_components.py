"""Regression checks for workspace/process separation, not an OS security sandbox."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from evog.core.errors import DeadlineExceeded
from evog.harness.executor import (
    ComponentExecutionError,
    MiddlewarePipeline,
    execute_component,
    validate_component,
)

ENTRY = "python:tools/implementations/probe.py:execute"
PATH = "tools/implementations/probe.py"


def run(code, payload=None, capabilities=None, **kwargs):
    return execute_component({PATH: code}, ENTRY, payload or {}, capabilities, **kwargs)


def test_fresh_worker_environment_and_import_scope(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-secret")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/project")
    code = """
import os, importlib.util, pathlib, tempfile
def execute(payload, context):
    return {'env': dict(os.environ), 'cwd': str(pathlib.Path.cwd()), 'tempdir': tempfile.gettempdir(),
            'host_import': importlib.util.find_spec('evog') is not None}
"""
    first, second = run(code), run(code)
    assert first["env"] == {}
    assert not first["host_import"]
    assert Path(first["cwd"]).name == "scratch"
    assert first["tempdir"] == first["cwd"]
    assert first["cwd"] != second["cwd"]
    assert not Path(first["cwd"]).exists()


def test_namespace_import_and_checkpoint_reload():
    contents = {
        PATH: "from tools.implementations.helper import VALUE\n"
        "def execute(payload, context): return {'value': VALUE}\n",
        "tools/implementations/helper.py": "VALUE = 1\n",
    }
    assert execute_component(contents, ENTRY, {}) == {"value": 1}
    contents["tools/implementations/helper.py"] = "VALUE = 2\n"
    assert execute_component(contents, ENTRY, {}) == {"value": 2}


def test_capabilities_bounded_and_unknown_denied():
    def capabilities(name, arguments):
        if name != "read_lines":
            raise PermissionError("host details must not cross the boundary")
        return [arguments["path"]]

    assert run(
        "def execute(payload, context): return context.call('read_lines', {'path': 'authorized'})",
        capabilities=capabilities,
    ) == ["authorized"]
    with pytest.raises(ComponentExecutionError, match="execution failed"):
        run(
            "def execute(payload, context): return context.call('unknown', {})",
            capabilities=capabilities,
        )


def test_streaming_tools_can_use_more_than_64_rpc_calls():
    assert run(
        "def execute(payload, context):\n"
        "    return sum(context.call('row', {'i': i}) for i in range(100))\n",
        capabilities=lambda name, arguments: arguments["i"],
    ) == sum(range(100))


def test_validation_imports_but_does_not_call_handler():
    contents = {PATH: "def execute(payload, context): raise RuntimeError('not executed')"}
    validate_component(contents, ENTRY)
    with pytest.raises(ComponentExecutionError):
        validate_component({PATH: "def execute(payload): pass"}, ENTRY)
    with pytest.raises(ComponentExecutionError):
        validate_component({PATH: "import missing_component_dependency"}, ENTRY)


def test_candidate_errors_are_sanitized():
    with pytest.raises(ComponentExecutionError) as exc:
        run("raise RuntimeError('private source and credential contents')")
    assert "private source" not in str(exc.value)
    assert "credential" not in str(exc.value)


def test_print_does_not_break_protocol_and_raw_stdout_is_rejected():
    assert (
        run(
            "print('ordinary import logging')\ndef execute(payload, context): print('log'); return 7"
        )
        == 7
    )
    with pytest.raises(ComponentExecutionError, match="protocol"):
        run("import os\ndef execute(payload, context): os.write(1,b'not-json\\n'); return 7")


def test_duplicate_result_and_nonfinite_json_are_rejected():
    with pytest.raises(ComponentExecutionError, match="protocol"):
        run(
            "import os\ndef execute(payload, context):\n"
            '    os.write(1, b\'{"type":"result","value":1}\\n\')\n    return 2'
        )
    with pytest.raises(ComponentExecutionError):
        run("def execute(payload, context): return float('nan')")


def test_timeout_and_output_limits():
    with pytest.raises(ComponentExecutionError, match="timed out"):
        run("import time\ndef execute(payload, context): time.sleep(3)", timeout_seconds=0.25)
    with pytest.raises(ComponentExecutionError, match="size limit|frame limit"):
        run("import os\ndef execute(payload, context): os.write(1,b'x'*(9*1024*1024)); return 1")


def test_transfer_limit_prevents_unbounded_capability_responses():
    with pytest.raises(ComponentExecutionError, match="transfer limit"):
        run(
            "def execute(payload, context): return context.call('large', {})",
            capabilities=lambda name, arguments: "x" * 2048,
            max_capability_bytes=1024,
        )


def test_descendants_are_cleaned_up_even_after_worker_exits():
    child = []

    def capability(name, arguments):
        child.append(arguments["pid"])
        return True

    code = """
import subprocess, sys
def execute(payload, context):
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(20)'])
    context.call('child_pid', {'pid': child.pid})
    return 1
"""
    with pytest.raises(ComponentExecutionError, match="timed out"):
        run(code, capabilities=capability, timeout_seconds=0.3)
    assert child
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child[0], 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail("Component descendant survived worker cleanup")


def test_modified_bundle_is_rejected_and_cannot_change_original_contents():
    code = """
from pathlib import Path
def execute(payload, context):
    path = Path(__file__)
    path.chmod(0o644)
    path.write_text('modified')
    return 1
"""
    with pytest.raises(ComponentExecutionError, match="modified its execution bundle"):
        run(code)


@pytest.mark.parametrize(
    "entrypoint",
    [
        "python:tools/implementations/../probe.py:execute",
        "python:tools/implementations/./probe.py:execute",
        "python:/tmp/probe.py:execute",
        "python:tools/implementations/probe.py:execute:extra",
    ],
)
def test_invalid_entrypoint(entrypoint):
    with pytest.raises(ComponentExecutionError, match="entrypoint"):
        execute_component({PATH: "def execute(payload, context): return {}"}, entrypoint, {})


def test_parallel_workers_have_distinct_scratch_and_no_module_state():
    code = """
from pathlib import Path
COUNT = 0
def execute(payload, context):
    global COUNT
    COUNT += 1
    Path('note.txt').write_text(str(payload['id']))
    return {'count': COUNT, 'id': Path('note.txt').read_text(), 'cwd': str(Path.cwd())}
"""
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: run(code, {"id": i}), range(4)))
    assert {result["count"] for result in results} == {1}
    assert len({result["cwd"] for result in results}) == 4
    assert [result["id"] for result in results] == ["0", "1", "2", "3"]


def test_middleware_uses_registry_order_and_disabled_entries():
    paths = {
        "middleware/first.py": "def execute(payload, context): return {'value': payload['value'] + 1}",
        "middleware/second.py": "def execute(payload, context): return {'value': payload['value'] * 2}",
    }
    registry = [
        {"name": "a", "handler": "python:middleware/first.py:execute", "hook": "before_model"},
        {
            "name": "disabled",
            "handler": "python:middleware/first.py:execute",
            "hook": "before_model",
            "enabled": False,
        },
        {"name": "b", "handler": "python:middleware/second.py:execute", "hook": "before_model"},
    ]
    pipeline = MiddlewarePipeline(SimpleNamespace(contents=paths, middleware_registry=registry))
    assert pipeline.run("before_model", {"value": 2}) == {"value": 6}
    assert pipeline.run("after_tool", {"value": 2}) == {}


def test_pipeline_recomputes_remaining_deadline_for_each_entry():
    path = "middleware/slow.py"
    contents = {
        path: "import time\ndef execute(payload, context): time.sleep(0.12); return {'value': 1}"
    }
    registry = [
        {"name": name, "handler": "python:" + path + ":execute", "hook": "before_model"}
        for name in ["first", "second"]
    ]
    pipeline = MiddlewarePipeline(SimpleNamespace(contents=contents, middleware_registry=registry))
    started = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        pipeline.run("before_model", {}, deadline=started + 0.2)
    assert time.monotonic() - started < 1.5


def test_default_middleware_preserves_task_anchors_and_source_results():
    root = Path(__file__).parents[1] / "src/evog/harness/base/middleware"
    contents = {
        "middleware/" + name: (root / name).read_text()
        for name in ["context_compaction.py", "long_tool_output.py", "budget_reminder.py"]
    }
    messages = [
        {"role": "system", "content": "fixed"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "x" * 1000},
        {"role": "tool", "content": "old"},
    ]
    result = execute_component(
        contents,
        "python:middleware/context_compaction.py:execute",
        {"messages": messages, "max_chars": 200, "target_chars": 200},
    )
    assert result["messages"] == messages[:2]
    source = {"records": [{"ref": "g/1", "text": "x" * 1000}]}
    result = execute_component(
        contents,
        "python:middleware/long_tool_output.py:execute",
        {"result": source, "max_chars": 100},
    )
    assert result["result"] == source
    assert result["archive_requested"] is True
