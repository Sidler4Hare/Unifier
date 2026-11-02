#!/bin/sh
# Stage F projection workload.  The same script and the same interpreter run
# on the host direct bind path, through the read-only product FUSE view, and
# inside the still-running container bind path.
#
# Usage:
#   workload.sh DIR REPS EXPECTED_COUNT EXPECTED_SMALL_SHA256 EXPECTED_SEQ_SHA256
#
# It times three operations inside the measured region:
#   1. stat every small file with stat(1)
#   2. read all 4096 bytes of every small file and hash the concatenation
#   3. read the 16 MiB sequential file and hash it
# One line per repetition is printed on stdout.  No timing value is an SLO;
# the probes report the measured distribution.
set -eu
LC_ALL=C
export LC_ALL

if [ "$#" -ne 5 ]; then
    echo "usage: workload.sh DIR REPS EXPECTED_COUNT EXPECTED_SMALL_SHA256 EXPECTED_SEQ_SHA256" >&2
    exit 2
fi

DIR=$1
REPS=$2
EXPECTED_COUNT=$3
EXPECTED_SMALL=$4
EXPECTED_SEQ=$5

[ -d "$DIR/small" ] || { echo "missing $DIR/small" >&2; exit 3; }
[ -f "$DIR/sequential.bin" ] || { echo "missing $DIR/sequential.bin" >&2; exit 3; }

set -- "$DIR"/small/*
COUNT=$#
[ "$COUNT" -eq "$EXPECTED_COUNT" ] || {
    echo "small file count $COUNT != $EXPECTED_COUNT" >&2
    exit 4
}

REP=0
while [ "$REP" -lt "$REPS" ]; do
    T0=$(date +%s%N)
    # stat(1) over every small file; output is captured to keep the shell
    # from interleaving it with the result line.  This is a metadata read.
    SIZES=$(stat -c '%s' "$DIR"/small/*)
    T1=$(date +%s%N)
    SMALL_SHA=$(cat "$DIR"/small/* | sha256sum | cut -d' ' -f1)
    T2=$(date +%s%N)
    SEQ_SHA=$(sha256sum "$DIR/sequential.bin" | cut -d' ' -f1)
    T3=$(date +%s%N)

    [ "$SMALL_SHA" = "$EXPECTED_SMALL" ] || {
        echo "small aggregate hash mismatch: $SMALL_SHA != $EXPECTED_SMALL" >&2
        exit 5
    }
    [ "$SEQ_SHA" = "$EXPECTED_SEQ" ] || {
        echo "sequential hash mismatch: $SEQ_SHA != $EXPECTED_SEQ" >&2
        exit 6
    }
    # The sizes output is only used for a coarse byte-correctness check; no
    # per-file timing claim is made from it.
    BAD=$(printf '%s\n' "$SIZES" | awk '$1 != 4096 { count++ } END { print count + 0 }')
    [ "$BAD" -eq 0 ] || {
        echo "found $BAD small files without 4096 bytes" >&2
        exit 7
    }

    STAT_NS=$((T1 - T0))
    READ_NS=$((T2 - T1))
    SEQ_NS=$((T3 - T2))
    printf 'rep=%s stat_ns=%s read_ns=%s seq_ns=%s small_sha=%s seq_sha=%s count=%s\n' \
        "$REP" "$STAT_NS" "$READ_NS" "$SEQ_NS" "$SMALL_SHA" "$SEQ_SHA" "$COUNT"
    REP=$((REP + 1))
done
