FROM python:3.12.13-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf

COPY docker/openjarvis-constraints.txt /tmp/openjarvis-constraints.txt

RUN python -m pip install --no-cache-dir --disable-pip-version-check \
        --constraint /tmp/openjarvis-constraints.txt \
        "OpenJarvis[server]==1.0.3" && \
    rm /tmp/openjarvis-constraints.txt && \
    groupadd --system --gid 10001 openjarvis && \
    useradd --system --uid 10001 --gid openjarvis \
        --create-home --home-dir /home/openjarvis openjarvis

ENV HOME=/home/openjarvis \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
EXPOSE 8011
ENTRYPOINT ["jarvis"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8011", "--engine", "ollama", "--model", "qwen3:0.6b", "--agent", "simple"]
