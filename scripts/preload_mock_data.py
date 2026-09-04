#!/usr/bin/env python3
"""Pre-load OpenSearch with mock Chronicler / Zathras CoreMark benchmark documents."""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


DEFAULT_DATA = Path(__file__).resolve().parent.parent / "data" / "mock_runs.json"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
SUMMARY_TEMPLATE = CONFIG_DIR / "opensearch_index_template.json"
TIMESERIES_TEMPLATE = CONFIG_DIR / "opensearch_timeseries_template.json"

INSTANCE_TYPE = "mock_x86_8c"
CLOUD_PROVIDER = "local"
OS_VENDOR = "fedora"
SCENARIO_NAME = "zathras-orion-eval"
COREMARK_VERSION = "v1.01"
WRAPPER_VERSION = "v2.5"


class OpenSearchClient:
    def __init__(self, base_url: str, insecure: bool = False) -> None:
        self.base_url, self._auth_header = self._normalize_base_url(base_url)
        self._ssl_context = self._build_ssl_context(insecure)

    @staticmethod
    def _normalize_base_url(base_url: str) -> tuple[str, dict[str, str]]:
        parsed = urlsplit(base_url.rstrip("/"))
        headers: dict[str, str] = {}

        username = parsed.username or os.environ.get("ES_USERNAME")
        password = parsed.password or os.environ.get("ES_PASSWORD")
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"

        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        clean = urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
        return clean, headers

    @staticmethod
    def _build_ssl_context(insecure: bool) -> ssl.SSLContext | None:
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | list[Any] | None = None,
        timeout: float = 30.0,
    ) -> tuple[int, Any]:
        data = None
        headers = {"Content-Type": "application/json", **self._auth_header}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        url = f"{self.base_url}{path}"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout, context=self._ssl_context) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed: Any = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = raw
            return exc.code, parsed


def wait_for_opensearch(client: OpenSearchClient, attempts: int = 60, delay: float = 2.0) -> None:
    print(f"==> Waiting for OpenSearch at {client.base_url}")
    last_err = "unknown"
    for i in range(1, attempts + 1):
        try:
            status, payload = client.request("GET", "/")
            if status == 200 and isinstance(payload, dict) and payload.get("version"):
                print(f"==> OpenSearch ready (attempt {i}): {payload['version'].get('number')}")
                return
            last_err = f"status={status} payload={payload}"
        except (URLError, TimeoutError, OSError) as exc:
            last_err = str(exc)
        time.sleep(delay)
    raise RuntimeError(f"OpenSearch not ready after {attempts} attempts: {last_err}")


def apply_index_template(client: OpenSearchClient, template_path: Path, template_name: str) -> None:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    status, payload = client.request(
        "PUT",
        f"/_index_template/{template_name}",
        template,
    )
    if status not in (200, 201):
        raise RuntimeError(f"Failed to apply index template {template_name}: {status} {payload}")
    print(f"==> Applied index template: {template_name}")


def recreate_index(client: OpenSearchClient, index: str) -> None:
    status, _ = client.request("DELETE", f"/{index}")
    if status in (200, 404):
        print(
            f"==> Recreated index (removed existing): {index}"
            if status == 200
            else f"==> Creating index: {index}"
        )
    # Orion expects uuid.keyword; Chronicler templates map strings as keyword.
    # Pin helper fields so Orion and Chronicler queries both work.
    status, payload = client.request(
        "PUT",
        f"/{index}",
        {
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "index": {"mapping": {"total_fields": {"limit": 5000}}},
            },
            "mappings": {
                "properties": {
                    "uuid": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "timestamp": {"type": "date"},
                    "iterations_per_second": {"type": "float"},
                }
            },
        },
    )
    if status not in (200, 201):
        raise RuntimeError(f"Failed to create index {index}: {status} {payload}")
    print(f"==> Created index: {index}")


