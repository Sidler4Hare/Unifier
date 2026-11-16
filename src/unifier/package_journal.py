"""Native recovery of the supported Debian and Fedora database journals."""

TARGETS = frozenset(("debian13", "fedora44"))

# This runs in the identity-pinned guest as root. The package manager holds
# its own locks during writeback. Shell checks refuse unexpected layouts
# before asking a native tool to open a database for writing.
SCRIPT = r'''
set -eu
target=$1
action=$2
case "$action" in
    probe|checkpoint) ;;
    *) exit 2 ;;
esac

plain_dir() { test -d "$1" && test ! -L "$1"; }
plain_file() { test -f "$1" && test ! -L "$1"; }
case "$target" in
    debian13)
        db=/var/lib/dpkg
        plain_dir /var && plain_dir /var/lib && plain_dir "$db" &&
            plain_file "$db/status" && plain_file "$db/lock" &&
            plain_dir "$db/updates" || exit 2
        entry=$(find "$db/updates" -mindepth 1 -maxdepth 1 -print -quit)
        if test -z "$entry"; then printf 'clean\n'; exit 0; fi
        if test "$action" = probe; then printf 'pending\n'; exit 0; fi
        unexpected=$(find "$db/updates" -mindepth 1 -maxdepth 1 \
            \( ! -type f -o \( ! -name '[0-9][0-9][0-9][0-9]' ! -name 'tmp.i' \) \) \
            -print -quit)
        test -z "$unexpected" || exit 2
        dpkg --admindir="$db" --set-selections </dev/null >&2
        ;;
    fedora44)
        db=$(rpm --eval '%{_dbpath}')
        case "$db" in
            /usr/lib/sysimage/rpm) plain_dir /usr && plain_dir /usr/lib &&
                plain_dir /usr/lib/sysimage || exit 2 ;;
            /var/lib/rpm) plain_dir /var && plain_dir /var/lib || exit 2 ;;
            *) exit 2 ;;
        esac
        test "$(rpm --eval '%{_db_backend}')" = sqlite &&
            plain_dir "$db" && plain_file "$db/rpmdb.sqlite" &&
            test -s "$db/rpmdb.sqlite" && plain_file "$db/.rpm.lock" || exit 2
        for file in "$db/rpmdb.sqlite-wal" "$db/rpmdb.sqlite-journal" "$db/rpmdb.sqlite-shm"; do
            test ! -L "$file" && { test ! -e "$file" || plain_file "$file"; } || exit 2
        done
        if test ! -s "$db/rpmdb.sqlite-wal" && test ! -s "$db/rpmdb.sqlite-journal"; then
            printf 'clean\n'; exit 0
        fi
        if test "$action" = probe; then printf 'pending\n'; exit 0; fi
        # Initializes only a missing DB. The verified existing SQLite DB
        # is opened read/write and its native close checkpoints the WAL.
        rpmdb --dbpath "$db" --initdb >&2
        ;;
    *) exit 2 ;;
esac
printf 'clean\n'
'''
