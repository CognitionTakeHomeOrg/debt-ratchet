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

WORKDIR /app
COPY detector/ /app/detector/
COPY orchestrator/ /app/orchestrator/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/detector:/app/orchestrator \
    MYPY_GATE=/opt/mypy-gate/bin/mypy

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD curl -fsS http://localhost:8099/health || exit 1

CMD ["python3", "/app/orchestrator/webhook.py"]
