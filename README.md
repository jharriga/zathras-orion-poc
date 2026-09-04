# zathras-orion-poc

Fedora 44 container that can run an embedded OpenSearch **or** connect to an existing cluster, optionally preload mock [Chronicler](https://github.com/redhat-performance/chronicler) / Zathras CoreMark benchmark documents, and use [cloud-bulldozer/orion](https://github.com/cloud-bulldozer/orion) to flag regressions above 5%.

Data follows the Chronicler object-based schema (`schema.py`) and CoreMark processor layout (`coremark_processor.py`), exported to the standard two-index architecture:

- **`zathras-results`** — summary documents (aggregated run metrics, no embedded timeseries)
- **`zathras-timeseries`** — individual CoreMark sequence points (`iterations_per_second`)

Top-level `uuid`, `timestamp`, and `iterations_per_second` fields are added during preload so Orion can query the same summary index for metadata and metrics.

## Layout

```
zathras-orion-eval/
├── Dockerfile
├── run.sh
├── config/
│   ├── coremark-regression.yaml
│   ├── opensearch_index_template.json
│   └── opensearch_timeseries_template.json
├── data/mock_runs.json
└── scripts/
    ├── docker-entrypoint.sh
    ├── preload_mock_data.py
    └── diagnose_opensearch.py
```

## Quick start (embedded OpenSearch)

```bash
chmod +x run.sh
./run.sh
```

## External OpenSearch

Point the container at an existing Chronicler-backed cluster and skip the embedded server:

```bash
export ES_SERVER="https://opensearch.example.com:9200"
export ES_USERNAME="admin"
export ES_PASSWORD="secret"
export ES_INSECURE=true              # self-signed TLS
export START_OPENSEARCH=false        # do not start embedded OpenSearch
export PRELOAD_MOCK_DATA=false       # skip mock data if indexes already populated
export ES_METADATA_INDEX="zathras-results"
export ES_BENCHMARK_INDEX="zathras-results"
export ORION_CONFIG="/opt/zathras-orion-eval/config/coremark-chronicler-external.yaml"
export NETWORK_MODE=host             # if OpenSearch is on the host (Linux)

./run.sh
```

Real Chronicler documents use `metadata.test_timestamp` (not a top-level `timestamp`) and store CoreMark scores under `results.runs.run_1.metrics.iterations_per_second`. The external Orion config points at those fields. Edit `config/coremark-chronicler-external.yaml` to add metadata filters for your environment (for example `metadata.cloud_provider`, `metadata.instance_type`).

Credentials can also be embedded in `ES_SERVER`:

```bash
export ES_SERVER="https://user:pass@opensearch.example.com:9200"
```

**Important:** Orion authenticates only through the `--es-server` URL. If you set `ES_USERNAME` and `ES_PASSWORD` separately, the entrypoint injects them into the URL passed to Orion automatically. Preload and diagnose already read those env vars directly.

For real Chronicler exports, use `config/coremark-chronicler-external.yaml`. It reads `metadata.test_timestamp` and `metadata.document_id`; `scripts/run_orion.py` patches Orion so nested UUID fields work (plain `orion` cannot).

When `ES_SERVER` is not `localhost` / `127.0.0.1`, embedded OpenSearch startup is skipped automatically even if `START_OPENSEARCH` is unset.

### Run only Orion (no preload)

```bash
ES_SERVER="https://opensearch.example.com:9200" \
START_OPENSEARCH=false \
PRELOAD_MOCK_DATA=false \
./run.sh
```

### Preload mock Chronicler data into an external cluster only

```bash
podman run --rm \
  --network host \
  -e ES_SERVER="https://opensearch.example.com:9200" \
  -e ES_INSECURE=true \
  zathras-orion-eval:latest \
  python3 /opt/zathras-orion-eval/scripts/preload_mock_data.py --insecure
```

## Pipeline

1. Start embedded OpenSearch **or** verify connectivity to `ES_SERVER`
2. Apply Chronicler index templates and load mock CoreMark runs into `zathras-results` / `zathras-timeseries`
3. Run Orion CMR analysis with a 5% threshold on `iterations_per_second`

Expected Orion exit codes:

- `0` — no regressions
- `2` — regressions detected (mock data intentionally triggers this)
- `3` — no matching data in OpenSearch

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ES_SERVER` | `http://127.0.0.1:9200` | OpenSearch URL (Orion + preload) |
| `ES_METADATA_INDEX` | `zathras-results` | Orion metadata / Chronicler summary index |
| `ES_BENCHMARK_INDEX` | `zathras-results` | Orion metrics index (top-level `iterations_per_second`) |
| `START_OPENSEARCH` | auto | `true` for localhost, `false` for external |
| `PRELOAD_MOCK_DATA` | `true` | Load `data/mock_runs.json` before Orion |
| `ES_INSECURE` | `false` | Skip TLS verification for preload |
| `ES_USERNAME` / `ES_PASSWORD` | — | Basic auth (if not in URL) |
| `LOOKBACK` | `30d` | Orion lookback window |
| `TEST_VERSION` | `v1.01` | CoreMark benchmark version (Orion `test.version`) |
| `DOCUMENT_ID_FIELD` | `metadata.document_id` | Label/field used in the regression summary |
| `NETWORK_MODE` | — | e.g. `host` for `podman run --network host` |

## Mock regression

Mock data includes six baseline CoreMark runs near **193k iterations/sec** and one regressed run at **~171.5k iterations/sec** (~11% drop). The regression summary reports `metadata.document_id` values and shows **7 documents analyzed** in the mock demo.

Each summary document includes:

- Chronicler metadata (`document_id`, `test_timestamp`, `instance_type`, UUIDs)
- `test.name: coremark` with benchmark version `v1.01`
- `results.runs.run_1` / `run_2` with `metrics.iterations_per_second`
- `results.primary_metrics` and `overall_statistics`
- Matching `zathras-timeseries` points per run sequence
- 
