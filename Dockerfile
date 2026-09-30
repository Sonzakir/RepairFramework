FROM docker:27-cli AS docker-cli

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        git \
        patch \
    && rm -rf /var/lib/apt/lists/*

# Runtime Python dependencies (mirrors [project].dependencies in pyproject.toml).
# The package itself is imported from the mounted source tree via
# PYTHONPATH=/workspace/src, but its third-party deps must be present in the image.
RUN pip install --no-cache-dir \
        "openai>=1.0.0" \
        "python-dotenv>=1.0.0"

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /workspace

ENV BUGSINPY_HOME=/workspace/.tools/bugsinpy
ENV PYTHONPATH=/workspace/src

COPY docker-entrypoint.sh /usr/local/bin/apr-entrypoint

RUN chmod +x /usr/local/bin/apr-entrypoint

ENTRYPOINT ["/usr/local/bin/apr-entrypoint"]

CMD ["bash"]
