#!/usr/bin/env python3
"""Diagnose why Orion cannot find matching OpenSearch metadata."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preload_mock_data import (  # noqa: E402
    CLOUD_PROVIDER,
    INSTANCE_TYPE,
    SCENARIO_NAME,
    OpenSearchClient,
)


def parse_lookback(lookback: str) -> timedelta:
    days = 0
    hours = 0
    if match := re.fullmatch(r"(\d+)d(\d+)h", lookback):
        days, hours = int(match.group(1)), int(match.group(2))
    elif match := re.fullmatch(r"(\d+)d", lookback):
        days = int(match.group(1))
    elif match := re.fullmatch(r"(\d+)h", lookback):
        hours = int(match.group(1))
    else:
        days = 30
    return timedelta(days=days, hours=hours)


def main() -> int:
    es_server = os.environ.get("ES_SERVER", "http://127.0.0.1:9200")
    metadata_index = os.environ.get("ES_METADATA_INDEX", "zathras-results")
    benchmark_index = os.environ.get("ES_BENCHMARK_INDEX", "zathras-results")
    timeseries_index = os.environ.get("ES_TIMESERIES_INDEX", "zathras-timeseries")
    lookback = os.environ.get("LOOKBACK", "30d")
    test_version = os.environ.get("TEST_VERSION", "v1.01")
    insecure = os.environ.get("ES_INSECURE", "").lower() in {"1", "true", "yes"}

    client = OpenSearchClient(es_server, insecure=insecure)
    lookback_start = (
        datetime.now(timezone.utc) - parse_lookback(lookback)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("==> Orion no-data diagnostics")
    print(f"    ES_SERVER          : {client.base_url}")
    print(f"    summary index      : {metadata_index}")
    print(f"    benchmark index    : {benchmark_index}")
    print(f"    timeseries index   : {timeseries_index}")
    print(f"    lookback           : {lookback} (metadata.test_timestamp > {lookback_start})")
    print(f"    test version       : {test_version}")

    for index in (metadata_index, benchmark_index, timeseries_index):
        status, payload = client.request("GET", f"/{index}/_count")
        count = payload.get("count", "?") if status == 200 else f"error({status})"
        print(f"    {index} count : {count}")

    queries = [
        ("all summary docs", {"match_all": {}}),
        (
            "Chronicler coremark summary filters",
            {
                "bool": {
                    "must": [
                        {"term": {"test.name": "coremark"}},
                        {"term": {"metadata.cloud_provider": CLOUD_PROVIDER}},
                        {"term": {"metadata.instance_type": INSTANCE_TYPE}},
                    ]
                }
            },
        ),
        (
            "Orion metadata filters (no lookback)",
            {
                "bool": {
                    "must": [
                        {"match": {"test.name": "coremark"}},
                        {"match": {"metadata.cloud_provider": CLOUD_PROVIDER}},
                        {"match": {"metadata.instance_type": INSTANCE_TYPE}},
                        {"match": {"metadata.scenario_name": SCENARIO_NAME}},
                    ]
                }
            },
        ),
        (
            "Orion metadata filters + lookback",
            {
                "bool": {
                    "must": [
                        {"match": {"test.name": "coremark"}},
                        {"match": {"metadata.cloud_provider": CLOUD_PROVIDER}},
                        {"match": {"metadata.instance_type": INSTANCE_TYPE}},
                        {"match": {"metadata.scenario_name": SCENARIO_NAME}},
                    ],
                    "filter": [
                        {"range": {"metadata.test_timestamp": {"gt": lookback_start}}},
                    ],
                }
            },
        ),
    ]

    for label, query in queries:
        status, payload = client.request(
            "POST",
            f"/{metadata_index}/_search",
            {"size": 3, "sort": [{"metadata.test_timestamp": {"order": "desc"}}], "query": query},
        )
        if status != 200:
            print(f"    probe '{label}': query failed ({status})")
            continue
        total = payload.get("hits", {}).get("total", {})
        count = total.get("value", total) if isinstance(total, dict) else total
        print(f"    probe '{label}': {count} hit(s)")
        for hit in payload.get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            print(
                f"      - uuid={src.get('uuid') or src.get('metadata', {}).get('document_id')} "
                f"test_timestamp={src.get('metadata', {}).get('test_timestamp')} "
                f"test={src.get('test', {}).get('name')} "
                f"iterations_per_second={src.get('iterations_per_second')}"
            )

    print("==> Tips:")
    print("    - Rebuild image after config changes: podman build -t zathras-orion-eval:latest .")
    print("    - Ensure PRELOAD_MOCK_DATA=true for mock/demo runs")
    print("    - For external OpenSearch, verify ES_METADATA_INDEX / ES_BENCHMARK_INDEX")
    print("    - Chronicler exports use zathras-results and zathras-timeseries indices")
    print("    - Set ORION_DEBUG=true for Orion query logging")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
