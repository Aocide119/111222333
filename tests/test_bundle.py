from __future__ import annotations

import copy
import json

import pytest
import yaml

from evog.core.errors import ContractError
from evog.core.io import dumps, fingerprint
from evog.harness.bundle import (
    MANIFEST_NAME,
    bundle_manifest,
    load_bundle,
    materialize_bundle,
    verify_bundle,
)
from evog.harness.schema import (
    Harness,
    Interventions,
    Operations,
    Representation,
    component_for,
    initial_files,
    prompt,
)


@pytest.fixture
def simple_bundle():
    """A complete tiny bundle independent of the installed plugin implementations."""
    return {
        "harness.toml": """format_version = 2
name = "test"
[components]
prompt = ["prompt/system.md"]
memory = "memory"
tools = "tools"
skills = "skills"
middleware = "middleware"
""",
        "prompt/system.md": "System instructions.",
        "memory/policy.md": "Notes require verified source references.\n",
        "memory/layout.toml": """version = 1
[categories]
people = "long_term_memory/people"
[round]
visibility = "frozen"
merge = "source_union"
working_scope = "question"
""",
        "memory/representation.json": dumps(Representation().model_dump()),
        "tools/operations.json": dumps(Operations().model_dump()),
        "middleware/interventions.json": dumps(Interventions().model_dump()),
        "tools/registry.yaml": "version: 1\ntools: []\n",
        "middleware/registry.yaml": "version: 1\nmiddleware: []\n",
    }


