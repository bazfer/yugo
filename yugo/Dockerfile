FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime code + import-time reads. `history` is imported by bot.py at boot;
# `IDENTITY.md` is the load_persona fallback. Bundling `.env.example` is
# harmless and gives operators a `docker run --rm <image> cat .env.example`
# reference. Tests are deliberately NOT copied — CI runs pytest against the
# source tree, and the runtime image doesn't need pytest.
COPY bot.py .
COPY coordinator.py .
COPY coordinator_main.py .
COPY coordinator_state.py .
COPY version.py .
COPY entrypoint.sh .
COPY history.py .
COPY fleet_bus.py .
COPY tools.py .
COPY filesystem_tools.py .
COPY http_tools.py .
COPY IDENTITY.md .
COPY .env.example .

# v0.3e — vendored fleet-bus envelope schema, pinned to an upstream commit and
# double-checked at build time.
#
# Local hash-check: catches accidental drift between the schema file and the
# recorded SHA in the same commit (a merge that touched one but not the other).
#
# Upstream hash-check: catches "vendored copy no longer matches the upstream
# commit it claims to pin" (someone bumped VERSION without re-copying the
# schema, or edited the schema locally without bumping VERSION). Curl to raw
# github with the pinned SHA — build fails loud if network unavailable OR if
# the pinned tree no longer serves this content OR if content differs.
COPY vendored ./vendored
# Reproducibility: FLEET_BUS_VERSION MUST be exactly 40 hex chars (a full
# commit SHA). Mutable refs (branch names, short SHAs, tags that can be
# force-pushed) defeat the reproducibility this slice claims — under a
# mutable ref, the upstream cmp still passes today AND then silently drifts
# when upstream moves, without a corresponding local re-vendor. Reject
# anything that isn't a full lowercase-hex SHA before it reaches the URL.
RUN version="$(cat vendored/FLEET_BUS_VERSION)" && \
    printf '%s' "$version" | grep -Eq '^[0-9a-f]{40}$' \
      || { echo "FLEET_BUS_VERSION must be a 40-char lowercase-hex commit SHA, got: '$version'"; exit 1; } && \
    expected="$(cat vendored/FLEET_BUS_SCHEMA_SHA256)" && \
    echo "$expected  vendored/envelope.v1.schema.json" | sha256sum -c - && \
    vectors_expected="$(cat vendored/FLEET_BUS_VECTORS_SHA256)" && \
    echo "$vectors_expected  vendored/envelope.v1.vectors.json" | sha256sum -c - && \
    curl -fsSL "https://raw.githubusercontent.com/bazfer/fleet-bus/${version}/schema/envelope.v1.schema.json" \
      -o /tmp/envelope.v1.schema.json && \
    cmp vendored/envelope.v1.schema.json /tmp/envelope.v1.schema.json && \
    curl -fsSL "https://raw.githubusercontent.com/bazfer/fleet-bus/${version}/conformance/envelope.v1.vectors.json" \
      -o /tmp/envelope.v1.vectors.json && \
    cmp vendored/envelope.v1.vectors.json /tmp/envelope.v1.vectors.json

ENTRYPOINT ["/app/entrypoint.sh"]
