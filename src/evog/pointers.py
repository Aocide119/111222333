"""Strict JSON Pointer reads for trace inspection and declarative checks."""

from __future__ import annotations

import re
from typing import Any

from evog.errors import ContractError


def resolve_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ContractError("field_path must be a JSON pointer")
    for token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ContractError("Invalid JSON pointer escape")
        part = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise ContractError("JSON pointer requires a nonnegative array index")
            # A valid index cannot have more digits than the length of the array.
            if len(part) > len(str(len(value))) or int(part) >= len(value):
                raise ContractError("JSON pointer array index is out of range")
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise ContractError("JSON pointer does not address a field")
    return value
