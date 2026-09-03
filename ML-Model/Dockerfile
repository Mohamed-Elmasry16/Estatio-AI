FROM python:3.11-slim

WORKDIR /app

# LightGBM needs libgomp at runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e .

COPY scripts/ scripts/
COPY models/ models/

EXPOSE 8000

CMD ["uvicorn", "price_predictor.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
