FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1

CMD ["python", "app.py", "serve", "--port", "5001"]
