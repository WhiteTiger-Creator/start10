FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

ENV DEBIAN_FRONTEND=noninteractive

# Least-volatile layers first: fix the working directory before the apt and
# dependency layers so a change to those never invalidates the WORKDIR layer.
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tmux \
        asciinema \
    && rm -rf /var/lib/apt/lists/*

# /app/data/development_triangle.json ships with only its first development
# column, exactly as the abandoned year-end rebuild left it; the incremental
# claim movements and the period calendar beside it are what the triangle has to
# be aggregated from.
COPY app/ /app/
RUN cp /app/workflow/develop_reserves.py /app/workflow/.develop_reserves.original \
    && chmod a-w /app/workflow/.develop_reserves.original
