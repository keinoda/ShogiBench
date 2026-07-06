# ShogiBench coordination server (Django UI + API + SQLite).
# Build from the repository root:
#   docker build -t shogibench -f Deploy/server/Dockerfile .

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["/app/Deploy/server/entrypoint.sh"]
