#!/usr/bin/env bash
##export ES_SERVER="https://opensearch.example.com:9200"
##export ES_USERNAME="admin"
##export ES_PASSWORD="secret"

export ES_INSECURE=true
export PRELOAD_MOCK_DATA=false
export START_OPENSEARCH=false
export NETWORK_MODE=host   # if OpenSearch is on the host
export ORION_CONFIG="/opt/zathras-orion-eval/config/coremark-chronicler-external.yaml"

podman build -t zathras-orion-eval:latest .

./run.sh