def with_tool(contents, *, source=None, schema=None):
    result = dict(contents)
    result["tools/implementations/search.py"] = source or (
        "def execute(arguments, context):\n    return {'matches': []}\n"
    )
    result["tools/registry.yaml"] = yaml.safe_dump(
        {
            "version": 1,
            "tools": [
                {
                    "name": "search",
                    "handler": "python:tools/implementations/search.py:execute",
                    "enabled": True,
                    "description_file": "tools/descriptions/search.yaml",
                    "readable_roots": ["memory_units"],
                    "writable_roots": [],
                }
            ],
        }
    )
    result["tools/descriptions/search.yaml"] = yaml.safe_dump(
        {
            "name": "search",
            "description": "Search authorized source records.",
            "parameters": schema
            or {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        }
    )
    return result


def test_packaged_baseline_has_five_components_and_exact_paper_prompt():
    harness = Harness(initial_files())
    assert harness.format_version == 2
    assert harness.system_prompt == prompt("group")
    assert harness.prompt_paths == ["prompt/system.md"]
    assert {path for path in harness.contents if path.startswith("prompt/")} == {"prompt/system.md"}
    assert {component_for(path) for path in harness.contents} == {
        "Harness",
        "Memory",
        "Tools",
        "Skills",
        "Prompt",
        "Middleware",
    }
    assert {tool["name"] for tool in harness.tool_registry} == {
        "list_files",
        "read_file",
        "grep_search",
        "write_file",
        "create_file",
    }
    assert harness.middleware_registry
    assert not any(path.endswith("/SKILL.md") for path in harness.contents)
    assert harness.config_path("operations") == "tools/operations.json"
    assert harness.id == fingerprint(dict(reversed(list(harness.contents.items()))))


def test_materialize_is_complete_verified_and_idempotent(simple_bundle, tmp_path):
    harness = Harness(simple_bundle)
    destination = tmp_path / "checkpoint"
    assert materialize_bundle(harness, destination) == destination
    assert verify_bundle(destination).contents == harness.contents
    assert json.loads((destination / MANIFEST_NAME).read_text()) == bundle_manifest(harness)
    assert materialize_bundle(harness, destination) == destination
    changed = dict(simple_bundle)
    changed["prompt/system.md"] += "A new instruction.\n"
    with pytest.raises(ContractError, match="overwrite"):
        materialize_bundle(Harness(changed), destination)
    assert verify_bundle(destination).id == harness.id
    assert not list(tmp_path.glob(".evog-bundle-*"))


def test_export_revalidates_mutated_harness_and_leaves_no_partial_destination(
    simple_bundle, tmp_path
):
    harness = Harness(simple_bundle)
    harness.contents["prompt/system.md"] += "A new instruction.\n"
    with pytest.raises(ContractError, match="changed after validation"):
        materialize_bundle(harness, tmp_path / "checkpoint")
    assert not (tmp_path / "checkpoint").exists()


@pytest.mark.parametrize("tamper", ["edit", "remove", "extra", "wrong-size", "bool-version"])
def test_manifest_detects_all_component_tampering(simple_bundle, tmp_path, tamper):
    checkpoint = materialize_bundle(Harness(simple_bundle), tmp_path / "checkpoint")
    if tamper == "edit":
        (checkpoint / "prompt/system.md").write_text("Edited instructions.\n")
    elif tamper == "remove":
        (checkpoint / "prompt/system.md").unlink()
    elif tamper == "extra":
        (checkpoint / "prompt/extra.md").write_text("Unlisted instructions.\n")
    else:
        path = checkpoint / MANIFEST_NAME
        manifest = json.loads(path.read_text())
        if tamper == "wrong-size":
            manifest["files"]["prompt/system.md"]["bytes"] += 1
        else:
            manifest["manifest_version"] = True
        path.write_text(json.dumps(manifest))
    with pytest.raises(ContractError):
        verify_bundle(checkpoint)


def test_candidate_can_load_without_seal_but_checkpoint_cannot(simple_bundle, tmp_path):
    checkpoint = materialize_bundle(Harness(simple_bundle), tmp_path / "candidate")
    (checkpoint / MANIFEST_NAME).unlink()
    assert load_bundle(checkpoint).contents == simple_bundle
    with pytest.raises(ContractError, match="missing"):
        verify_bundle(checkpoint)


@pytest.mark.parametrize("link", ["root", "file", "directory"])
def test_bundle_rejects_symlinks(simple_bundle, tmp_path, link):
    checkpoint = materialize_bundle(Harness(simple_bundle), tmp_path / "checkpoint")
    if link == "root":
        target = tmp_path / "link"
        target.symlink_to(checkpoint, target_is_directory=True)
    elif link == "file":
        original = checkpoint / "prompt/system.md"
        original.unlink()
        original.symlink_to(checkpoint / "memory/policy.md")
        target = checkpoint
    else:
        (checkpoint / "linked").symlink_to(checkpoint / "prompt", target_is_directory=True)
        target = checkpoint
    with pytest.raises(ContractError, match="symlink"):
        load_bundle(target)


@pytest.mark.parametrize(
    "path",
    [
        "../prompt.md",
        "/prompt/system.md",
        "prompt//system.md",
        "prompt/./system.md",
        "tools/implementations/../../escape.py",
        "tools/implementations\\escape.py",
        "skills/tool/scripts/run.py",
        "memory/template.py",
        "prompt/system.py",
        ".DS_Store",
    ],
)
def test_candidate_paths_are_bounded_and_component_specific(simple_bundle, path):
    contents = dict(simple_bundle)
    contents[path] = "Unapproved content.\n"
    with pytest.raises(ContractError):
        Harness(contents)


def test_unknown_root_file_is_rejected(simple_bundle):
    contents = dict(simple_bundle)
    contents["operations.json"] = "{}"
    with pytest.raises(ContractError, match="outside revision surfaces"):
        Harness(contents)


def test_plugin_validation_never_executes_code(simple_bundle, tmp_path):
    marker = tmp_path / "must-not-exist"
    contents = with_tool(
        simple_bundle,
        source=(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            "def execute(arguments, context):\n    return {}\n"
        ),
    )
    harness = Harness(contents)
    materialize_bundle(harness, tmp_path / "checkpoint")
    assert not marker.exists()
    assert harness.tool_registry[0]["name"] == "search"


@pytest.mark.parametrize(
    "source",
    [
        "def different(arguments, context):\n    return {}\n",
        "def execute(arguments):\n    return {}\n",
        "async def execute(arguments, context):\n    return {}\n",
        "def execute(arguments, context, *args):\n    return {}\n",
        "def execute(arguments, context, *, required):\n    return {}\n",
        "def execute(arguments, context):\n    return (\n",
    ],
)
def test_registered_python_signature_and_syntax_are_checked(simple_bundle, source):
    with pytest.raises(ContractError):
        Harness(with_tool(simple_bundle, source=source))


@pytest.mark.parametrize(
    "mutator",
    [
        lambda entries: entries.append(copy.deepcopy(entries[0])),
        lambda entries: entries[0].update(handler="python:../escape.py:execute"),
        lambda entries: entries[0].update(handler="python:middleware/search.py:execute"),
        lambda entries: entries[0].update(enabled="yes"),
        lambda entries: entries[0].update(writable_roots=["memory_units"]),
        lambda entries: entries[0].update(description_file="tools/descriptions/missing.yaml"),
    ],
)
def test_registry_requires_consistent_names_handlers_and_permissions(simple_bundle, mutator):
    contents = with_tool(simple_bundle)
    registry = yaml.safe_load(contents["tools/registry.yaml"])
    mutator(registry["tools"])
    contents["tools/registry.yaml"] = yaml.safe_dump(registry)
    with pytest.raises(ContractError):
        Harness(contents)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"type": "object", "properties": {}, "required": ["missing"]},
        {"type": "object", "properties": {"query": {"$ref": "https://example.test/schema"}}},
        {"type": "object", "properties": {"query": {"$ref": "#/$defs/missing"}}},
        {"type": "object", "properties": {"query": {"type": "unknown"}}},
    ],
)
def test_tool_schema_rejects_inconsistent_arguments_and_external_refs(simple_bundle, schema):
    with pytest.raises(ContractError):
        Harness(with_tool(simple_bundle, schema=schema))


