FROM python:3.11-alpine

ARG GIT_REPO=https://github.com/maxss280/MusicBot.git
ARG GIT_BRANCH=

# Clone the repository
WORKDIR /musicbot
RUN if [ -z "${GIT_BRANCH}" ]; then \
      git clone ${GIT_REPO} .; \
    else \
      git clone -b ${GIT_BRANCH} ${GIT_REPO} .; \
    fi

# Install build dependencies for pip
RUN apk update && apk add --no-cache --virtual .build-deps \
  build-base \
  libffi-dev \
  libsodium-dev

# Install pip dependencies from cloned repo
RUN pip3 install --no-cache-dir -r requirements.txt

# Clean up build dependencies
RUN apk del .build-deps

# Install runtime dependencies
RUN apk update && apk add --no-cache \
  ca-certificates \
  ffmpeg \
  opus-dev \
  libffi \
  libsodium

# Clean up git from build
RUN rm -rf .git

# Create volumes for audio cache, config, data and logs
VOLUME ["/musicbot/audio_cache", "/musicbot/config", "/musicbot/data", "/musicbot/logs"]

ENV APP_ENV=docker

ENTRYPOINT ["/bin/sh", "docker-entrypoint.sh"]
