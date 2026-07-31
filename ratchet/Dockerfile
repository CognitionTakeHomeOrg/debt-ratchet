# The orchestrator image.
#
# Node and Python both present because the gates are polyglot: oxlint is a Rust
# binary shipped through npm, mypy is Python, and zizmor is Python. A verifier
# that cannot run all three cannot check its own claims.
#
# Node's version is pinned from the repo's own .nvmrc rather than chosen here.
# Reproducing CI exactly is the entire value of this container -- a baseline
# measured with a different toolchain than the one CI uses is not a baseline.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git curl jq ca-certificates gnupg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Trap 1, encoded.
#
# Pre-commit runs mypy in an isolated environment containing mypy and ten stub
# packages and nothing else. In that environment Superset's master is clean. With
# Superset's real dependencies installed, the same command on the same commit
# reports 1,502 errors. Both numbers are real; only the first one is the gate.
# Installing anything else into this venv silently changes the answer.
RUN python3 -m venv /opt/mypy-gate \
    && /opt/mypy-gate/bin/pip install --no-cache-dir \
         "mypy==1.15.0" \
         types-cachetools types-simplejson types-python-dateutil types-requests \
         types-pytz types-croniter types-PyYAML types-setuptools types-paramiko \
         types-Markdown

# The repository under measurement is bind-mounted from the host, so its files
# are owned by the host user while this container runs as root. Git treats that
# as "dubious ownership" and refuses every command -- which would break the
# detector's clean-tree assertion before any gate ran. The mount is read-only
# and the container measures rather than commits, so trusting it is safe here.
RUN git config --global --add safe.directory '*'

WORKDIR /app
# Mirror the repository layout under /app rather than flattening it.
#
# The modules locate their siblings with `Path(__file__).resolve().parents[2]`,
# which is the repo root when the tree is `<root>/ratchet/orchestrator/x.py`.
# Copying to `/app/orchestrator/` instead made that resolve to `/`, so the
# orchestrator looked for `/ratchet/state.db` and `/baseline` while compose
# mounted them at `/app/...`: sqlite3.OperationalError, and a dashboard reading
# an empty ledger. Keeping the `ratchet/` level costs nothing and makes the
# in-container paths identical to the ones used outside it.
COPY detector/ /app/ratchet/detector/
COPY orchestrator/ /app/ratchet/orchestrator/
COPY fixtures/ /app/ratchet/fixtures/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/ratchet/detector:/app/ratchet/orchestrator \
    MYPY_GATE=/opt/mypy-gate/bin/mypy \
    STATE_DB=/app/state/state.db

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD curl -fsS http://localhost:8099/health || exit 1

# The node base image sets an ENTRYPOINT that execs node, so any argument passed
# to `docker compose run` is handed to node rather than to the service -- which
# fails with "node: bad option". Clearing it makes CMD authoritative.
#
# Note that this also means `compose run simulate --fast` *replaces* CMD instead
# of appending to it; pass FAST=1 in the environment instead.
ENTRYPOINT []
CMD ["python3", "/app/ratchet/orchestrator/webhook.py"]
