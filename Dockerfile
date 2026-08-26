# anki ships manylinux_2_35 wheels, so glibc >= 2.35 is required:
# bookworm has 2.36. Alpine/musl has no wheel at all and will not work.
FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.9.22 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /srv

# Dependencies come from uv.lock, the same file the dev venv uses, so the
# pinned anki version cannot drift between development and production (R1).
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

ENV PATH="/srv/.venv/bin:$PATH"

COPY app ./app
COPY scripts ./scripts

# uvicorn reads UVICORN_PORT itself, so the listen port is changeable at run
# time without a rebuild. The bind address stays 0.0.0.0: inside a container
# anything else is unreachable.
ENV UVICORN_PORT=8000
EXPOSE 8000

# One worker, always: the collection has exactly one writer (D2).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--workers", "1"]
