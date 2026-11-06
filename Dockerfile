FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV HALLWAY_DB=/data/hallway.db
VOLUME /data
EXPOSE 8787
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8787/health')"
# Behind a Cloudflare Tunnel (cloudflared -> http://hallway:8787) TLS ends at
# Cloudflare. Trust X-Forwarded-Proto/-For only from private addresses (the
# tunnel container, or the Docker gateway for a host cloudflared), so the
# scheme is https and the peer is right. The visitor's IP is CF-Connecting-IP
# (main.client_ip). Override with FORWARDED_ALLOW_IPS in .env if needed.
ENV FORWARDED_ALLOW_IPS=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--proxy-headers"]
