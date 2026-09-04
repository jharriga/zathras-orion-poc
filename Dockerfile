# Multi-stage Fedora 44 image for OpenSearch + Orion regression evaluation.
#
# Stages:
#   1. opensearch-dist  - Official OpenSearch distribution (source of binaries)
#   2. base             - Fedora 44 common runtime packages
#   3. opensearch       - OpenSearch installed on Fedora 44
#   4. orion            - cloud-bulldozer/orion CLI installed on Fedora 44
#   5. runtime          - Combined image: start OS, preload mock data, run Orion
#
# Runtime pipeline (docker-entrypoint.sh):
#   start OpenSearch -> preload throughput/latency mocks -> Orion CMR @ 5%

############################
# Stage 1: OpenSearch dist #
############################
FROM docker.io/opensearchproject/opensearch:2.19.1 AS opensearch-dist

############################
# Stage 2: Fedora 44 base  #
############################
FROM registry.fedoraproject.org/fedora:44 AS base

RUN echo "==> [base] Installing common packages" && \
    dnf -y install \
        python3 \
        python3-pip \
        python3-devel \
        git \
        gcc \
        gcc-c++ \
        make \
        rust \
        cargo \
        openssl-devel \
        pkg-config \
        curl \
        tar \
        gzip \
        which \
        findutils \
        procps-ng \
        shadow-utils \
        util-linux \
    && dnf clean all

############################
# Stage 3: OpenSearch      #
############################
FROM base AS opensearch

ENV OPENSEARCH_HOME=/opt/opensearch \
    JAVA_HOME=/opt/opensearch/jdk \
    DISABLE_SECURITY_PLUGIN=true \
    DISABLE_INSTALL_DEMO_CONFIG=true \
    OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m"

RUN groupadd -g 1000 opensearch && \
    useradd -u 1000 -g 1000 -d "${OPENSEARCH_HOME}" -s /sbin/nologin opensearch

COPY --from=opensearch-dist --chown=1000:1000 /usr/share/opensearch/ ${OPENSEARCH_HOME}/

# Drop the security plugin so Orion can talk plain HTTP on :9200.
RUN rm -rf ${OPENSEARCH_HOME}/plugins/opensearch-security && \
    mkdir -p ${OPENSEARCH_HOME}/data ${OPENSEARCH_HOME}/logs && \
    chown -R opensearch:opensearch ${OPENSEARCH_HOME}

############################
# Stage 4: Orion CLI       #
############################
FROM base AS orion

WORKDIR /build

RUN echo "==> [orion] Cloning and installing cloud-bulldozer/orion" && \
    git clone --depth 1 https://github.com/cloud-bulldozer/orion.git /build/orion && \
    pip3 install --no-cache-dir --upgrade pip setuptools wheel && \
    pip3 install --no-cache-dir -r /build/orion/requirements.txt && \
    pip3 install --no-cache-dir /build/orion && \
    orion --version

############################
# Stage 5: Runtime image   #
############################
FROM base AS runtime

LABEL org.opencontainers.image.title="zathras-orion-eval" \
      org.opencontainers.image.description="Fedora 44 image with OpenSearch + Orion for throughput/latency regression checks (>5%)" \
      org.opencontainers.image.source="https://github.com/cloud-bulldozer/orion"

ENV OPENSEARCH_HOME=/opt/opensearch \
    JAVA_HOME=/opt/opensearch/jdk \
    PATH="/opt/opensearch/jdk/bin:/usr/local/bin:${PATH}" \
    DISABLE_SECURITY_PLUGIN=true \
    DISABLE_INSTALL_DEMO_CONFIG=true \
    OPENSEARCH_JAVA_OPTS="-Xms512m -Xmx512m" \
    ES_SERVER=http://127.0.0.1:9200 \
    ES_METADATA_INDEX=zathras-results \
    ES_BENCHMARK_INDEX=zathras-results \
    ORION_CONFIG=/opt/zathras-orion-eval/config/coremark-regression.yaml \
    ORION_OUTPUT=/opt/zathras-orion-eval/output/regression-report.json \
    LOOKBACK=30d \
    TEST_VERSION=v1.01

# OpenSearch user + binaries from the opensearch stage
RUN groupadd -g 1000 opensearch && \
    useradd -u 1000 -g 1000 -d "${OPENSEARCH_HOME}" -s /sbin/nologin opensearch

COPY --from=opensearch --chown=1000:1000 /opt/opensearch /opt/opensearch

# Orion + Python deps from the orion stage
COPY --from=orion /usr/local /usr/local

# Evaluation assets
COPY config /opt/zathras-orion-eval/config
COPY data /opt/zathras-orion-eval/data
COPY scripts /opt/zathras-orion-eval/scripts

RUN chmod +x /opt/zathras-orion-eval/scripts/*.sh /opt/zathras-orion-eval/scripts/*.py && \
    mkdir -p /opt/zathras-orion-eval/output && \
    chown -R opensearch:opensearch /opt/opensearch/data /opt/opensearch/logs /opt/zathras-orion-eval/output

WORKDIR /opt/zathras-orion-eval

EXPOSE 9200 9600

ENTRYPOINT ["/opt/zathras-orion-eval/scripts/docker-entrypoint.sh"]
