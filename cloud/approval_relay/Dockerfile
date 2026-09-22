# Build context is this directory (mirrors cloud/approval_receiver/'s own
# convention, not cloud/approval_dispatcher/'s repo-root context): this
# relay is genuinely independent and needs no files from scripts/ or
# elsewhere in the repository.
#
#   docker build -f cloud/approval_relay/Dockerfile cloud/approval_relay

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p cloud/approval_relay && \
    touch cloud/__init__.py cloud/approval_relay/__init__.py
COPY main.py router.py ledger.py storage.py github_dispatch.py gmail_reader.py auth.py watch_renewal.py ./cloud/approval_relay/

RUN useradd --create-home --uid 10001 relay
USER relay

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "60", "cloud.approval_relay.main:create_app_from_env()"]
