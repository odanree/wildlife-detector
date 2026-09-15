# Postgres 16 + pgvector, built on the SAME alpine base the stack already
# runs (postgres:16-alpine). Needed by the embedder service (project
# rat re-id Phase 2a) for the `vector(512)` column in alert_embeddings.
#
# Why build instead of `image: pgvector/pgvector:pg16`?
#   The official pgvector image is Debian/glibc. Our pg-data volume was
#   initdb'd under alpine/musl with datcollate=en_US.utf8. Swapping libc
#   changes the collation implementation → Postgres flags a collation
#   version mismatch and every text btree index (idx_alerts_species,
#   idx_alerts_snapshot, uniq_alerts_ts_species_snap, …) needs a REINDEX
#   before it's trustworthy. Compiling pgvector onto the alpine image is
#   ~90s of build and zero data-migration risk. Same PG major (16), same
#   on-disk format — the existing volume mounts unchanged.
#
# Pattern: **additive infrastructure change with zero data-plane
# migration** — extend the running image rather than swap its base.
#
# Rebuild: docker compose build postgres && docker compose up -d postgres
# (≈10s of DB unavailability; psycopg pools + the LISTEN loops reconnect).

FROM postgres:16-alpine

ARG PGVECTOR_VERSION=v0.8.1

# with_llvm=no: skip emitting LLVM bitcode for JIT inlining — avoids
# pulling clang/llvm (~400MB) into the build stage. pgvector's hot path
# is SIMD C, JIT inlining buys nothing measurable at our row counts.
# OPTFLAGS="" drops -march=native so the image is portable across CPUs.
RUN set -eux; \
    apk add --no-cache --virtual .pgvector-build git build-base; \
    git clone --branch "${PGVECTOR_VERSION}" --depth 1 \
        https://github.com/pgvector/pgvector.git /tmp/pgvector; \
    cd /tmp/pgvector; \
    make OPTFLAGS="" with_llvm=no; \
    make install with_llvm=no; \
    cd /; rm -rf /tmp/pgvector; \
    apk del .pgvector-build
