#!/bin/sh

if [[ -z "$(ls -A /musicbot/config 2>/dev/null)" ]]; then
    cp -r /musicbot/sample_config/* /musicbot/config
fi

exec python3 run.py $@
