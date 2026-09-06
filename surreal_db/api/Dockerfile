FROM python:3.12-slim

WORKDIR /app

# Dependencies first, so editing application code does not reinstall the world.
COPY pyproject.toml ./
RUN pip install --no-cache-dir uv && \
    uv pip install --system --no-cache .

COPY cortex ./cortex

EXPOSE 8000
CMD ["uvicorn", "cortex.main:app", "--host", "0.0.0.0", "--port", "8000"]
