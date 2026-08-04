FROM python:3.11-slim

WORKDIR /app

# Set environment variables for non-buffered logging and standard port
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# Install dependencies
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Expose standard Cloud Run port
EXPOSE 8080

# Run the uvicorn FastAPI server
CMD ["python", "-m", "src.interview_agent.server"]
