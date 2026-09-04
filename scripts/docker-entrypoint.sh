#!/usr/bin/env bash
# Runtime stages (all optional except Orion when invoked without args):
#   1) start embedded OpenSearch (skipped for external clusters)
#   2) preload mock Chronicler / Zathras CoreMark results (optional)
#   3) run Orion against OpenSearch with a 5% regression threshold
set -euo pipefail

OPENSEARCH_HOME="${OPENSEARCH_HOME:-/opt/opensearch}"
ES_SERVER="${ES_SERVER:-http://127.0.0.1:9200}"
ES_METADATA_INDEX="${ES_METADATA_INDEX:-zathras-results}"
ES_BENCHMARK_INDEX="${ES_BENCHMARK_INDEX:-zathras-results}"
ORION_CONFIG="${ORION_CONFIG:-/opt/zathras-orion-eval/config/coremark-regression.yaml}"
ORION_OUTPUT="${ORION_OUTPUT:-/opt/zathras-orion-eval/output/regression-report.json}"
LOOKBACK="${LOOKBACK:-30d}"
TEST_VERSION="${TEST_VERSION:-v1.01}"
OPENSEARCH_USER="${OPENSEARCH_USER:-opensearch}"
ES_INSECURE="${ES_INSECURE:-false}"
PRELOAD_MOCK_DATA="${PRELOAD_MOCK_DATA:-true}"
REGRESSION_THRESHOLD="${REGRESSION_THRESHOLD:-5}"
REGRESSION_UUIDS_OUTPUT="${REGRESSION_UUIDS_OUTPUT:-/opt/zathras-orion-eval/output/regression-uuids.json}"

log() { printf '%s\n' "==> $*"; }

# Orion's OpenSearch client only authenticates via credentials embedded in --es-server.
# Preload accepts ES_USERNAME/ES_PASSWORD separately; inject them for Orion when needed.
resolve_orion_es_server() {
  if [[ -z "${ES_USERNAME:-}" || -z "${ES_PASSWORD:-}" ]]; then
    printf '%s' "${ES_SERVER}"
    return
  fi
  case "${ES_SERVER}" in
    *://*@*)
      printf '%s' "${ES_SERVER}"
      return
      ;;
  esac
  ES_SERVER="${ES_SERVER}" ES_USERNAME="${ES_USERNAME}" ES_PASSWORD="${ES_PASSWORD}" \
  python3 - <<'PY'
import os
from urllib.parse import quote, urlsplit, urlunsplit

url = os.environ["ES_SERVER"]
user = quote(os.environ["ES_USERNAME"], safe="")
password = quote(os.environ["ES_PASSWORD"], safe="")
parsed = urlsplit(url)
host = parsed.hostname or ""
if parsed.port:
    host = f"{host}:{parsed.port}"
netloc = f"{user}:{password}@{host}"
print(urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)))
PY
}

