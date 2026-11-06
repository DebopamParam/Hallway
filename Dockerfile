# Hallway: the attendee app, the MCP endpoint and the privacy guard.
# The base image is pinned by exact tag and digest; Dependabot bumps both.
FROM python:3.13.18-slim-trixie@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc

# No .pyc files at runtime (the root filesystem is read-only in compose), logs
# straight to `docker logs`, and no pip cache or version check in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# An unprivileged user with a fixed uid, so the data volume's owner is stable
# across rebuilds. /data is created owned by it: a new named volume copies that.
RUN groupadd --system --gid 10001 hallway \
 && useradd --system --uid 10001 --gid hallway --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin hallway \
 && install -d -o hallway -g hallway -m 0750 /data

WORKDIR /srv
# requirements.txt is a lock with hashes (made from requirements.in by pip-compile).
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
# Bytecode made once here, as root, since nothing can write it at runtime.
RUN python -m compileall -q app

ENV HALLWAY_DB=/data/hallway.db
# Behind a Cloudflare Tunnel (cloudflared -> http://hallway:8787) TLS ends at
# Cloudflare. Trust X-Forwarded-Proto/-For only from private addresses (the
# tunnel container, or the Docker gateway for a host cloudflared), so the
# scheme is https and the peer is right. The visitor's IP is CF-Connecting-IP
# (main.client_ip). Override with FORWARDED_ALLOW_IPS in .env if needed.
ENV FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16

USER hallway:hallway
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers"]
