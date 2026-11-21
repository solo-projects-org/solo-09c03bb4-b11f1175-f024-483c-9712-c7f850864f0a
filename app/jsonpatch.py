"""A strict, dependency-free JSON Patch (RFC 6902) implementation.

The archive uses patches as a correction trail, so applying one must either
succeed completely or leave the document untouched.  Unknown operation
members are ignored as RFC 6902 permits, but malformed known members and
invalid JSON Pointers are errors.
"""

from __future__ import annotations

import copy
from typing import Any

from .jcs import CanonicalizationError, canonicalize

_MISSING = object()


class PatchError(ValueError):
    """Raised when a patch document is malformed or cannot be applied."""


def apply_patch(document: Any, patch: Any) -> Any:
    """Apply *patch* to *document* and return the patched value.

    *document* is never modified.  On any error :class:`PatchError` is raised.
    """

    if not isinstance(patch, list):
        raise PatchError("patch document must be a JSON array")

    result = copy.deepcopy(document)
    for index, operation in enumerate(patch):
        try:
            result = _apply_operation(result, operation)
        except PatchError as exc:
            raise PatchError(f"operation {index}: {exc}") from exc
    return result


def _apply_operation(document: Any, operation: Any) -> Any:
    if not isinstance(operation, dict):
        raise PatchError("operation must be a JSON object")
    op = operation.get("op")
    path = operation.get("path")
    if not isinstance(op, str):
        raise PatchError("'op' must be a string")
    if not isinstance(path, str):
        raise PatchError("'path' must be a string")

    if op == "add":
        if "value" not in operation:
            raise PatchError("'add' requires 'value'")
        token, parent = _resolve_parent(document, path, allow_root=True)
        _add(parent, token, operation["value"])
    elif op == "remove":
        token, parent = _resolve_parent(document, path)
        _remove(parent, token)
    elif op == "replace":
        if "value" not in operation:
            raise PatchError("'replace' requires 'value'")
        token, parent = _resolve_parent(document, path)
        _replace(parent, token, operation["value"])
    elif op == "move":
        from_path = _require_from(operation)
        value = _get(document, from_path)
        if from_path != path and path.startswith(from_path + "/"):
            raise PatchError("cannot move a value into one of its own children")
        token, parent = _resolve_parent(document, from_path)
        _remove(parent, token)
        token, parent = _resolve_parent(document, path)
        _add(parent, token, value)
    elif op == "copy":
        from_path = _require_from(operation)
        value = copy.deepcopy(_get(document, from_path))
        token, parent = _resolve_parent(document, path, allow_root=True)
        _add(parent, token, value)
    elif op == "test":
        if "value" not in operation:
            raise PatchError("'test' requires 'value'")
        actual = _get(document, path)
        try:
            equal = canonicalize(actual) == canonicalize(operation["value"])
        except CanonicalizationError as exc:
            raise PatchError(f"cannot compare values: {exc}") from exc
        if not equal:
            raise PatchError("'test' operation failed")
    else:
        raise PatchError(f"unsupported operation {op!r}")
    return document


def _require_from(operation: dict[str, Any]) -> str:
    value = operation.get("from")
    if not isinstance(value, str):
        raise PatchError(f"'{operation.get('op')}' requires string member 'from'")
    return value


def parse_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise PatchError(f"invalid JSON Pointer {pointer!r}")
    tokens = []
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        # Re-scan for "~" sequences that were not valid escapes: replacing
        # "~0" first would hide a following "~", so validate after decoding
        # by re-encoding and comparing.
        if _encode_token(token) != raw:
            raise PatchError(f"invalid JSON Pointer escape in {pointer!r}")
        tokens.append(token)
    return tokens


def _encode_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _resolve_parent(document: Any, pointer: str, allow_root: bool = False):
    tokens = parse_pointer(pointer)
    if not tokens:
        if allow_root:
            return _MISSING, document
        raise PatchError("the document root cannot be addressed by this operation")
    parent = document
    for token in tokens[:-1]:
        parent = _child(parent, token, for_add=False)
    return tokens[-1], parent


def _child(container: Any, token: str, *, for_add: bool) -> Any:
    if isinstance(container, dict):
        if token not in container:
            raise PatchError(f"path segment {token!r} does not exist")
        return container[token]
    if isinstance(container, list):
        index = _index(container, token, for_add=for_add)
        return container[index]
    raise PatchError("cannot traverse into a scalar value")


def _index(container: list[Any], token: str, *, for_add: bool) -> int:
    if token == "-":
        if for_add:
            return len(container)
        raise PatchError("'-' is only valid as an array index for 'add'")
    if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
        raise PatchError(f"invalid array index {token!r}")
    index = int(token)
    upper = len(container) if for_add else len(container) - 1
    if index > upper:
        raise PatchError(f"array index {index} is out of range")
    return index


def _add(parent: Any, token: str, value: Any) -> None:
    if isinstance(parent, dict):
        if token == _MISSING:
            raise PatchError("cannot add the document root")
        parent[token] = copy.deepcopy(value)
    elif isinstance(parent, list):
        parent.insert(_index(parent, token, for_add=True), copy.deepcopy(value))
    else:
        raise PatchError("cannot add a value to a scalar")


def _remove(parent: Any, token: str) -> None:
    if isinstance(parent, dict):
        if token not in parent:
            raise PatchError(f"path segment {token!r} does not exist")
        del parent[token]
    elif isinstance(parent, list):
        index = _index(parent, token, for_add=False)
        if index < 0:
            raise PatchError("array index is out of range")
        del parent[index]
    else:
        raise PatchError("cannot remove a value from a scalar")


def _replace(parent: Any, token: str, value: Any) -> None:
    if isinstance(parent, dict):
        if token not in parent:
            raise PatchError(f"path segment {token!r} does not exist")
        parent[token] = copy.deepcopy(value)
    elif isinstance(parent, list):
        index = _index(parent, token, for_add=False)
        parent[index] = copy.deepcopy(value)
    else:
        raise PatchError("cannot replace a value in a scalar")


def _get(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    tokens = parse_pointer(pointer)
    current = document
    for token in tokens:
        current = _child(current, token, for_add=False)
    return current
