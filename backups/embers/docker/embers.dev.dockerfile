# Embers Dev Dockerfile - Debug build matching `cargo make run`
#
# Differences from embers.dockerfile (production):
#   - Debug profile (no --release): faster compile, debug assertions, no LTO
#   - No cross-compilation: builds for the host platform only
#   - Log level defaults to info,embers=trace (matching Makefile.toml)
#   - RUST_BACKTRACE=full for better error diagnostics

FROM rust:1.92-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update && \
    apt-get install -y pkg-config protobuf-compiler clang libclang-dev && \
    rm -rf /var/lib/apt/lists/*

COPY packages/firefly-client-macros firefly-client-macros
COPY packages/firefly-client firefly-client
COPY packages/embers embers

WORKDIR /app/embers

RUN cargo build --bin embers && \
    mv target/debug/embers /app/embers-debug

FROM debian:bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/embers-debug ./embers

EXPOSE 3000

ENV EMBERS__PORT="3000"
ENV EMBERS__ADDRESS="::"
ENV EMBERS__LOG_LEVEL="info,embers=trace"
ENV RUST_BACKTRACE="full"

STOPSIGNAL SIGINT
ENTRYPOINT ["/app/embers"]
