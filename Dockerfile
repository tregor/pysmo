FROM python:3.9-slim
LABEL authors="tregor"

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

CMD ["flask", "run", "--host=0.0.0.0", "--port=5000"]