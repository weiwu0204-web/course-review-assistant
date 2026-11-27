ARG PYTHON_BASE_IMAGE=python:3.13-slim
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv/reviewer
COPY requirements.txt requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements-service.txt && mkdir -p /home/service && chown 10001:10001 /home/service
ENV HOME=/home/service
COPY app/ app/
COPY migrations/ migrations/
COPY scripts/service_admin.py scripts/service_admin.py
USER 10001:10001
CMD ["python", "-m", "uvicorn", "app.service_main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
