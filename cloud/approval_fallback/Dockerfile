# Build from the repository root so the reviewed relay GitHub App and OIDC
# boundaries are reused rather than forked:
#   docker build -f cloud/approval_fallback/Dockerfile .

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY cloud/approval_fallback/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p cloud/approval_fallback cloud/approval_relay && \
    touch cloud/__init__.py cloud/approval_fallback/__init__.py \
    cloud/approval_relay/__init__.py

COPY cloud/approval_fallback/main.py ./cloud/approval_fallback/
COPY cloud/approval_relay/auth.py \
     cloud/approval_relay/github_dispatch.py \
     cloud/approval_relay/github_app_dispatch.py \
     ./cloud/approval_relay/

RUN useradd --create-home --uid 10001 fallback
USER fallback

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "45", "cloud.approval_fallback.main:create_app_from_env()"]