def index_doc(client: OpenSearchClient, index: str, doc_id: str, document: dict[str, Any]) -> None:
    status, payload = client.request("PUT", f"/{index}/_doc/{doc_id}", document)
    if status not in (200, 201):
        raise RuntimeError(f"Failed to index {doc_id} into {index}: {status} {payload}")


def normalize_runs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    runs = []
    now = datetime.now(timezone.utc)
    for run in payload["runs"]:
        normalized = dict(run)
        if "day_offset" in run:
            ts = now - timedelta(days=int(run["day_offset"]))
            normalized["test_timestamp"] = ts.replace(
                hour=12, minute=0, second=0, microsecond=0
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif "test_timestamp" not in run:
            raise ValueError(
                f"Run {run.get('result_uuid', '?')} needs day_offset or test_timestamp"
            )
        runs.append(normalized)
    return runs


def _timeseries_summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "stddev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "first_value": values[0] if values else None,
        "last_value": values[-1] if values else None,
    }


def _run_block(
    run_number: int,
    iterations_per_second: float,
    start_time: str,
    include_timeseries: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a Chronicler-style run object and optional timeseries docs."""
    values = [
        iterations_per_second * factor
        for factor in (0.995, 1.0, 1.002, 0.998, 1.001)
    ]
    run_key = f"run_{run_number}"
    run_doc = {
        "run_number": run_number,
        "status": "PASS",
        "start_time": start_time,
        "end_time": start_time,
        "duration_seconds": 12.5,
        "configuration": {
            "compiler": "gcc (GCC) 14.2.1",
            "compiler_flags": "-O3 -funroll-all-loops -finline-limit=600",
            "threads": 4,
        },
        "metrics": {
            "iterations_per_second": iterations_per_second,
            "total_iterations": 10000000,
            "total_time_seconds": 51.7,
            "coremark_size": 0,
        },
        "timeseries_summary": _timeseries_summary(values),
        "validation": {
            "status": "PASS",
            "seedcrc": "0xe9f5",
            "threads": [
                {
                    "thread": 0,
                    "crcfinal": "0x65c5",
                    "crclist": "0x5147",
                    "crcmatrix": "0x1fd7",
                    "crcstate": "0x8e7a",
                }
            ],
        },
    }

    timeseries_docs: list[dict[str, Any]] = []
    if include_timeseries:
        timeseries: dict[str, Any] = {}
        for sequence, value in enumerate(values):
            point_time = (
                datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                + timedelta(seconds=sequence)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            seq_key = f"sequence_{sequence}"
            timeseries[seq_key] = {
                "timestamp": point_time,
                "metrics": {"iterations_per_second": value},
            }
            timeseries_docs.append(
                {
                    "sequence": sequence,
                    "timestamp": point_time,
                    "value": value,
                    "run_key": run_key,
                    "run_number": run_number,
                }
            )
        run_doc["timeseries"] = timeseries

    return run_doc, timeseries_docs


def build_summary_document(run: dict[str, Any], include_timeseries: bool = False) -> dict[str, Any]:
    """Build a Zathras summary document matching Chronicler schema.py structure."""
    test_timestamp = run["test_timestamp"]
    processing_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result_uuid = run["result_uuid"]
    document_id = f"coremark_{INSTANCE_TYPE}_1_{test_timestamp[:10].replace('-', '')}"

    run_1, ts_1 = _run_block(1, float(run["iterations_per_second"]), test_timestamp, include_timeseries)
    run_2_value = float(run.get("run_2_iterations_per_second", run["iterations_per_second"]))
    run_2, ts_2 = _run_block(2, run_2_value, test_timestamp, include_timeseries)

    all_values = [run_1["metrics"]["iterations_per_second"], run_2["metrics"]["iterations_per_second"]]
    primary_value = statistics.mean(all_values)

    summary = {
        "uuid": result_uuid,
        "timestamp": test_timestamp,
        "iterations_per_second": primary_value,
        "metadata": {
            "document_id": document_id,
            "document_type": "zathras_test_result",
            "zathras_version": "1.0",
            "test_timestamp": test_timestamp,
            "processing_timestamp": processing_timestamp,
            "collection_timestamp": test_timestamp,
            "os_vendor": OS_VENDOR,
            "cloud_provider": CLOUD_PROVIDER,
            "instance_type": INSTANCE_TYPE,
            "iteration": 1,
            "scenario_name": SCENARIO_NAME,
            "project_uuid": "33333333-3333-4333-a333-333333333301",
            "run_uuid": "44444444-4444-4444-a444-444444444401",
            "result_uuid": result_uuid,
        },
        "test": {
            "name": "coremark",
            "version": COREMARK_VERSION,
            "wrapper_version": WRAPPER_VERSION,
            "schema_version": "1.0",
            "description": "CoreMark CPU benchmark (mock Chronicler export)",
            "url": "https://github.com/redhat-performance/coremark-wrapper",
        },
        "system_under_test": {
            "hardware": {
                "cpu": {
                    "vendor": "Intel",
                    "model": "Xeon Platinum 8370C",
                    "architecture": "x86_64",
                    "cores": 8,
                    "threads_per_core": 2,
                    "sockets": 1,
                    "numa_nodes": 1,
                    "frequency_mhz": 2800.0,
                    "flags": {"avx2": True, "avx512": True, "sse4_2": True},
                },
                "memory": {
                    "total_gb": 32,
                    "total_kb": 33554432,
                    "available_kb": 30000000,
                    "speed_mhz": 3200,
                    "type": "DDR4",
                },
            },
            "operating_system": {
                "distribution": "Fedora",
                "version": "44",
                "kernel_version": "6.14.0",
                "hostname": "mock-sut.local",
            },
            "configuration": {
                "tuned_profile": "throughput-performance",
                "selinux_status": "enforcing",
                "transparent_hugepages": "always",
            },
        },
        "test_configuration": {
            "iterations_requested": 5,
            "parameters": {
                "test_iterations": 5,
                "threads": 4,
            },
            "tuning": {
                "tuned_setting": "throughput-performance",
            },
        },
        "results": {
            "status": "PASS",
            "execution_time_seconds": 25.0,
            "total_runs": 2,
            "primary_metrics": [
                {
                    "name": "iterations_per_second",
                    "value": primary_value,
                    "unit": "per_second",
                }
            ],
            "overall_statistics": {
                "mean": primary_value,
                "median": statistics.median(all_values),
                "min": min(all_values),
                "max": max(all_values),
                "stddev": statistics.stdev(all_values) if len(all_values) > 1 else 0.0,
                "sample_count": len(all_values),
            },
            "runs": {
                "run_1": run_1,
                "run_2": run_2,
            },
        },
        "runtime_info": {
            "start_time": test_timestamp,
            "end_time": test_timestamp,
            "duration_seconds": 25.0,
            "command": "./coremark_run --iterations 5",
            "working_directory": "/tmp/coremark",
            "user": "root",
        },
        "_export_metadata": {
            "exported_at": processing_timestamp,
            "exporter": "zathras-opensearch-exporter",
            "exporter_version": "1.0.0",
        },
    }

    if run.get("note"):
        summary["metadata"]["note"] = run["note"]

    if not include_timeseries:
        for run_key in ("run_1", "run_2"):
            summary["results"]["runs"][run_key].pop("timeseries", None)

    summary["_timeseries_points"] = ts_1 + ts_2
    return summary


def build_timeseries_document(
    summary: dict[str, Any],
    point: dict[str, Any],
) -> dict[str, Any]:
    """Build a zathras-timeseries document for one CoreMark sequence point."""
    metadata = summary["metadata"]
    timeseries_id = (
        f"{metadata['document_id']}_{point['run_key']}_sequence_{point['sequence']}"
    )
    return {
        "metadata": {
            "document_id": metadata["document_id"],
            "timeseries_id": timeseries_id,
            "timestamp": point["timestamp"],
            "sequence": point["sequence"],
            "test_timestamp": metadata["test_timestamp"],
            "processing_timestamp": metadata["processing_timestamp"],
            "os_vendor": metadata["os_vendor"],
            "cloud_provider": metadata["cloud_provider"],
            "instance_type": metadata["instance_type"],
            "scenario_name": metadata["scenario_name"],
            "iteration": metadata["iteration"],
        },
        "test": summary["test"],
        "system_under_test": summary["system_under_test"],
        "results": {
            "run": {
                "run_key": point["run_key"],
                "run_number": point["run_number"],
                "status": "PASS",
                "configuration": summary["results"]["runs"][point["run_key"]]["configuration"],
            },
            "value": point["value"],
            "unit": "per_second",
            "point_metrics": {
                "iterations_per_second": point["value"],
            },
        },
        "_export_metadata": summary["_export_metadata"],
    }


def count_docs(client: OpenSearchClient, index: str) -> int:
    status, payload = client.request("GET", f"/{index}/_count")
    if status != 200 or not isinstance(payload, dict):
        return -1
    return int(payload.get("count", 0))


def verify_preload(
    client: OpenSearchClient,
    summary_index: str,
    timeseries_index: str,
) -> None:
    summary_count = count_docs(client, summary_index)
    ts_count = count_docs(client, timeseries_index)
    print(
        f"==> Index document counts: {summary_index}={summary_count}, "
        f"{timeseries_index}={ts_count}"
    )
    if summary_count <= 0:
        raise RuntimeError(f"Preload verification failed: no documents in {summary_index}")
    if ts_count <= 0:
        raise RuntimeError(f"Preload verification failed: no documents in {timeseries_index}")

    status, payload = client.request(
        "POST",
        f"/{summary_index}/_search",
        {
            "size": 1,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"test.name": "coremark"}},
                        {"term": {"metadata.cloud_provider": CLOUD_PROVIDER}},
                        {"term": {"metadata.instance_type": INSTANCE_TYPE}},
                    ]
                }
            },
        },
    )
    if status != 200:
        raise RuntimeError(f"Chronicler-style summary probe failed: {status} {payload}")
    hits = payload.get("hits", {}).get("total", {})
    total = hits.get("value", 0) if isinstance(hits, dict) else hits
    print(f"==> Chronicler-style summary probe matched {total} document(s)")
    if total <= 0:
        raise RuntimeError("Preload verification failed: Chronicler summary query returned 0 hits")

    status, payload = client.request(
        "POST",
        f"/{timeseries_index}/_search",
        {
            "size": 0,
            "query": {"term": {"test.name": "coremark"}},
            "aggs": {
                "avg_value": {"avg": {"field": "results.value"}},
            },
        },
    )
    if status != 200:
        raise RuntimeError(f"Chronicler-style timeseries probe failed: {status} {payload}")
    avg_value = payload.get("aggregations", {}).get("avg_value", {}).get("value")
    print(f"==> Chronicler-style timeseries avg results.value: {avg_value}")

    status, payload = client.request(
        "POST",
        f"/{summary_index}/_search",
        {
            "size": 1,
            "query": {
                "bool": {
                    "must": [
                        {"match": {"test.name": "coremark"}},
                        {"match": {"metadata.cloud_provider": CLOUD_PROVIDER}},
                        {"match": {"metadata.instance_type": INSTANCE_TYPE}},
                        {"match": {"metadata.scenario_name": SCENARIO_NAME}},
                    ]
                }
            },
        },
    )
    if status != 200:
        raise RuntimeError(f"Orion-style metadata probe failed: {status} {payload}")
    sample_uuid = payload.get("hits", {}).get("hits", [{}])[0].get("_source", {}).get("uuid")
    if not sample_uuid:
        raise RuntimeError("Preload verification failed: could not sample a summary uuid")

    status, payload = client.request(
        "POST",
        f"/{summary_index}/_search",
        {
            "size": 0,
            "query": {
                "bool": {
                    "must": [
                        {"terms": {"uuid.keyword": [sample_uuid]}},
                    ]
                }
            },
            "aggs": {
                "by_uuid": {
                    "terms": {"field": "uuid.keyword", "size": 1},
                    "aggs": {
                        "avg_iterations_per_second": {
                            "avg": {"field": "iterations_per_second"}
                        }
                    },
                }
            },
        },
    )
    if status != 200:
        raise RuntimeError(f"Orion-style metrics probe failed: {status} {payload}")
    buckets = payload.get("aggregations", {}).get("by_uuid", {}).get("buckets", [])
    print(f"==> Orion-style metrics probe matched {len(buckets)} uuid bucket(s)")
    if not buckets:
        raise RuntimeError("Preload verification failed: Orion metrics aggregation returned 0 buckets")


def preload(client: OpenSearchClient, data_path: Path, recreate: bool = True) -> None:
    payload = json.loads(data_path.read_text(encoding="utf-8"))
    summary_index = payload["summary_index"]
    timeseries_index = payload["timeseries_index"]
    runs = normalize_runs(payload)

    if SUMMARY_TEMPLATE.is_file():
        apply_index_template(client, SUMMARY_TEMPLATE, "zathras-results-template")
    if TIMESERIES_TEMPLATE.is_file():
        apply_index_template(client, TIMESERIES_TEMPLATE, "zathras-timeseries-template")

    if recreate:
        recreate_index(client, summary_index)
        recreate_index(client, timeseries_index)

    print(f"==> Loading {len(runs)} mock CoreMark runs into Chronicler indices")
    for run in runs:
        summary = build_summary_document(run, include_timeseries=True)
        timeseries_points = summary.pop("_timeseries_points")
        document_id = summary["metadata"]["document_id"]
        index_doc(client, summary_index, document_id, summary)

        for point in timeseries_points:
            ts_doc = build_timeseries_document(summary, point)
            index_doc(
                client,
                timeseries_index,
                ts_doc["metadata"]["timeseries_id"],
                ts_doc,
            )

        print(
            f"    loaded {document_id} uuid={run['result_uuid']} @ {run['test_timestamp']}: "
            f"iterations_per_second={summary['iterations_per_second']:.1f} "
            f"timeseries_points={len(timeseries_points)}"
        )

    for index in (summary_index, timeseries_index):
        status, payload = client.request("POST", f"/{index}/_refresh")
        if status not in (200, 201):
            raise RuntimeError(f"Failed to refresh {index}: {status} {payload}")

    verify_preload(client, summary_index, timeseries_index)

    print("==> Mock Chronicler data preload complete")
    print(f"    summary index    : {summary_index}")
    print(f"    timeseries index : {timeseries_index}")
    print("    Latest run intentionally regresses iterations_per_second by >5%")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--es-server",
        default=os.environ.get("ES_SERVER", "http://127.0.0.1:9200"),
        help="OpenSearch base URL (default: ES_SERVER or http://127.0.0.1:9200)",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA,
        help=f"Mock runs JSON (default: {DEFAULT_DATA})",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=os.environ.get("ES_INSECURE", "").lower() in {"1", "true", "yes"},
        help="Skip TLS certificate verification (or set ES_INSECURE=true)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only verify OpenSearch is reachable, do not load data",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not delete/recreate indexes before loading mock data",
    )
    args = parser.parse_args()

    client = OpenSearchClient(args.es_server, insecure=args.insecure)
    wait_for_opensearch(client)
    if args.check_only:
        print(f"==> External OpenSearch is reachable at {client.base_url}")
        return 0
    preload(client, args.data, recreate=not args.no_recreate)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - surface load failures clearly in containers
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
