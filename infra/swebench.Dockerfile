# The official SWE-bench harness, meant to run as a Linux process.
#
# Its evaluation scripts are shell: on Windows every file it writes picks up CRLF endings, the
# container's bash then reads the carriage return as part of each test selector and path, and
# every instance fails as "missing_module" even when the patch applied cleanly. Running the
# harness in a container and letting it drive Docker over the socket avoids that entirely.
#
# The Docker CLI is installed on purpose: the harness shells out to it when it tears its
# evaluation containers down, and a missing binary there leaves containers running.
FROM python:3.12-slim
ARG DOCKER_CLI_VERSION=27.5.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /var/lib/apt/lists/* \
    && apt-get purge -y curl \
    && apt-get autoremove -y
RUN pip install --no-cache-dir swebench==5.0.2
WORKDIR /work
ENV PYTHONUTF8=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "swebench.harness.run_evaluation"]
