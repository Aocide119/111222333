import json

from evog.cli import main


def test_cli_offline_demo_and_revision_inspection(tmp_path, capsys):
    workspace = str(tmp_path / "workspace")
    assert main(["--workspace", workspace, "demo"]) == 0
    demo = json.loads(capsys.readouterr().out)
    assert demo["mode"] == "offline-fixture"
    assert demo["activated"]["validation"] == "structural"
    assert main(["--workspace", workspace, "revisions"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_cli_missing_input_reports_error_without_traceback(tmp_path, capsys):
    assert main(["--workspace", str(tmp_path / "workspace"), "ingest", "missing.jsonl"]) == 1
    output = capsys.readouterr()
    assert "evog:" in output.err and "Traceback" not in output.err


def test_cli_canonical_import(tmp_path, capsys):
    assert (
        main(["--workspace", str(tmp_path / "workspace"), "ingest", "examples/messages.jsonl"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["imported"] == 3