def test_tool_schema_allows_argument_named_type_and_local_definitions(simple_bundle):
    schema = {
        "type": "object",
        "properties": {"type": {"$ref": "#/$defs/query"}},
        "$defs": {"query": {"type": "string"}},
        "required": ["type"],
    }
    Harness(with_tool(simple_bundle, schema=schema))


@pytest.mark.parametrize(
    "yaml_text",
    [
        "version: 1\nversion: 1\ntools: []\n",
        "version: 1\ntools: &entries []\nalias: *entries\n",
        "version: 1\ntools: !!python/object:builtins.dict {}\n",
    ],
)
def test_registry_yaml_is_safe_and_unambiguous(simple_bundle, yaml_text):
    contents = dict(simple_bundle)
    contents["tools/registry.yaml"] = yaml_text
    with pytest.raises(ContractError):
        Harness(contents)


def test_memory_round_isolation_cannot_be_disabled(simple_bundle):
    contents = dict(simple_bundle)
    contents["memory/layout.toml"] = contents["memory/layout.toml"].replace(
        'visibility = "frozen"', 'visibility = "shared"'
    )
    with pytest.raises(ContractError, match="isolation"):
        Harness(contents)


def test_component_paths_identify_editable_component_boundaries():
    assert component_for("memory/policy.md") == "Memory"
    assert component_for("tools/implementations/search/rank.py") == "Tools"
    assert component_for("skills/search/SKILL.md") == "Skills"
    assert component_for("prompt/system.md") == "Prompt"
    assert component_for("middleware/context_compaction.py") == "Middleware"


@pytest.mark.parametrize("content", ["\x00binary", "\ud800invalid", "x" * 131073])
def test_v2_rejects_nontext_and_oversize_files(simple_bundle, content):
    contents = dict(simple_bundle)
    contents["prompt/system.md"] = content
    with pytest.raises(ContractError):
        Harness(contents)


