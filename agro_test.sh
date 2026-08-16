#!/usr/bin/env bash
#
# Run ODX's own unittest suite and judge it against a KNOWN-FAILURE list.
#
# Upstream ODX ships seven test files and its CI never runs any of them —
# `.github/workflows/test-build-prs.yml` only builds Docker images and a Windows
# installer. So the suite has been drifting untested. One test is red on arrival, and
# without a list of what is expected to be red there are only two options, both bad:
# ignore the suite entirely, or let a real regression hide behind a familiar failure.
#
# So this script asserts the failure set EXACTLY:
#
#   an UNEXPECTED failure           -> CI fails (a regression)
#   a KNOWN failure that now PASSES -> CI fails (delete it from the list)
#
# The second rule is the one that keeps this honest. A known-failure list that only
# ever grows becomes a place regressions go to hide; requiring it to shrink when
# upstream fixes something means the list stays a statement about reality.
#
# Usage:
#   ./agro_test.sh                 # run inside a container that already has the deps
#   ./agro_test.sh --docker        # pull the pinned ODX image and run inside it
set -uo pipefail

# ---------------------------------------------------------------------------------
# Known failures. Format: <module>.<Class>.<test>
#
# test_photo.TestPhoto.test_jpeg_xl
#   Reads a JPEG-XL asset and expects camera_make == "Lab 308"; gets "". Verified
#   PRE-EXISTING and NOT OURS on 2026-08-16 by swapping upstream/master's photo.py and
#   multispectral.py into the same image and re-running — it fails identically. Ours is
#   not in the path. Reproduces in both `webodm/odx:latest` and our engine image.
# ---------------------------------------------------------------------------------
KNOWN_FAILURES=(
    "test_photo.TestPhoto.test_jpeg_xl"
)

# Pinned so a CI result means something. If Docker Hub prunes this digest the pull
# fails loudly — re-pin from `docker buildx imagetools inspect webodm/odx:latest`, or
# from our own engine-odx mirror in Artifact Registry.
ODX_IMAGE="${ODX_IMAGE:-webodm/odx@sha256:a4cdbbaf7a68cf8c15d7856a935703d5374c8c1ce03d9b93e51f07e3a7f1185f}"

if [ "${1:-}" = "--docker" ]; then
    here="$(cd "$(dirname "$0")" && pwd)"
    # The suite writes into tests/assets/output, so the mounts must be writable.
    exec docker run --rm --platform linux/amd64 \
        -v "$here/tests:/code/tests" \
        -v "$here/opendm:/code/opendm" \
        -v "$here/stages:/code/stages" \
        -v "$here/agro_test.sh:/code/agro_test.sh:ro" \
        --entrypoint bash "$ODX_IMAGE" -c 'cd /code && ./agro_test.sh'
fi

cd "$(dirname "$0")"
out="$(python3 -m unittest discover tests "test_*.py" -v 2>&1)"
echo "$out"
echo
echo "──────────────────────────────────────────────────────────────────"

# unittest prints:  FAIL: test_name (module.Class.test_name)
# The parenthesised id is the stable one — take that.
actual="$(printf '%s\n' "$out" \
    | grep -E '^(FAIL|ERROR): ' \
    | sed -E 's/^(FAIL|ERROR): .*\(([^)]*)\).*/\2/' \
    | sort -u || true)"

expected="$(printf '%s\n' "${KNOWN_FAILURES[@]}" | sort -u)"

unexpected="$(comm -23 <(printf '%s\n' "$actual" | grep -v '^$' || true) <(printf '%s\n' "$expected"))"
fixed="$(comm -13 <(printf '%s\n' "$actual" | grep -v '^$' || true) <(printf '%s\n' "$expected"))"

rc=0
if [ -n "$unexpected" ]; then
    echo "❌ UNEXPECTED FAILURES — these are regressions:"
    printf '     %s\n' $unexpected
    rc=1
fi
if [ -n "$fixed" ]; then
    echo "❌ These are on the KNOWN_FAILURES list but PASSED:"
    printf '     %s\n' $fixed
    echo "   Remove them from KNOWN_FAILURES in agro_test.sh. A list that only grows is"
    echo "   somewhere regressions hide."
    rc=1
fi
if [ "$rc" = "0" ]; then
    n=$(printf '%s\n' "$actual" | grep -vc '^$' || true)
    echo "✅ suite matches the expected failure set exactly (${n} known failure(s), 0 regressions)"
fi
exit $rc
