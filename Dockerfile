FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
COPY app /app/
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /uploads

EXPOSE 8080

CMD ["python", "/app/main.py"]