#!/bin/sh
. /etc/os-release
printf "child:%s:%s\n" "$ID" "$1"
