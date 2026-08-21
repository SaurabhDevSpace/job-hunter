FROM python:3.11-slim

# Install system dependencies for WeasyPrint and font support
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-dejavu-core \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir weasyprint pypdf

COPY . .

ENV PORT=8080
ENV JOBHUNTER_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["sh", "-c", "python -m jobhunter.cli serve --host 0.0.0.0 --port ${PORT:-8080}"]
