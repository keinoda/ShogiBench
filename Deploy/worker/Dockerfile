# Optional prebuilt worker image, usable as a vast.ai template image.
# Build & push:
#   docker build -t <user>/shogibench-worker -f Deploy/worker/Dockerfile Deploy/worker
#   docker push <user>/shogibench-worker
#
# Run with:
#   -e OPENBENCH_SERVER=... -e OPENBENCH_USERNAME=... -e OPENBENCH_PASSWORD=...

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git make clang g++ python3 python3-pip procps \
    && rm -rf /var/lib/apt/lists/*

# Rust toolchain for building the shogitest match runner
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

COPY setup_worker.sh /usr/local/bin/setup_worker.sh
RUN chmod +x /usr/local/bin/setup_worker.sh

CMD ["/usr/local/bin/setup_worker.sh"]
