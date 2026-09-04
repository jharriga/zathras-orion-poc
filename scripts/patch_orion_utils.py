#!/usr/bin/env python3
"""Patch Orion Utils for Chronicler nested UUID fields in raw OpenSearch documents."""

from __future__ import annotations

import inspect
import textwrap
from typing import Any

import orion.utils as utils_mod
from orion.matcher import Matcher
from orion.utils import Utils


def record_uuid(self: Utils, record: dict[str, Any], matcher: Matcher | None = None) -> Any:
    """Read uuid_field from metadata rows or full Chronicler _source documents."""
    if self.uuid_field in record:
        return record[self.uuid_field]
    if "." in self.uuid_field:
        if matcher is not None:
            return matcher.dotDictFind(record, self.uuid_field)
        return Matcher._get_nested(record, self.uuid_field)
    return record.get(self.uuid_field)


def _rewrite_method_source(source: str) -> str:
    source = source.replace("run[self.uuid_field]", "self.record_uuid(run, match)")
    source = source.replace(
        'run["buildUrl"]',
        '(run.get("buildUrl") or run.get("build_url") or "N/A")',
    )
    return source


def _rebind_method(method_name: str) -> None:
    original = getattr(Utils, method_name)
    source = _rewrite_method_source(textwrap.dedent(inspect.getsource(original)))
    namespace = dict(vars(utils_mod))
    exec(compile(source, f"<patched {method_name}>", "exec"), namespace)  # noqa: S102
    setattr(Utils, method_name, namespace[method_name])


def apply() -> None:
    Utils.record_uuid = record_uuid
    for method_name in ("get_version", "get_build_urls"):
        _rebind_method(method_name)


apply()
