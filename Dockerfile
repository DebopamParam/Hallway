FROM python:3.13-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV HALLWAY_DB=/data/hallway.db
VOLUME /data
EXPOSE 8787
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8787/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787"]
