# ─── Stage 1: build the headroom-ai wheel in manylinux ─────────────────────
# Building from source inside `node:22-bookworm` produced a `_core.so` that
# referenced `__isoc23_strtoll` (a glibc 2.38+ symbol). The runtime in the
# same image couldn't resolve it at import time because something in the
# build chain — gcc version, libc6-dev backport, or cc-rs compiling
# transitive C deps against newer headers — emitted C23-era symbols that
# the runtime libc.so.6 doesn't have. Building inside the manylinux_2_28
# container (AlmaLinux 8, glibc 2.28 baseline) guarantees the wheel works
# on any glibc 2.28+ runtime, including bookworm.
FROM quay.io/pypa/manylinux_2_28_x86_64 AS builder

# No OpenSSL system deps required. As of the rustls-everywhere refactor,
# all HTTP+TLS in our Rust crates goes through `rustls`. fastembed's
# `hf-hub-rustls-tls` + `ort-download-binaries-rustls-tls` features
# replace the default native-tls path; `cargo tree -p headroom-py -i
# openssl-sys` returns "not found", confirming this Dockerfile no
# longer needs `openssl-devel`, `pkgconfig`, or any perl modules
# (Time::Piece, IPC::Cmd) for the openssl-src vendored Configure
# script. The historical `yum install openssl-devel pkgconfig
# perl-IPC-Cmd` line is intentionally absent.

# Install rust toolchain into the manylinux container. Match the
# rust-toolchain.toml at repo root (1.95.0 + rustfmt + clippy) so cargo
# doesn't try to mutate the toolchain on first invocation.
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:/opt/python/cp311-cp311/bin:${PATH}
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --no-modify-path --profile minimal -c rustfmt -c clippy --default-toolchain 1.95.0

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY crates/ crates/
COPY headroom/ headroom/

# Build the wheel with maturin. PYO3_USE_ABI3_FORWARD_COMPATIBILITY allows
# building against Python 3.14+ until we bump pyo3 past 0.22.
ENV PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
# Build for Python 3.11 — aider-chat==0.86.2 requires Python <3.12, and
# trixie's default python3.13 is too new for aider. The manylinux image
# has python interpreters for every version at /opt/python/cpXY-cpXY/.
# Stage 2 uses `python:3.11-slim` (now trixie-based, glibc 2.41).
RUN /opt/python/cp311-cp311/bin/pip install 'maturin>=1.5,<2.0' && \
    /opt/python/cp311-cp311/bin/maturin build --release --out /dist --interpreter python3.11

# ─── Stage 2: python+node runtime ───────────────────────────────────────────
# Base on `python:3.11-slim` (now trixie, glibc 2.41) instead of
# `node:22-bookworm` (glibc 2.36): the wheel's `_core.so` references
# three glibc 2.38+ C23 wrappers (`__isoc23_strtol{,l,ul}`) that one of
# our transitive C/C++ deps emits during cc-rs compilation even when
# built inside manylinux_2_28 (probably libstdc++'s `<cstdlib>` resolving
# `std::strtoll` to the C23 variant when the toolchain has newer
# headers). Bookworm's libc.so.6 doesn't export these wrappers, so
# `import headroom._core` fails at runtime. Trixie's glibc 2.41 does.
#
# We need Python 3.11 here (aider-chat==0.86.2 requires Python <3.12).
# Trixie's default `python3` is 3.13 — too new for aider. python:3.11-slim
# gives us Python 3.11 + trixie glibc + apt access for installing Node.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    AIDER_CHAT_VERSION=0.86.2 \
    OPENCODE_VERSION=1.17.8 \
    PATH="/opt/wrap-tools/node_modules/.bin:/opt/headroom-venv/bin:/opt/aider-venv/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Pinned OpenClaw 2026.10.7 requires Node >=24.22.1 <25 (or >=26.12.1).
# Install the Node 24 LTS line alongside Python 3.11. Also install git for packages
# that clone at install time.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_24.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# CRA-1534: use the same qualified distribution globally and inside wrap-tools.
# Verify public artifact bytes before installing; no user runtime is changed.
COPY e2e/wrap/tools/npm-security/artifact.json /tmp/cra1534-npm-artifact.json
COPY e2e/wrap/tools/npm-security/package-lock.json /tmp/cra1534-npm-lock.json
COPY scripts/test_security_npm.mjs /tmp/cra1534-test-security-npm.mjs
RUN python - <<'PY'
import base64
import hashlib
import json
from pathlib import Path
import urllib.request
metadata = json.loads(Path('/tmp/cra1534-npm-artifact.json').read_text())
url = 'https://github.com/crasiak/headroom/releases/download/npm-security-cra-1534-1/' + metadata['filename']
with urllib.request.urlopen(url, timeout=60) as response:
    payload = response.read(32 * 1024 * 1024 + 1)
