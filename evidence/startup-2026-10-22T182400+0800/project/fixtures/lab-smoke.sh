#!/bin/sh
set -eu

shared=$1
target=$2
expected_uid=$3
expected_gid=$4
expected_kernel=$5
expected_hash=$6
manager=$7

. /etc/os-release
actual_uid=$(id -u)
actual_gid=$(id -g)
actual_kernel=$(uname -r)
actual_hash=$(sha256sum "$shared/host-input.txt")
actual_hash=${actual_hash%% *}

test "$actual_uid" = "$expected_uid"
test "$actual_gid" = "$expected_gid"
test "$actual_kernel" = "$expected_kernel"
test "$actual_hash" = "$expected_hash"
command -v "$manager" >/dev/null

case "$target" in
    debian13) dpkg-query -W '-f=${Package}\t${Version}\n' > "$shared/$target.packages.txt" ;;
    fedora44) rpm -qa --qf '%{NAME}\t%{VERSION}-%{RELEASE}.%{ARCH}\n' > "$shared/$target.packages.txt" ;;
    arch) pacman -Q > "$shared/$target.packages.txt" ;;
    *) exit 2 ;;
esac

printf '%s\n' "$target:$expected_hash" > "$shared/$target.result.txt"
printf '%s\n' "$target" >> "$shared/cross-environment.txt"
printf 'ML_ID=%s\n' "$ID"
printf 'ML_VERSION_ID=%s\n' "${VERSION_ID-}"
printf 'ML_BUILD_ID=%s\n' "${BUILD_ID-}"
printf 'ML_PRETTY_NAME=%s\n' "${PRETTY_NAME-}"
printf 'ML_UID=%s\n' "$actual_uid"
printf 'ML_GID=%s\n' "$actual_gid"
printf 'ML_KERNEL=%s\n' "$actual_kernel"
printf 'ML_ARCH=%s\n' "$(uname -m)"
printf 'ML_INPUT_SHA256=%s\n' "$actual_hash"