def test_v2_json_configuration_rejects_duplicate_keys(simple_bundle):
    contents = dict(simple_bundle)
    contents["tools/operations.json"] = '{"search_limit":8,"search_limit":9}'
    with pytest.raises(ContractError, match="Duplicate JSON"):
        Harness(contents)


def test_loader_rejects_unknown_and_binary_files(simple_bundle, tmp_path):
    checkpoint = materialize_bundle(Harness(simple_bundle), tmp_path / "candidate")
    extra = checkpoint / "extra.txt"
    extra.write_text("Unexpected file")
    with pytest.raises(ContractError, match="outside revision"):
        load_bundle(checkpoint)
    extra.unlink()
    (checkpoint / "prompt/system.md").write_bytes(b"\xff\xfe")
    with pytest.raises(ContractError, match="text file"):
        load_bundle(checkpoint)


def test_memory_categories_cannot_alias_or_cross_working_scope(simple_bundle):
    for extra in ('other = "long_term_memory/people"', 'other = "working_memory/people"'):
        contents = dict(simple_bundle)
        contents["memory/layout.toml"] = contents["memory/layout.toml"].replace(
            "[round]", extra + "\n[round]"
        )
        with pytest.raises(ContractError, match="unique paths"):
            Harness(contents)


@pytest.mark.parametrize(
    "reference",
    [
        {"$ref": "https://example.test/schema.json"},
        {"$dynamicRef": "https://example.test/schema.json#anchor"},
        {"$ref": "other.json#/$defs/query"},
        {"$ref": "#anchor"},
        {"$id": "https://example.test/schema.json", "type": "string"},
    ],
)
def test_schema_resolution_cannot_fetch_external_resources(simple_bundle, reference):
    schema = {"type": "object", "properties": {"query": reference}}
    with pytest.raises(ContractError):
        Harness(with_tool(simple_bundle, schema=schema))


def test_schema_supports_nullable_composition_and_local_json_pointers(simple_bundle):
    schema = {
        "type": "object",
        "properties": {
            "query": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "alternate": {"$ref": "#/$defs/a~1b"},
        },
        "$defs": {"a/b": {"type": ["string", "null"]}},
    }
    Harness(with_tool(simple_bundle, schema=schema))


def test_schema_meta_validation_rejects_invalid_numeric_keywords(simple_bundle):
    schema = {"type": "object", "properties": {"count": {"type": "integer", "minimum": "zero"}}}
    with pytest.raises(ContractError, match="Invalid tool JSON Schema"):
        Harness(with_tool(simple_bundle, schema=schema))


@pytest.mark.parametrize("enabled", [True, False])
def test_registered_tools_cannot_collide_with_declarative_query_aliases(simple_bundle, enabled):
    contents = with_tool(simple_bundle)
    registry = yaml.safe_load(contents["tools/registry.yaml"])
    registry["tools"][0].update(name="query_sender", enabled=enabled)
    contents["tools/registry.yaml"] = yaml.safe_dump(registry)
    description = yaml.safe_load(contents["tools/descriptions/search.yaml"])
    description["name"] = "query_sender"
    contents["tools/descriptions/search.yaml"] = yaml.safe_dump(description)
    contents["tools/operations.json"] = dumps(
        {
            "query_tools": [
                {
                    "name": "query_sender",
                    "description": "Find records by sender.",
                }
            ]
        }
    )
    with pytest.raises(ContractError, match="unique names"):
        Harness(contents)


def test_component_contract_readmes_are_allowed_but_arbitrary_docs_are_not(simple_bundle):
    contents = dict(simple_bundle)
    contents["tools/README.md"] = "# Tool capability contracts\n"
    contents["middleware/README.md"] = "# Middleware payload contracts\n"
    Harness(contents)
    assert component_for("tools/README.md") == "Tools"
    assert component_for("middleware/README.md") == "Middleware"
    contents["tools/arbitrary.md"] = "Unregistered documentation\n"
    with pytest.raises(ContractError):
        Harness(contents)