if len(payload) > 32 * 1024 * 1024:
    raise SystemExit('npm source artifact exceeds size budget')
if hashlib.sha256(payload).hexdigest() != metadata['sha256']:
    raise SystemExit('npm source artifact SHA256 mismatch')
if 'sha512-' + base64.b64encode(hashlib.sha512(payload).digest()).decode() != metadata['integrity']:
    raise SystemExit('npm source artifact SRI mismatch')
with Path('/tmp/cra1534-npm.tgz').open('xb') as output:
    output.write(payload)
PY
RUN npm install -g --ignore-scripts --no-fund --no-audit /tmp/cra1534-npm.tgz && \
    SECURITY_NPM_PACKAGE="$(npm root -g)/npm" \
    SECURITY_NPM_LOCK=/tmp/cra1534-npm-lock.json \
    node --test /tmp/cra1534-test-security-npm.mjs

# Freeze transitive CLI dependencies as well as the top-level versions.
# Global npm install re-resolves ranges and can select partially published
# dependency releases; npm ci consumes the reviewed lockfile instead.
COPY e2e/wrap/tools/package.json e2e/wrap/tools/package-lock.json /opt/wrap-tools/
# npm12 permits only the explicitly declared root artifact URL, never all remote
# transitive URLs. Keep normal reviewed image lifecycle behavior; do not bypass
# npm's script policy. Verify both actual npm copies rather than only the lock.
RUN npm ci --prefix /opt/wrap-tools --allow-remote=root --no-fund --no-audit && \
    SECURITY_NPM_PACKAGE=/opt/wrap-tools/node_modules/npm \
    SECURITY_NPM_LOCK=/tmp/cra1534-npm-lock.json \
    node --test /tmp/cra1534-test-security-npm.mjs

WORKDIR /workspace

# Bring in the prebuilt manylinux wheel from stage 1.
COPY --from=builder /dist/*.whl /tmp/wheels/

# `sdk/typescript` and `plugins/openclaw` are wired into the wrap-e2e
# harness (e2e/wrap/run.py invokes `npx openclaw`). DO NOT copy
# `headroom/` or `pyproject.toml` from the workspace — they would shadow
# the installed wheel's `headroom/` package via cwd and Python would
# import the source-only `headroom/` (which has no `_core.so`),
# triggering the same `ModuleNotFoundError` the wheel install fixes.
COPY sdk/typescript ./sdk/typescript
COPY plugins/openclaw ./plugins/openclaw

# Install the prebuilt manylinux wheel with [proxy] extras. Pip resolves
# extras from the wheel's metadata and pulls deps from public PyPI. The
# wheel's `_core.so` was compiled against glibc 2.28, so it loads cleanly
# against bookworm's glibc 2.36 at runtime.
RUN python -m venv /opt/headroom-venv && \
    /opt/headroom-venv/bin/python -m pip install --upgrade pip && \
    /opt/headroom-venv/bin/python -m pip install "$(ls /tmp/wheels/headroom_ai-*.whl)[proxy]" && \
    /opt/headroom-venv/bin/python -c "from headroom._core import DiffCompressor; print('headroom._core OK')" && \
    python -m venv /opt/aider-venv && \
    /opt/aider-venv/bin/python -m pip install --upgrade pip && \
    /opt/aider-venv/bin/python -m pip install "aider-chat==${AIDER_CHAT_VERSION}" && \
    curl -fsSL https://opencode.ai/install | VERSION="${OPENCODE_VERSION}" bash

# The opencode installer adds its bin dir to .bashrc. Non-login shells
# (including CMD, docker run, and e2e shims spawned by subprocess) won't
# source .bashrc, so add the dir to PATH explicitly. The e2e shim shadows
# opencode anyway, but this guarantees `which opencode` works for
# diagnostics.
ENV PATH="/root/.opencode/bin:${PATH}"

COPY e2e/wrap ./e2e/wrap

CMD ["python", "e2e/wrap/run.py"]
