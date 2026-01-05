FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app /app/app
COPY wsgi.py /app/

RUN mkdir -p /uploads

EXPOSE 8080

CMD ["gunicorn", "-b", "0.0.0.0:8080", "wsgi:app"]