import copy
import keyword
from typing import Any


READONLY_POST_PATH_SUFFIXES = (
    "/search",
    "/search/plan",
    "/_search",
)


def patch_openapi_spec_for_keywords(spec: dict[str, Any]) -> dict[str, Any]:
    """
    Return a deep copy of an OpenAPI spec with Python keyword property names patched.

    FastMCP maps OpenAPI schemas to Python call signatures. Some APIs expose fields such
    as `from` or `in`, which conflict with Python keywords. The convention is to add a
    trailing underscore.
    """
    patched_spec = copy.deepcopy(spec)
    return _patch_keywords_in_place(patched_spec)


def filter_openapi_spec_for_readonly(spec: dict[str, Any]) -> dict[str, Any]:
    """
    Return a deep copy of an OpenAPI spec with write operations removed.

    Quickwit search accepts POST, so readonly mode keeps POST search endpoints while
    removing ingest, index management, source mutation, delete tasks, clear, and other
    mutating operations.
    """
    filtered_spec = copy.deepcopy(spec)
    paths = filtered_spec.get("paths", {})
    if not isinstance(paths, dict):
        return filtered_spec

    for path, operations in list(paths.items()):
        if not isinstance(operations, dict):
            continue

        for method in list(operations.keys()):
            method_lower = method.lower()
            if method_lower == "get":
                continue
            if method_lower == "post" and _is_readonly_post_path(path):
                continue
            if method_lower in {"parameters", "summary", "description"}:
                continue
            operations.pop(method, None)

        operation_methods = {key.lower() for key in operations if isinstance(key, str)}
        if not operation_methods.intersection({"get", "post", "put", "patch", "delete", "head", "options", "trace"}):
            paths.pop(path, None)

    return filtered_spec


def _patch_keywords_in_place(value: Any) -> Any:
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            for property_name in list(properties.keys()):
                if keyword.iskeyword(property_name):
                    properties[f"{property_name}_"] = properties.pop(property_name)

        for key, child in value.items():
            value[key] = _patch_keywords_in_place(child)

    elif isinstance(value, list):
        for index, child in enumerate(value):
            value[index] = _patch_keywords_in_place(child)

    return value


def _is_readonly_post_path(path: str) -> bool:
    normalized_path = path.rstrip("/").lower()
    return normalized_path.endswith(READONLY_POST_PATH_SUFFIXES)
