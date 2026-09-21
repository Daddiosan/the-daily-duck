# Build context MUST be the repository root, not this directory:
#
#   docker build -f cloud/approval_dispatcher/Dockerfile .
#
# This differs from cloud/approval_receiver/Dockerfile's own convention
# (built with its own directory as context) because A2 needs
# scripts/approval_domain.py and scripts/a2_dispatch.py, which live
# outside cloud/approval_dispatcher/. Only those two files are copied from
# scripts/ -- not the rest of that directory, which contains unrelated
# scripts with heavier third-party dependencies A2 does not need.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY cloud/approval_dispatcher/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY cloud/approval_dispatcher/main.py cloud/approval_dispatcher/gmail_reader.py cloud/approval_dispatcher/storage.py ./
COPY scripts/approval_domain.py scripts/a2_dispatch.py ./scripts/

RUN useradd --create-home --uid 10001 dispatcher
USER dispatcher

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "60", "main:create_app_from_env()"]
