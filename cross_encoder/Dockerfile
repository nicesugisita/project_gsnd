FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages \
    torch --index-url https://download.pytorch.org/whl/cu128 && \
    pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY main.py .

EXPOSE 7200

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7200"]
