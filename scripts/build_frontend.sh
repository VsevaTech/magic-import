#!/usr/bin/env sh
# Rebuild the generated front-end assets from pinned npm packages:
#   app/static/app.css              (Tailwind v4 CLI, from app/static/src/input.css)
#   app/static/vendor/alpine.min.js (Alpine.js CDN build, vendored - no runtime CDN calls)
# Both files are committed; run this after editing templates/CSS or bumping versions.
set -e
cd "$(dirname "$0")/.."

TAILWIND_VERSION="${TAILWIND_VERSION:-4.3.3}"
ALPINE_VERSION="${ALPINE_VERSION:-3.17.4}"

WORK="${FRONTEND_BUILD_DIR:-.frontend-build}"
mkdir -p "$WORK"
(
  cd "$WORK"
  if [ ! -f package.json ]; then
    printf '{"name":"magic-import-frontend","private":true}\n' > package.json
  fi
  npm install --no-audit --no-fund --silent \
    "tailwindcss@$TAILWIND_VERSION" "@tailwindcss/cli@$TAILWIND_VERSION" "alpinejs@$ALPINE_VERSION"
)

mkdir -p app/static/vendor
cp "$WORK/node_modules/alpinejs/dist/cdn.min.js" app/static/vendor/alpine.min.js

# Tailwind resolves `@import "tailwindcss"` by walking up from the input file, so the
# packages must be reachable from the repo root. Link them there for the duration of the build.
LINKED=0
if [ ! -e node_modules ]; then
  ln -s "$(cd "$WORK" && pwd)/node_modules" node_modules
  LINKED=1
fi
rc=0
"$WORK/node_modules/.bin/tailwindcss" -i app/static/src/input.css -o app/static/app.css --minify || rc=$?
if [ "$LINKED" = 1 ]; then rm node_modules; fi
[ "$rc" = 0 ] || exit "$rc"
echo "built app/static/app.css and app/static/vendor/alpine.min.js"
