FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV SERVICE_MODULE=src.ui_service.server

# Install uv for deterministic, fast dependency installation
RUN pip install --no-cache-dir --upgrade pip uv

COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache --no-deps .

EXPOSE 8080

CMD sh -c "python -m $SERVICE_MODULE"
