FROM python:3.11-alpine

ARG GIT_REPO=https://github.com/maxss280/MusicBot.git
ARG GIT_BRANCH=

# Install git for cloning
RUN apk update && apk add --no-cache git

# Clone the repository
WORKDIR /musicbot
RUN if [ -z "${GIT_BRANCH}" ]; then \
      git clone ${GIT_REPO} .; \
    else \
      git clone -b ${GIT_BRANCH} ${GIT_REPO} .; \
    fi

# Clean up git from build
RUN rm -rf .git

# Install build dependencies for pip (including python3-dev for PyNaCl)
RUN apk update && apk add --no-cache --virtual .build-deps \
  build-base \
  libffi-dev \
  libsodium-dev \
  python3-dev

# Install pip dependencies from cloned repo
RUN pip3 install --no-cache-dir -r requirements.txt

# Clean up build dependencies
RUN apk del .build-deps

# Install runtime dependencies
RUN apk update && apk add --no-cache \
  ca-certificates \
  ffmpeg \
  opus \
  libffi \
  libsodium

# Create sample_config directory with example files for docker-entrypoint
RUN mkdir -p /musicbot/sample_config/i18n && \
    cp /musicbot/config/example_*.ini /musicbot/sample_config/ && \
    cp /musicbot/config/example_aliases.json /musicbot/sample_config/ && \
    cp /musicbot/config/_autoplaylist.txt /musicbot/sample_config/ && \
    cp /musicbot/config/i18n/*.json /musicbot/sample_config/i18n/

# Create volumes for audio cache, config, data and logs
VOLUME ["/musicbot/audio_cache", "/musicbot/config", "/musicbot/data", "/musicbot/logs"]

ENV APP_ENV=docker

ENTRYPOINT ["/bin/sh", "docker-entrypoint.sh"]
