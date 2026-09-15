FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /srv/app/
COPY members.example.json /srv/members.example.json

# SQLite lives on a mounted volume so the DB survives rebuilds.
VOLUME ["/data"]

CMD ["python", "-m", "app.main"]
