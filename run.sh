#!/usr/bin/env bash
# Build and run the Orion + OpenSearch evaluation image.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="${IMAGE_NAME:-zathras-orion-eval:latest}"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-}"

if [[ -z "${CONTAINER_RUNTIME}" ]]; then
  if command -v podman >/dev/null 2>&1; then
    CONTAINER_RUNTIME=podman
  else
    CONTAINER_RUNTIME=docker
  fi
fi

log() { printf '%s\n' "==> $*"; }

cd "${ROOT_DIR}"

RUN_ARGS=()
ENV_ARGS=(
  -e "LOOKBACK=${LOOKBACK:-30d}"
  -e "TEST_VERSION=${TEST_VERSION:-v1.01}"
)

pass_env() {
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    ENV_ARGS+=(-e "${name}=${!name}")
  fi
}

for var in \
  ES_SERVER \
  ES_METADATA_INDEX \
  ES_BENCHMARK_INDEX \
  ES_USERNAME \
  ES_PASSWORD \
  ES_INSECURE \
  START_OPENSEARCH \
  PRELOAD_MOCK_DATA \
  ORION_CONFIG \
  ORION_OUTPUT \
  DOCUMENT_ID_FIELD; do
  pass_env "${var}"
done

if [[ "${START_OPENSEARCH:-}" == "false" ]]; then
  log "External OpenSearch mode (START_OPENSEARCH=false)"
  if [[ -z "${ES_SERVER:-}" ]]; then
    echo "ERROR: ES_SERVER is required when START_OPENSEARCH=false" >&2
    exit 1
  fi
else
  case "${ES_SERVER:-http://127.0.0.1:9200}" in
    http://127.0.0.1|http://127.0.0.1:*|http://localhost|http://localhost:*)
      ;;
    *)
      log "External OpenSearch detected via ES_SERVER=${ES_SERVER}"
      ENV_ARGS+=(-e "START_OPENSEARCH=false")
      ;;
  esac
fi

if [[ -n "${NETWORK_MODE:-}" ]]; then
  RUN_ARGS+=(--network "${NETWORK_MODE}")
fi

if [[ "${START_OPENSEARCH:-true}" != "false" ]]; then
  RUN_ARGS+=(
    --ulimit memlock=-1:-1
    --ulimit nofile=65536:65536
  )
fi

log "Building ${IMAGE_NAME} with ${CONTAINER_RUNTIME}"
${CONTAINER_RUNTIME} build \
  -f Dockerfile \
  -t "${IMAGE_NAME}" \
  .

log "Running Orion evaluation pipeline"
${CONTAINER_RUNTIME} run --rm -it \
  --name zathras-orion-eval \
  "${RUN_ARGS[@]}" \
  "${ENV_ARGS[@]}" \
  "${IMAGE_NAME}" "$@"
