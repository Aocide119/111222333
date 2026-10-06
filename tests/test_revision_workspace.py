import pytest
from conftest import ScriptedProvider

from evog.agents.analysis import analyze
from evog.agents.demo import DemoProvider
from evog.agents.evolution import apply, propose
from evog.agents.interaction import interact
from evog.app import Application
from evog.core.errors import ContractError
from evog.core.models import Feedback
from evog.core.providers import ModelReply, ToolCall
from evog.harness.tools import Tools
from evog.harness.workspace import CandidateWorkspace


def report_for(store, settings):
    answer = interact(store, DemoProvider(), settings, "Latest release?", ["demo-team"])
    store.feedback(Feedback(run_id=answer.run_id, outcome="rejected", source="test"))
    return analyze(store, DemoProvider(), settings)


def registration(path, finding):
    return {
        "paths": [path],
        "interface": "Operation" if path.startswith("tools/") else "Policy",
        "finding_ids": [finding],
        "rationale": "The inspected trace motivates a candidate capability change",
        "expected_effect": "The next round runs this implementation",
        "regression_risk": "The tool may omit previous results",
        "validation": "Isolated entrypoint validation and paired execution",
    }


@pytest.mark.parametrize("interface", ["Operation", "Policy"])
def test_model_edits_actual_python_then_next_load_and_rollback_use_exact_versions(
    store, settings, interface
):
    report = report_for(store, settings)
    parent = store.harness().id
    path = "tools/implementations/list_files.py"
    code = 'def execute(arguments, context):\n    return {"files": [], "marker": "candidate-v2"}\n'
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="write", name="write_workspace", arguments={"path": path, "content": code}
                )
            ]
        ),
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="register",
                    name="register_change",
                    arguments={**registration(path, report.findings[0].id), "interface": interface},
                )
            ]
        ),
        ModelReply(tool_calls=[ToolCall(id="validate", name="validate_workspace", arguments={})]),
        ModelReply(content="Updated the list tool; the candidate entrypoint was validated."),
    )
    configured = settings.model_copy(update={"evolution_turns": 5})
    plan = propose(store, provider, configured, report)
    assert store.harness().id == parent
    assert plan.changes[0].content == code
    assert plan.changes[0].operation == "write"
    assert plan.changes[0].interface == interface
    apply(store, plan.id)
    manifest = store.artifact(f"manifest:{plan.id}", "change_manifest")
    assert manifest["changes"][0]["component"] == "Tools"
    assert manifest["changes"][0]["entry"] == interface
    result = Tools(store, store.harness(), ["demo-team"]).execute("list_files", {})
    assert result["marker"] == "candidate-v2"
    assert store.checkpoint(store.harness()).joinpath(path).read_text() == code
    store.rollback(parent)
    assert Tools(store, store.harness(), ["demo-team"]).execute("list_files", {})["files"]
    assert store.harness().contents[path] != code


def test_candidate_edits_require_registration_and_reediting_invalidates_it(store):
    workspace = CandidateWorkspace(store.workspace, store.harness())
    path = "skills/check-status/SKILL.md"
    workspace.execute("write_workspace", {"path": path, "content": "Check current status."})
    with pytest.raises(ContractError, match="registration"):
        workspace.draft("Change")
    workspace.execute("register_change", registration(path, "finding"))
    assert workspace.draft("Change").changes[0].path == path
    workspace.execute("edit_workspace", {"path": path, "old_text": "current", "new_text": "latest"})
    with pytest.raises(ContractError, match="registration"):
        workspace.draft("Change")


def test_workspace_edits_cannot_escape_or_replace_ambiguous_text(store):
    workspace = CandidateWorkspace(store.workspace, store.harness())
    with pytest.raises(ContractError):
        workspace.execute("write_workspace", {"path": "../api.env", "content": "value"})
    path = "skills/check-status/SKILL.md"
    workspace.execute("write_workspace", {"path": path, "content": "same same"})
    with pytest.raises(ContractError, match="exactly one"):
        workspace.execute("edit_workspace", {"path": path, "old_text": "same", "new_text": "new"})


def test_export_import_loads_complete_bundle_and_tamper_is_detected(store, tmp_path):
    with Application(store.workspace, provider=DemoProvider()) as app:
        exported = app.export_harness(tmp_path / "export")
        with Application(tmp_path / "other", provider=DemoProvider()) as other:
            assert other.import_harness(exported) == app.store.harness().id
        root = app.store.checkpoint(app.store.harness())
        altered = root / "prompt/group.md"
        altered.chmod(0o600)
        altered.write_text("Changed after checkpoint publication")
        with pytest.raises(ContractError):
            app.store.harness()


def test_stored_revision_payload_cannot_change_under_an_existing_id(store):
    import json

    original = store.harness()
    changed = dict(original.contents)
    changed["prompt/group.md"] += "\nChanged after storage."
    with store.connect() as db:
        db.execute("UPDATE revisions SET files=? WHERE id=?", (json.dumps(changed), original.id))
    with pytest.raises(ContractError, match="fingerprint"):
        store.harness(original.id)


def test_checkpoint_and_application_bundle_paths_reject_symlinks(store, tmp_path):
    harness = store.harness()
    outside = tmp_path / "outside"
    outside.mkdir()
    with Application(store.workspace, provider=DemoProvider()) as app:
        exported = app.export_harness(tmp_path / "export")
        alias = tmp_path / "alias"
        alias.symlink_to(exported, target_is_directory=True)
        with pytest.raises(ContractError):
            app.import_harness(alias)
        destination = tmp_path / "destination"
        destination.symlink_to(outside, target_is_directory=True)
        with pytest.raises(ContractError):
            app.export_harness(destination)
    other = tmp_path / "redirected"
    from evog.core.store import Store

    redirected = Store(other)
    (other / "checkpoints").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink"):
        redirected.checkpoint(harness)
    assert not list(outside.iterdir())
