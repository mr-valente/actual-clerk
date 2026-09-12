#!/usr/bin/env bash
# Build the Actual Clerk phone app as an installable APK, using Docker for
# the whole Android toolchain. The output lands in dist/android/.
#
# The first run creates a signing key in android/keystore/ (ignored by git).
# Keep it: Android only installs an update over an app signed with the same
# key, so losing the key means uninstalling the app before the next build.
set -euo pipefail

cd "$(dirname "$0")/.."
KEYSTORE_DIR=android/keystore
KEYSTORE="$KEYSTORE_DIR/clerk.jks"
PASSWORD_FILE="$KEYSTORE_DIR/password"
OUT_DIR=${OUT_DIR:-dist/android}
APP_VERSION=${APP_VERSION:-$(grep -m1 '^version' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')}
APP_VERSION_CODE=${APP_VERSION_CODE:-$(git rev-list --count HEAD 2>/dev/null || echo 1)}

mkdir -p "$KEYSTORE_DIR" "$OUT_DIR"

if [ ! -f "$PASSWORD_FILE" ]; then
    umask 077
    head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$PASSWORD_FILE"
    umask 022
fi
PASSWORD=$(cat "$PASSWORD_FILE")

if [ ! -f "$KEYSTORE" ]; then
    echo "Creating a signing key at $KEYSTORE (kept out of git; keep a copy)"
    docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$PWD/$KEYSTORE_DIR:/ks" eclipse-temurin:17-jdk-jammy \
        keytool -genkeypair -keystore /ks/clerk.jks -alias clerk -keyalg RSA -keysize 2048 \
            -validity 10000 -storepass "$PASSWORD" -keypass "$PASSWORD" \
            -dname "CN=Actual Clerk companion, O=Actual Clerk"
fi

echo "Building version $APP_VERSION ($APP_VERSION_CODE)"
docker build -f android/Dockerfile --target apk \
    --build-arg "KEYSTORE_PASSWORD=$PASSWORD" \
    --build-arg "APP_VERSION=$APP_VERSION" \
    --build-arg "APP_VERSION_CODE=$APP_VERSION_CODE" \
    --output "type=local,dest=$OUT_DIR" android

echo "APK: $OUT_DIR/actual-clerk-companion.apk"
