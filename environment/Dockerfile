FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

ENV DEBIAN_FRONTEND=noninteractive

# Least-volatile layers first: fix the working directory before the apt and
# dependency layers so a change to those never invalidates the WORKDIR layer.
WORKDIR /app

# The only step in this build that reaches the network, and a single transient
# mirror failure was enough to fail an image build and void a grading trial, so
# the fetch is retried rather than attempted once.
RUN set -eu; \
    for attempt in 1 2 3; do \
        if apt-get update \
            && apt-get install -y --no-install-recommends tmux asciinema; then \
            rm -rf /var/lib/apt/lists/*; \
            exit 0; \
        fi; \
        echo "apt attempt ${attempt} failed, retrying" >&2; \
        sleep 5; \
    done; \
    exit 1

# /app/data/development_triangle.json ships with only its first development
# column, exactly as the abandoned year-end rebuild left it; the incremental
# claim movements and the period calendar beside it are what the triangle has to
# be aggregated from.
COPY app/ /app/
RUN cp /app/workflow/develop_reserves.py /app/workflow/.develop_reserves.original \
    && chmod a-w /app/workflow/.develop_reserves.original
