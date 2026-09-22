#!/usr/bin/env sh
# Rebuild app/static/app.css (and the vendored Alpine.js) from pinned npm packages.
# Kept for backwards compatibility - delegates to build_frontend.sh.
set -e
exec sh "$(dirname "$0")/build_frontend.sh"