is_local_es_server() {
  case "${ES_SERVER}" in
    http://127.0.0.1|http://127.0.0.1:*|http://localhost|http://localhost:*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

resolve_start_opensearch() {
  if [[ -n "${START_OPENSEARCH:-}" ]]; then
    return
  fi
  if is_local_es_server; then
    START_OPENSEARCH=true
  else
    START_OPENSEARCH=false
  fi
}

cleanup() {
  if [[ -n "${OPENSEARCH_PID:-}" ]] && kill -0 "${OPENSEARCH_PID}" 2>/dev/null; then
    log "Stopping embedded OpenSearch (pid ${OPENSEARCH_PID})"
    kill "${OPENSEARCH_PID}" 2>/dev/null || true
    wait "${OPENSEARCH_PID}" 2>/dev/null || true
  fi
}

run_as_opensearch() {
  if [[ "$(id -u)" -eq 0 ]] && id "${OPENSEARCH_USER}" &>/dev/null; then
    runuser -u "${OPENSEARCH_USER}" -- "$@"
  else
    "$@"
  fi
}

stage_opensearch() {
  log "[stage 1/3] Starting embedded OpenSearch single-node instance"
  export JAVA_HOME="${JAVA_HOME:-${OPENSEARCH_HOME}/jdk}"
  export PATH="${JAVA_HOME}/bin:${PATH}"
  export OPENSEARCH_JAVA_OPTS="${OPENSEARCH_JAVA_OPTS:--Xms512m -Xmx512m}"
  export DISABLE_SECURITY_PLUGIN="${DISABLE_SECURITY_PLUGIN:-true}"
  export DISABLE_INSTALL_DEMO_CONFIG="${DISABLE_INSTALL_DEMO_CONFIG:-true}"

  mkdir -p "${OPENSEARCH_HOME}/data" "${OPENSEARCH_HOME}/logs"
  if id "${OPENSEARCH_USER}" &>/dev/null; then
    chown -R "${OPENSEARCH_USER}:${OPENSEARCH_USER}" \
      "${OPENSEARCH_HOME}/data" "${OPENSEARCH_HOME}/logs" || true
  fi

  cat > "${OPENSEARCH_HOME}/config/opensearch.yml" <<'EOF'
cluster.name: zathras-orion-eval
node.name: zathras-orion-eval-node
network.host: 0.0.0.0
http.port: 9200
discovery.type: single-node
bootstrap.memory_lock: false
EOF
  if id "${OPENSEARCH_USER}" &>/dev/null; then
    chown "${OPENSEARCH_USER}:${OPENSEARCH_USER}" \
      "${OPENSEARCH_HOME}/config/opensearch.yml" || true
  fi

  if [[ -d "${OPENSEARCH_HOME}/plugins/opensearch-security" ]]; then
    log "Disabling OpenSearch security plugin for local evaluation"
    rm -rf "${OPENSEARCH_HOME}/plugins/opensearch-security"
  fi

  run_as_opensearch env \
    JAVA_HOME="${JAVA_HOME}" \
    PATH="${PATH}" \
    OPENSEARCH_JAVA_OPTS="${OPENSEARCH_JAVA_OPTS}" \
    "${OPENSEARCH_HOME}/bin/opensearch" &
  OPENSEARCH_PID=$!
  trap cleanup EXIT
  log "OpenSearch pid=${OPENSEARCH_PID}"
}

stage_external_opensearch() {
  log "[stage 1/3] Using external OpenSearch at ${ES_SERVER}"
  python3 /opt/zathras-orion-eval/scripts/preload_mock_data.py \
    --es-server "${ES_SERVER}" \
    ${ES_INSECURE:+--insecure} \
    --check-only
}

stage_preload() {
  log "[stage 2/3] Pre-loading mock throughput/latency test-run results"
  python3 /opt/zathras-orion-eval/scripts/preload_mock_data.py \
    --es-server "${ES_SERVER}" \
    ${ES_INSECURE:+--insecure}
}

summarize_orion_reports() {
  REGRESSION_THRESHOLD="${REGRESSION_THRESHOLD}" \
  REGRESSION_UUIDS_OUTPUT="${REGRESSION_UUIDS_OUTPUT}" \
  DOCUMENT_ID_FIELD="${DOCUMENT_ID_FIELD:-metadata.document_id}" \
  python3 - <<'PY'
import json
import os
import pathlib

THRESHOLD = float(os.environ.get("REGRESSION_THRESHOLD", "5"))
DOCUMENT_ID_FIELD = os.environ.get("DOCUMENT_ID_FIELD", "metadata.document_id")
UUIDS_OUTPUT = pathlib.Path(os.environ.get(
    "REGRESSION_UUIDS_OUTPUT",
    "/opt/zathras-orion-eval/output/regression-uuids.json",
))
out_dir = pathlib.Path("/opt/zathras-orion-eval/output")
files = sorted(out_dir.glob("*.json"))
files = [f for f in files if f.name != UUIDS_OUTPUT.name]

if not files:
    print("No Orion JSON reports found under /opt/zathras-orion-eval/output")
    raise SystemExit(0)

regressed_records = []
all_document_ids = []

def split_ids(id_value):
    if not id_value:
        return []
    if isinstance(id_value, list):
        return [str(item).strip() for item in id_value if str(item).strip()]
    return [part.strip() for part in str(id_value).split(",") if part.strip()]

def extract_document_ids(record):
    """Orion stores the configured uuid_field value under the 'uuid' key."""
    ids = split_ids(record.get("uuid"))
    if ids:
        return ids

    flat = record.get(DOCUMENT_ID_FIELD)
    if flat:
        return split_ids(flat)

    metadata = record.get("metadata")
    if isinstance(metadata, dict) and metadata.get("document_id"):
        return [str(metadata["document_id"])]

    return []

def metric_exceeds_threshold(metric_name, metric_data):
    pct = metric_data.get("percentage_change")
    if pct is None:
        return False, None
    try:
        pct_f = float(pct)
    except (TypeError, ValueError):
        return False, None
    return abs(pct_f) > THRESHOLD, pct_f

def analyze_record(record):
    if not isinstance(record, dict):
        return None

    document_ids = extract_document_ids(record)
    metrics_block = record.get("metrics") or {}
    regressed_metrics = []

    for metric_name, metric_data in metrics_block.items():
        if not isinstance(metric_data, dict):
            continue
        exceeds, pct_f = metric_exceeds_threshold(metric_name, metric_data)
        if exceeds:
            regressed_metrics.append({
                "metric": metric_name,
                "percentage_change": pct_f,
                "value": metric_data.get("value"),
                "labels": metric_data.get("labels", []),
            })

    if not regressed_metrics:
        return None

    return {
        "document_ids": document_ids,
        "is_changepoint": bool(record.get("is_changepoint")),
        "timestamp": record.get("timestamp"),
        "model": record.get("model"),
        "buildUrl": record.get("buildUrl"),
        "regressed_metrics": regressed_metrics,
    }

for path in files:
    print(f"--- Orion report: {path.name} ---")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"Could not parse {path}: {exc}")
        print(path.read_text(encoding="utf-8")[:2000])
        continue
    print(json.dumps(data, indent=2)[:6000])

    records = data if isinstance(data, list) else [data]
    for record in records:
        all_document_ids.extend(extract_document_ids(record))
        result = analyze_record(record)
        if result:
            regressed_records.append(result)

unique_document_ids = list(dict.fromkeys(all_document_ids))

regressed_document_ids = []
for entry in regressed_records:
    regressed_document_ids.extend(entry["document_ids"])
unique_regressed_document_ids = list(dict.fromkeys(regressed_document_ids))

summary = {
    "threshold_percent": THRESHOLD,
    "document_id_field": DOCUMENT_ID_FIELD,
    "documents_analyzed": len(unique_document_ids),
    "regressed_document_id_count": len(unique_regressed_document_ids),
    "regressed_document_ids": unique_regressed_document_ids,
    "records": regressed_records,
}

UUIDS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
UUIDS_OUTPUT.write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(f"\nWrote regression summary: {UUIDS_OUTPUT}")
print(f"\nDocuments analyzed: {len(unique_document_ids)}")

if regressed_records:
    print(f"\nRegressions above {THRESHOLD:g}%:")
    for entry in regressed_records:
        doc_id_list = ", ".join(entry["document_ids"]) or "(no document id)"
        print(f"  {DOCUMENT_ID_FIELD}: {doc_id_list}")
        if entry.get("model"):
            print(f"    model: {entry['model']}")
        if entry.get("buildUrl"):
            print(f"    buildUrl: {entry['buildUrl']}")
        for metric in entry["regressed_metrics"]:
            print(
                f"    - {metric['metric']}: {metric['percentage_change']:+.2f}%"
            )
    print(f"\nAll regressed {DOCUMENT_ID_FIELD} values ({len(unique_regressed_document_ids)}):")
    for document_id in unique_regressed_document_ids:
        print(f"  - {document_id}")
else:
    print(f"\nNo records with metrics above {THRESHOLD:g}% threshold.")
    print("Inspect the JSON report above for Orion's full comparison output.")
PY
}

