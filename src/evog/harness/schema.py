"""Versioned five-component harness contracts, validated without running plugin code."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from importlib.resources import files
from typing import Any, Literal
from urllib.parse import unquote

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, field_validator, model_validator

from evog.core.errors import ContractError
from evog.core.io import fingerprint
from evog.core.models import Record


def prompt(name: str) -> str:
    """Load a fixed control-plane prompt or the complete base group contract.

    The group prompt is part of the versioned Harness component bundle.  Keeping
    this loader pointed at that bundle prevents a second, silently divergent copy
    under ``agents/prompts``.  The remaining prompts are fixed orchestration prompts
    for reflection, analysis, synthesis, and revision; they are intentionally
    outside the evolvable five-component bundle.
    """
    if name == "group":
        root = files("evog").joinpath("harness/base/prompt")
        return "\n\n".join(
            root.joinpath(part).read_text(encoding="utf-8") for part in ("system.md", "group.md")
        )
    return files("evog").joinpath(f"agents/prompts/{name}.md").read_text(encoding="utf-8")


class Representation(Record):
    include_metadata: bool = False
    timestamp_view: Literal["original", "utc", "both"] = "original"
    include_reply_refs: bool = False


SearchField = Literal[
    "text", "sender", "timestamp", "group_id", "message_id", "ref", "reply_to", "metadata"
]


class QueryTool(Record):
    """A declarative search tool that compiles to the fixed query runtime.

    Operation revisions may add these entries, but they cannot provide Python,
    shell, imports, or arbitrary callbacks.  This keeps the operation surface
    extensible while preserving the runtime safety boundary.
    """

    name: str = Field(min_length=7, max_length=64, pattern=r"^query_[a-z][a-z0-9_]{0,47}$")
    description: str = Field(min_length=1, max_length=400)
    target: Literal["memory_units", "memory_store"] = "memory_units"
    resource_path: str = Field(default="", max_length=512)
    search_mode: Literal["any", "all"] = "any"
    search_limit: int = Field(default=8, ge=1, le=30)
    context_window: int = Field(default=0, ge=0, le=5)
    search_fields: list[SearchField] = Field(
        default_factory=lambda: [
            "text",
            "sender",
            "timestamp",
            "group_id",
            "message_id",
            "ref",
            "reply_to",
            "metadata",
        ],
        min_length=1,
        max_length=8,
    )
    match_mode: Literal["literal", "word"] = "literal"
    excerpt_chars: int = Field(default=1400, ge=256, le=4000)

    @field_validator("resource_path")
    @classmethod
    def safe_resource_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or (value and any(part in {"", ".", ".."} for part in value.split("/")))
        ):
            raise ValueError("Query tool resource_path must be a safe relative path")
        return value


class Operations(Record):
    search_mode: str = Field(default="any", pattern=r"^(any|all)$")
    search_limit: int = Field(default=8, ge=1, le=30)
    context_window: int = Field(default=0, ge=0, le=5)
    search_fields: list[SearchField] = Field(
        default_factory=lambda: [
            "text",
            "sender",
            "timestamp",
            "group_id",
            "message_id",
            "ref",
            "reply_to",
            "metadata",
        ],
        min_length=1,
        max_length=8,
    )
    match_mode: Literal["literal", "word"] = "literal"
    read_limit: int = Field(default=20, ge=1, le=50)
    excerpt_chars: int = Field(default=1400, ge=256, le=4000)
    query_tools: list[QueryTool] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_query_tool_names(self):
        names = [tool.name for tool in self.query_tools]
        if len(names) != len(set(names)):
            raise ValueError("Query tool names must be unique")
        builtins = {"list_files", "read_file", "grep_search", "write_file", "create_file"}
        if builtins.intersection(names):
            raise ValueError("Query tool names cannot replace built-in tools")
        return self

    def for_query_tool(self, tool: QueryTool) -> Operations:
        """Compile one declarative query tool into fixed search settings."""
        return self.model_copy(
            update={
                "search_mode": tool.search_mode,
                "search_limit": tool.search_limit,
                "context_window": tool.context_window,
                "search_fields": tool.search_fields,
                "match_mode": tool.match_mode,
                "excerpt_chars": tool.excerpt_chars,
            }
        )


class Interventions(Record):
    require_citations_for_partial: Literal[True] = True
    max_citations: int = Field(default=32, ge=1, le=64)
    max_answer_chars: int = Field(default=8000, ge=256, le=12000)
    min_complete_citations: int = Field(default=1, ge=1, le=16)
    partial_confidence_cap: float = Field(default=1, ge=0, le=1)

    @model_validator(mode="after")
    def compatible_citation_limits(self):
        if self.min_complete_citations > self.max_citations:
            raise ValueError("Minimum complete citations exceed the maximum citation count")
        return self


CONFIG_PATHS = {
    "representation": "memory/representation.json",
    "operations": "tools/operations.json",
    "interventions": "middleware/interventions.json",
}
REQUIRED_FILES = {
    "harness.toml",
    "prompt/system.md",
    "prompt/group.md",
    "memory/policy.md",
    "memory/layout.toml",
    "tools/registry.yaml",
    "middleware/registry.yaml",
    *CONFIG_PATHS.values(),
}
MAX_FILES = 128
MAX_FILE_BYTES = 131072
MAX_BUNDLE_BYTES = 1048576
NAME = r"[a-z][a-z0-9_]{0,63}"
SEGMENT = r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,95}"
HOOKS = {"before_model", "after_model", "before_tool", "after_tool", "before_finalize"}
ROOTS = {"memory_units", "memory_store", "workspace", "skills"}


def validate_relative_path(path: str) -> str:
    """Reject aliases before filesystem access or hashing."""
    if not isinstance(path, str) or len(path) > 512 or "\\" in path:
        raise ContractError("Harness path must be a bounded POSIX relative path")
    parts = path.split("/")
    if any(not re.fullmatch(SEGMENT, part) or part in {".", ".."} for part in parts):
        raise ContractError(f"Invalid harness path: {path}")
    return path


def component_for(path: str) -> str:
    validate_relative_path(path)
    if path == "harness.toml":
        return "Harness"
    if re.fullmatch(r"prompt/[a-z][a-z0-9_-]{0,63}\.md", path):
        return "Prompt"
    if path in {"memory/representation.json", "memory/layout.toml"}:
        return "Memory"
    if re.fullmatch(
        r"memory/(?:rules/|templates/)?(?:" + SEGMENT + r"/)*" + SEGMENT + r"\.md", path
    ):
        return "Memory"
    if path in {"tools/operations.json", "tools/registry.yaml", "tools/README.md"}:
        return "Tools"
    if re.fullmatch(r"tools/descriptions/" + NAME + r"\.yaml", path):
        return "Tools"
    if re.fullmatch(r"tools/implementations/(?:" + SEGMENT + r"/)*" + SEGMENT + r"\.py", path):
        return "Tools"
    if path in {
        "middleware/interventions.json",
        "middleware/registry.yaml",
        "middleware/README.md",
    }:
        return "Middleware"
    if re.fullmatch(r"middleware/(?:" + SEGMENT + r"/)*" + SEGMENT + r"\.py", path):
        return "Middleware"
    if path == "skills/README.md":
        return "Skills"
    if re.fullmatch(r"skills/[a-z0-9][a-z0-9_-]{0,63}/SKILL\.md", path):
        return "Skills"
    if re.fullmatch(
        r"skills/[a-z0-9][a-z0-9_-]{0,63}/references/(?:"
        + SEGMENT
        + r"/)*"
        + SEGMENT
        + r"\.(?:md|txt|json|toml|yaml)",
        path,
    ):
        return "Skills"
    raise ContractError(f"File is outside revision surfaces: {path}")


def initial_files() -> dict[str, str]:
    """Read the installed baseline bundle; behavior is authored in its component files."""
    root = files("evog").joinpath("harness/base")
    contents: dict[str, str] = {}

    def visit(directory, prefix: str = "") -> None:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = prefix + child.name
            if child.is_dir():
                visit(child, relative + "/")
            elif child.is_file():
                # Build tools and Python may leave caches in an editable installation.
                if child.name.startswith(".") or "__pycache__" in relative.split("/"):
                    continue
                contents[relative] = child.read_text(encoding="utf-8")

    visit(root)
    return contents


def _mapping(value: Any, location: str) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ContractError(f"Expected a string-keyed mapping in {location}")
    return value


def _keys(value: dict, required: set[str], optional: set[str], location: str) -> None:
    if not required.issubset(value) or set(value) - required - optional:
        raise ContractError(f"Unexpected or missing fields in {location}")


class _UniqueSafeLoader(yaml.SafeLoader):
    """Safe YAML with duplicate keys rejected rather than silently overwritten."""


def _yaml_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ContractError("YAML mapping keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


def _yaml(text: str, location: str) -> dict:
    try:
        # Aliases can produce cycles or amplify small inputs; components need neither.
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(text)
        ):
            raise ContractError(f"YAML aliases are not allowed in {location}")
        return _mapping(yaml.load(text, Loader=_UniqueSafeLoader), location)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ContractError(f"Invalid YAML in {location}") from exc


def _toml(text: str, location: str) -> dict:
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError) as exc:
        raise ContractError(f"Invalid TOML in {location}") from exc


def _schema(schema: Any, location: str) -> None:
    """Check JSON Schema locally, with bounded structure and no remote resolution."""
    _mapping(schema, location)
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ContractError(f"Tool parameters must be an object schema in {location}")
    try:
        json.dumps(schema, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError(f"Tool schema must contain JSON values in {location}") from exc
    pending = [(schema, 0)]
    nodes = 0
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if depth > 24 or nodes > 2000:
            raise ContractError(f"Tool schema exceeds nesting budget in {location}")
        if isinstance(node, dict):
            if "required" in node and ("properties" in node or node.get("type") == "object"):
                required = node["required"]
                properties = node.get("properties", {})
                if (
                    isinstance(required, list)
                    and isinstance(properties, dict)
                    and any(
                        not isinstance(name, str) or name not in properties for name in required
                    )
                ):
                    raise ContractError(
                        f"Required tool parameters need property definitions in {location}"
                    )
            # Resource identifiers and anchors can change a fragment's resolution
            # scope. This bundle format uses one schema document and JSON pointers.
            if {"$id", "$anchor", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor"} & set(
                node
            ):
                raise ContractError(
                    f"Schema resource identifiers and anchors are unsupported in {location}"
                )
            for key in ("$ref", "$dynamicRef"):
                if key not in node:
                    continue
                ref = node[key]
                if not isinstance(ref, str) or not ref.startswith("#/"):
                    raise ContractError(
                        f"Only local JSON-pointer schema references are allowed in {location}"
                    )
                resolved: Any = schema
                try:
                    for token in unquote(ref[2:]).split("/"):
                        if re.search(r"~(?![01])", token):
                            raise ValueError("invalid JSON pointer")
                        token = token.replace("~1", "/").replace("~0", "~")
                        if isinstance(resolved, list):
                            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                                raise ValueError("invalid list index")
                            resolved = resolved[int(token)]
                        else:
                            resolved = resolved[token]
                    if not isinstance(resolved, (dict, bool)):
                        raise ValueError("reference does not identify a schema")
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ContractError(f"Unresolved schema reference in {location}") from exc
            pending.extend((value, depth + 1) for value in node.values())
        elif isinstance(node, list):
            pending.extend((value, depth + 1) for value in node)
    try:
        Draft202012Validator.check_schema(schema)
    except (SchemaError, RecursionError) as exc:
        raise ContractError(f"Invalid tool JSON Schema in {location}") from exc


def _handler(handler: str, contents: dict[str, str], component: str) -> tuple[str, str]:
    if not isinstance(handler, str):
        raise ContractError("Handler must be a Python file entrypoint")
    match = re.fullmatch(r"python:([^:]+):([a-zA-Z_][a-zA-Z0-9_]*)", handler)
    if not match:
        raise ContractError("Handler must use python:<relative-file>:<function>")
    path, entrypoint = match.groups()
    if component_for(path) != component or not path.endswith(".py") or path not in contents:
        raise ContractError(f"Handler is missing or belongs to another component: {path}")
    if component == "Tools" and not path.startswith("tools/implementations/"):
        raise ContractError("Tools must load implementations inside tools/implementations")
    try:
        module = ast.parse(contents[path], filename=path)
    except (SyntaxError, ValueError) as exc:
        raise ContractError(f"Invalid Python syntax in {path}") from exc
    candidates = [
        n
        for n in module.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == entrypoint
    ]
    if len(candidates) != 1 or isinstance(candidates[0], ast.AsyncFunctionDef):
        raise ContractError(f"Handler needs one synchronous function {entrypoint} in {path}")
    args = candidates[0].args
    if len(args.posonlyargs) + len(args.args) != 2 or args.vararg or args.kwarg:
        raise ContractError(f"Handler must accept exactly two arguments in {path}")
    if any(default is None for default in args.kw_defaults):
        raise ContractError(f"Handler cannot require keyword-only arguments in {path}")
    return path, entrypoint


def _registry(contents: dict[str, str], component: str) -> list[dict]:
    key = "tools" if component == "Tools" else "middleware"
    location = f"{key}/registry.yaml"
    document = _yaml(contents[location], location)
    _keys(document, {"version", key}, set(), location)
    if type(document["version"]) is not int or document["version"] != 1:
        raise ContractError(f"Unsupported registry version in {location}")
    entries = document[key]
    if not isinstance(entries, list) or len(entries) > 32:
        raise ContractError(f"Registry entries exceed the limit in {location}")
    names, descriptions = set(), set()
    for raw in entries:
        entry = _mapping(raw, location)
        required = {"name", "handler", "enabled"}
        required |= (
            {"description_file", "readable_roots", "writable_roots"}
            if component == "Tools"
            else {"hook", "config"}
        )
        _keys(entry, required, set(), location)
        name = entry["name"]
        if not isinstance(name, str) or not re.fullmatch(NAME, name) or name in names:
            raise ContractError(f"Invalid or duplicate component registration in {location}")
        names.add(name)
        if type(entry["enabled"]) is not bool:
            raise ContractError(f"enabled must be boolean in {location}")
        _handler(entry["handler"], contents, component)
        if component == "Tools":
            for field in ("readable_roots", "writable_roots"):
                roots = entry[field]
                allowed = ROOTS if field == "readable_roots" else {"memory_store"}
                if (
                    not isinstance(roots, list)
                    or any(not isinstance(r, str) for r in roots)
                    or len(roots) != len(set(roots))
                    or not set(roots).issubset(allowed)
                ):
                    raise ContractError(f"Invalid {field} in {location}")
            path = entry["description_file"]
            if (
                not isinstance(path, str)
                or component_for(path) != "Tools"
                or not path.startswith("tools/descriptions/")
                or path not in contents
                or path in descriptions
            ):
                raise ContractError(f"Missing or reused tool description in {location}")
            descriptions.add(path)
            description = _yaml(contents[path], path)
            _keys(description, {"name", "description", "parameters"}, set(), path)
            if description["name"] != name or not isinstance(description["description"], str):
                raise ContractError(f"Tool name/description disagree in {path}")
            if not description["description"].strip() or len(description["description"]) > 16000:
                raise ContractError(f"Tool description is empty or too large in {path}")
            _schema(description["parameters"], path)
        else:
            if not isinstance(entry["hook"], str) or entry["hook"] not in HOOKS:
                raise ContractError(f"Unsupported middleware hook in {location}")
            config = _mapping(entry["config"], location)
            try:
                encoded = json.dumps(config, allow_nan=False)
            except (ValueError, TypeError, RecursionError) as exc:
                raise ContractError(
                    f"Middleware config must be JSON-compatible in {location}"
                ) from exc
            if len(encoded) > 16000:
                raise ContractError(f"Middleware config exceeds size budget in {location}")
    return entries


class Harness:
    """An immutable-by-convention component snapshot; no handler is imported here."""

    def __init__(self, contents: dict[str, str]):
        if not isinstance(contents, dict):
            raise ContractError("Harness contents must be a file-content mapping")
        self.format_version = 2
        if not REQUIRED_FILES.issubset(contents):
            raise ContractError("A complete component bundle is required")
        max_files, max_total, max_file = MAX_FILES, MAX_BUNDLE_BYTES, MAX_FILE_BYTES
        if len(contents) > max_files:
            raise ContractError("Harness exceeds the bounded file budget")
        total = 0
        for path, text in contents.items():
            component_for(path)
            if not isinstance(text, str) or not text.strip() or "\x00" in text:
                raise ContractError(f"Empty or nontext harness file: {path}")
            try:
                encoded = text.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ContractError(f"Invalid UTF-8 text in harness file: {path}") from exc
            size = len(encoded)
            total += size
            if size > max_file:
                raise ContractError(f"Oversized harness file: {path}")
            if path.endswith(".py"):
                try:
                    ast.parse(text, filename=path)
                except (SyntaxError, ValueError) as exc:
                    raise ContractError(f"Invalid Python syntax in {path}") from exc
        if total > max_total:
            raise ContractError("Harness exceeds the bounded byte budget")
        self.contents = dict(contents)
        self.prompt_paths: list[str] = []
        self.tool_registry: list[dict] = []
        self.middleware_registry: list[dict] = []
        self.memory_layout: dict = {}
        manifest = _toml(contents["harness.toml"], "harness.toml")
        _keys(manifest, {"format_version", "name", "components"}, set(), "harness.toml")
        if type(manifest["format_version"]) is not int or manifest["format_version"] != 2:
            raise ContractError("Unsupported harness format_version")
        if not isinstance(manifest["name"], str) or not re.fullmatch(NAME, manifest["name"]):
            raise ContractError("Invalid harness name")
        components = _mapping(manifest["components"], "harness.toml")
        _keys(
            components,
            {"prompt", "memory", "tools", "skills", "middleware"},
            set(),
            "harness.toml",
        )
        if any(components[key] != key for key in ("memory", "tools", "skills", "middleware")):
            raise ContractError("Component roots must use their canonical directory names")
        paths = components["prompt"]
        if (
            not isinstance(paths, list)
            or not paths
            or len(paths) > 8
            or any(
                not isinstance(p, str) or p not in contents or component_for(p) != "Prompt"
                for p in paths
            )
            or len(paths) != len(set(paths))
        ):
            raise ContractError("Prompt entries must name unique bundled prompt files")
        self.prompt_paths = paths
        self.tool_registry = _registry(contents, "Tools")
        self.middleware_registry = _registry(contents, "Middleware")
        layout = _toml(contents["memory/layout.toml"], "memory/layout.toml")
        _keys(layout, {"version", "categories", "round"}, set(), "memory/layout.toml")
        if type(layout["version"]) is not int or layout["version"] != 1:
            raise ContractError("Unsupported memory layout version")
        categories = _mapping(layout["categories"], "memory/layout.toml")
        if not categories or len(categories) > 16:
            raise ContractError("Memory needs a bounded set of categories")
        category_paths = set()
        for name, path in categories.items():
            if not re.fullmatch(NAME, name):
                raise ContractError("Invalid memory category name")
            validate_relative_path(path)
            if not path.startswith("long_term_memory/") or path in category_paths:
                raise ContractError("Memory categories need unique paths under long_term_memory")
            category_paths.add(path)
        if layout["round"] != {
            "visibility": "frozen",
            "merge": "source_union",
            "working_scope": "question",
        }:
            raise ContractError("Memory round isolation and merge contracts cannot be weakened")
        self.memory_layout = layout

        def read_config(name: str):
            path = self.config_path(name)

            def unique_pairs(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ContractError(f"Duplicate JSON fields in {path}")
                    result[key] = value
                return result

            try:
                return json.loads(
                    contents[path],
                    object_pairs_hook=unique_pairs,
                )
            except (json.JSONDecodeError, RecursionError) as exc:
                raise ContractError(f"Invalid JSON in {path}") from exc

        self.representation = Representation.model_validate(read_config("representation"))
        self.operations = Operations.model_validate(read_config("operations"))
        if {entry["name"] for entry in self.tool_registry} & {
            tool.name for tool in self.operations.query_tools
        }:
            raise ContractError(
                "Registered tools and declarative query aliases must have unique names"
            )
        self.interventions = Interventions.model_validate(read_config("interventions"))
        self.id = fingerprint(contents)

    def config_path(self, name: str) -> str:
        if name not in CONFIG_PATHS:
            raise ContractError(f"Unknown harness configuration: {name}")
        return CONFIG_PATHS[name]

    @staticmethod
    def component_for(path: str) -> str:
        return component_for(path)

    @property
    def memory_policy(self) -> str:
        return self.contents.get("memory/policy.md", "")

    @property
    def system_prompt(self) -> str:
        return "\n\n".join(self.contents[path] for path in self.prompt_paths)
