#!/usr/bin/env bash

set -euo pipefail

DEFAULT_VERSION="v0.1.0"
IMAGE_NAME="valentemath/actual-clerk"

usage() {
    echo "Usage: $0 [version]"
    echo "Builds and pushes $IMAGE_NAME using the supplied version."
    echo "Default version: $DEFAULT_VERSION"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

if [[ "$#" -gt 1 ]]; then
    usage >&2
    exit 1
fi

VERSION="${1:-$DEFAULT_VERSION}"

if [[ ! "$VERSION" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
    echo "Invalid Docker image version tag: $VERSION" >&2
    exit 1
fi

APP_VERSION="${VERSION#v}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LATEST_IMAGE="$IMAGE_NAME:latest"
VERSION_IMAGE="$IMAGE_NAME:$VERSION"

echo "Building $LATEST_IMAGE with version $VERSION..."
sudo docker build \
    --build-arg "ACTUAL_CLERK_VERSION=$APP_VERSION" \
    --tag "$LATEST_IMAGE" \
    "$SCRIPT_DIR"

if [[ "$VERSION_IMAGE" != "$LATEST_IMAGE" ]]; then
    echo "Tagging $VERSION_IMAGE..."
    sudo docker tag "$LATEST_IMAGE" "$VERSION_IMAGE"
fi

echo "Pushing images to registry..."
sudo docker push "$LATEST_IMAGE"

if [[ "$VERSION_IMAGE" != "$LATEST_IMAGE" ]]; then
    sudo docker push "$VERSION_IMAGE"
fi

echo "Successfully built and pushed:"
echo "  - $LATEST_IMAGE"

if [[ "$VERSION_IMAGE" != "$LATEST_IMAGE" ]]; then
    echo "  - $VERSION_IMAGE"
fi
