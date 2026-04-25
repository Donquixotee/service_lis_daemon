FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY config/ config/
COPY src/ src/

# Run as non-root
RUN useradd -m -s /bin/bash lis && chown -R lis:lis /app
USER lis

CMD ["python", "-m", "src.main"]
