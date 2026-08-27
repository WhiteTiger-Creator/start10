FROM public.ecr.aws/docker/library/python:3.13-slim-bookworm@sha256:01f42367a0a94ad4bc17111776fd66e3500c1d87c15bbd6055b7371d39c124fb

ENV DEBIAN_FRONTEND=noninteractive

# Least-volatile layers first: fix the working directory before the apt and
# dependency layers so a change to those never invalidates the WORKDIR layer.
WORKDIR /app

# The only step in this build that reaches the network, and a single transient
# mirror failure was enough to fail an image build and void a grading trial, so
# the fetch is retried rather than attempted once: Acquire::Retries retries each
# individual download, the loop retries the whole transaction after clearing the
# partial lists a failed update leaves behind, and the timeouts are explicit so a
# mirror that hangs rather than refuses cannot eat the build budget. curl, git
# and procps are the tools a trial harness assumes it can find; the slim base
# carries none of them.
RUN set -eu; \
    delay=5; \
    for attempt in 1 2 3 4 5 6; do \
        if apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 \
                -o Acquire::https::Timeout=30 update \
            && apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 \
                -o Acquire::https::Timeout=30 install -y --no-install-recommends \
                ca-certificates curl git procps tmux asciinema; then \
            rm -rf /var/lib/apt/lists/*; \
            exit 0; \
        fi; \
        echo "apt attempt ${attempt} failed, retrying in ${delay}s" >&2; \
        rm -rf /var/lib/apt/lists/*; \
        sleep "${delay}"; \
        if [ "${delay}" -lt 60 ]; then delay=$((delay * 2)); fi; \
    done; \
    exit 1

# /app/data/development_triangle.json ships EMPTY -- one bare object per line of
# business and not a cell in any of them, exactly as the abandoned year-end
# rebuild left it; the incremental claim movements and the period calendar
# beside it are what the triangle has to be aggregated from.
COPY app/ /app/
RUN cp /app/workflow/develop_reserves.py /app/workflow/.develop_reserves.original \
    && chmod a-w /app/workflow/.develop_reserves.original
