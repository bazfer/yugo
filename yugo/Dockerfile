FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY yugo/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime code + import-time reads. `history` is imported by bot.py at boot;
# `IDENTITY.md` is the load_persona fallback. Bundling `.env.example` is
# harmless and gives operators a `docker run --rm <image> cat .env.example`
# reference. Tests are deliberately NOT copied — CI runs pytest against the
# source tree, and the runtime image doesn't need pytest.
COPY yugo/bot.py .
COPY yugo/coordinator.py .
COPY yugo/coordinator_main.py .
COPY yugo/coordinator_state.py .
COPY yugo/version.py .
COPY yugo/entrypoint.sh .
COPY yugo/history.py .
COPY yugo/fleet_bus.py .
COPY yugo/tools.py .
COPY yugo/filesystem_tools.py .
COPY yugo/http_tools.py .
COPY yugo/IDENTITY.md .
COPY yugo/.env.example .

# The contract now lives in this repository. Copy the authoritative files from
# the root build context so schema/vector changes and their Python adaptations
# can land atomically in one commit.
COPY schema/envelope.v1.schema.json ./schema/envelope.v1.schema.json
COPY conformance/envelope.v1.vectors.json ./conformance/envelope.v1.vectors.json

ENTRYPOINT ["/app/entrypoint.sh"]
