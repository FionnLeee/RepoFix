FROM python:3.12-slim
RUN pip install --no-cache-dir uv==0.12.6
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY vendor/mini-swe-agent vendor/mini-swe-agent
RUN uv sync --frozen --no-dev
COPY services/agent-worker services/agent-worker
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/services/agent-worker MSWEA_SILENT_STARTUP=1 MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT=2
CMD ["python", "-m", "repopilot.worker"]
