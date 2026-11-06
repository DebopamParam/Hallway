# Day pass worker: runs organiser-paid agents through Hallway's MCP tools.
# The base image is pinned by exact tag and digest; Dependabot bumps both.
FROM python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Unprivileged, no home directory: anything temporary goes to /tmp (a tmpfs in compose).
RUN groupadd --system --gid 10002 daypass \
 && useradd --system --uid 10002 --gid daypass --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin daypass

WORKDIR /srv
# requirements.txt is a lock with hashes (made from requirements.in by pip-compile).
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY worker.py .
RUN python -m compileall -q worker.py

ENV HOME=/tmp
USER daypass:daypass
CMD ["python", "worker.py"]
