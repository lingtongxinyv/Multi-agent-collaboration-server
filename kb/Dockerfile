FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web
COPY kb_config.json ./

RUN mkdir -p data

EXPOSE 8787

CMD ["python", "-m", "app.cli", "serve"]
