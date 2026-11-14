FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
RUN pip install --no-cache-dir numpy==2.5.4 ai-edge-litert==2.3.0
COPY . /app
ENTRYPOINT ["python", "-m", "emulators.run"]
