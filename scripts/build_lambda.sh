#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${PROJECT_DIR}/build/lambda"

mkdir -p "${BUILD_DIR}"
find "${BUILD_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

python3 -m pip install \
  --requirement "${PROJECT_DIR}/requirements-lambda.txt" \
  --target "${BUILD_DIR}" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.12 \
  --abi cp312 \
  --only-binary=:all: \
  --upgrade

cp -R "${PROJECT_DIR}/src/career_agent" "${BUILD_DIR}/career_agent"
mkdir -p "${BUILD_DIR}/data"
cp "${PROJECT_DIR}/data/candidate_profile.example.json" "${BUILD_DIR}/data/candidate_profile.example.json"
cp "${PROJECT_DIR}/data/source_registry.json" "${BUILD_DIR}/data/source_registry.json"
cp "${PROJECT_DIR}/data/employer_universe.json" "${BUILD_DIR}/data/employer_universe.json"
cp "${PROJECT_DIR}/data/employer_source_routes.json" "${BUILD_DIR}/data/employer_source_routes.json"

# Application and matching workers need the exact frozen profile/index pair.
# Pass RICH_MATCHING_BUILD_DIR only for a verified rich release; do not silently
# substitute a stale local index when assembling a normal legacy package.
if [[ -n "${RICH_MATCHING_BUILD_DIR:-}" ]]; then
  test -f "${RICH_MATCHING_BUILD_DIR}/profile.json"
  test -f "${RICH_MATCHING_BUILD_DIR}/index.json"
  cp -R "${RICH_MATCHING_BUILD_DIR}" "${BUILD_DIR}/data/rich_matching"
fi

python3 -m compileall -q "${BUILD_DIR}/career_agent"
# Full builds synchronize the API artifact as well. Scoped API releases use the
# pinned live-package staging helper instead, leaving build/lambda unchanged.
API_BUILD_DIR="${PROJECT_DIR}/build/api-lambda"
mkdir -p "${API_BUILD_DIR}"
rsync -a --delete "${BUILD_DIR}/" "${API_BUILD_DIR}/"
echo "Lambda bundle staged at ${BUILD_DIR}"
