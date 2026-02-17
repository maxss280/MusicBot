FROM python:3.11-alpine

ARG GIT_REPO=https://github.com/maxss280/MusicBot.git
ARG GIT_BRANCH=

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install build dependencies for git and pip
RUN apk update && apk add --no-cache --virtual .build-deps \
  build-base \
  libffi-dev \
  libsodium-dev \
  git

# Install pip dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Clone the repository
WORKDIR /musicbot
RUN if [ -z "${GIT_BRANCH}" ]; then \
      git clone ${GIT_REPO} .; \
    else \
      git clone -b ${GIT_BRANCH} ${GIT_REPO} .; \
    fi

# Clean up git from build
RUN rm -rf .git

# Create volumes for audio cache, config, data and logs
VOLUME ["/musicbot/audio_cache", "/musicbot/config", "/musicbot/data", "/musicbot/logs"]

ENV APP_ENV=docker

ENTRYPOINT ["/bin/sh", "docker-entrypoint.sh"]
