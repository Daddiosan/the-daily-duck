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

COPY main.py router.py ledger.py github_dispatch.py ./

RUN useradd --create-home --uid 10001 relay
USER relay

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "60", "main:create_app()"]