diagnose_orion_no_data() {
  ES_SERVER="${ES_SERVER}" \
  ES_METADATA_INDEX="${ES_METADATA_INDEX}" \
  ES_BENCHMARK_INDEX="${ES_BENCHMARK_INDEX}" \
  LOOKBACK="${LOOKBACK}" \
  MODEL="${TEST_VERSION}" \
  ES_INSECURE="${ES_INSECURE}" \
  python3 /opt/zathras-orion-eval/scripts/diagnose_opensearch.py
}

stage_orion() {
  log "[stage 3/3] Running Orion regression analysis (threshold=${REGRESSION_THRESHOLD}%)"
  mkdir -p "$(dirname "${ORION_OUTPUT}")"

  set +e
  ORION_ES_SERVER="$(resolve_orion_es_server)"
  ORION_ARGS=(
    --config "${ORION_CONFIG}"
    --es-server "${ORION_ES_SERVER}"
    --metadata-index "${ES_METADATA_INDEX}"
    --benchmark-index "${ES_BENCHMARK_INDEX}"
    --lookback "${LOOKBACK}"
    --input-vars "{\"test_version\": \"${TEST_VERSION}\"}"
    --cmr
    --save-output-path "${ORION_OUTPUT}"
    -o json
  )
  if [[ "${ORION_DEBUG:-false}" == "true" ]]; then
    ORION_ARGS+=(--debug)
  fi
  python3 /opt/zathras-orion-eval/scripts/run_orion.py "${ORION_ARGS[@]}"
  rc=$?
  set -e

  log "Orion exit code: ${rc} (0=ok, 2=regressions found, 3=no matching data)"
  if [[ "${rc}" -eq 3 ]]; then
    log "Orion found no matching metadata/metrics in OpenSearch for the configured filters"
    diagnose_orion_no_data || true
  fi
  summarize_orion_reports
  return "${rc}"
}

main() {
  if [[ $# -gt 0 ]]; then
    exec "$@"
  fi

  resolve_start_opensearch

  if [[ "${START_OPENSEARCH}" == "true" ]]; then
    stage_opensearch
  else
    stage_external_opensearch
  fi

  if [[ "${PRELOAD_MOCK_DATA}" == "true" ]]; then
    stage_preload
  else
    log "[stage 2/3] Skipping mock preload (PRELOAD_MOCK_DATA=false)"
  fi

  stage_orion
}

main "$@"
