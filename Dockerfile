FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app/project
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
CMD ["python3", "-m", "shop_bot"]
