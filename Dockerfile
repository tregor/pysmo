FROM python:3.9-slim
LABEL authors="tregor"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    wget \
    htop \
    procps \
    ca-certificates && \
    apt-get clean

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]