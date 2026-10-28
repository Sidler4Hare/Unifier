#!/bin/sh
set -eu
. /etc/os-release
printf "parent:%s\n" "$ID"
/usr/bin/env sh ./child.sh "$1"
