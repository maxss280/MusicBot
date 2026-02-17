#!/bin/sh

# Check if sample_config exists (created by Dockerfile)
if [ -d "/musicbot/sample_config" ]; then
    # Copy example config files (options.ini, permissions.ini, aliases.json)
    # Only if they don't exist in config directory
    for file in options.ini permissions.ini aliases.json; do
        if [ ! -f "/musicbot/config/$file" ]; then
            cp -n "/musicbot/sample_config/$file" "/musicbot/config/"
        fi
    done

    # Always copy i18n files (they should always match the repo)
    if [ -d "/musicbot/sample_config/i18n" ]; then
        for f in /musicbot/sample_config/i18n/*.json; do
            if [ -f "$f" ]; then
                cp -n "$f" "/musicbot/config/i18n/"
            fi
        done
    fi

    # Copy other example files that don't have user modifications
    if [ -f "/musicbot/sample_config/_autoplaylist.txt" ]; then
        if [ ! -f "/musicbot/config/autoplaylist.txt" ]; then
            cp -n "/musicbot/sample_config/_autoplaylist.txt" "/musicbot/config/autoplaylist.txt"
        fi
    fi
fi

exec python3 run.py $@
