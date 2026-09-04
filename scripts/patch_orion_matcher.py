#!/usr/bin/env python3
"""Patch Orion Matcher for Chronicler nested UUID fields (e.g. metadata.document_id)."""

from __future__ import annotations

import inspect
import textwrap
from typing import Any

import orion.matcher as matcher_mod
from orion.matcher import Matcher


def uuid_terms_field(self: Matcher) -> str:
    """OpenSearch field for terms queries on the run identifier."""
    if "." in self.uuid_field:
        return self.uuid_field
    return f"{self.uuid_field}.keyword"


def uuid_from_source(self: Matcher, source: dict[str, Any]) -> Any:
    """Read the configured uuid_field from a document _source."""
    if "." in self.uuid_field:
        return self.dotDictFind(source, self.uuid_field)
    return source.get(self.uuid_field)


def _rewrite_method_source(source: str) -> str:
    source = source.replace(
        'hit.to_dict()["_source"][self.uuid_field]',
        'self.uuid_from_source(hit.to_dict()["_source"])',
    )
    source = source.replace('self.uuid_field+".keyword"', "self.uuid_terms_field()")
    source = source.replace('self.uuid_field + ".keyword"', "self.uuid_terms_field()")
    source = source.replace(
        "doc.get(self.uuid_field)",
        "self.uuid_from_source(doc)",
    )
    return source


def _rebind_method(method_name: str) -> None:
    original = getattr(Matcher, method_name)
    source = _rewrite_method_source(textwrap.dedent(inspect.getsource(original)))
    namespace = dict(vars(matcher_mod))
    exec(compile(source, f"<patched {method_name}>", "exec"), namespace)  # noqa: S102
    setattr(Matcher, method_name, namespace[method_name])


def apply() -> None:
    Matcher.uuid_terms_field = uuid_terms_field
    Matcher.uuid_from_source = uuid_from_source

    for method_name in (
        "get_uuid_by_metadata",
        "match_kube_burner",
        "get_results",
        "get_agg_metric_query",
        "get_agg_metrics_batch",
        "get_results_batch",
    ):
        _rebind_method(method_name)


apply()
