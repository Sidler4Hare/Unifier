#!/bin/sh
# Dolphin supplies one argument per selected local file through %F.
set -eu

fail() {
    printf '%s\n' "$1" >&2
    if command -v kdialog >/dev/null 2>&1; then
        kdialog --title Unifier --error "$1" || :
    fi
    exit 1
}

[ "$#" -ge 3 ] || fail 'No files selected.'
launcher=$1
target=$2
shift 2
for source do
    case "$source" in
        /*) [ -f "$source" ] && [ ! -L "$source" ] || fail "只支持本地普通文件：$source" ;;
        *) fail "只支持本地绝对路径：$source" ;;
    esac
done

# Keep a pathname's trailing newlines distinct from the CLI's line ending.
destination=$("$launcher" path "$target" --files --writable 2>&1 && printf '.') || fail "$destination"
newline='
'
destination=${destination%"$newline."}
case "$destination" in
    /*) [ -d "$destination" ] && [ ! -L "$destination" ] || fail "共享目录不可用：$destination" ;;
    *) fail "共享目录不可用：$destination" ;;
esac

temporary=
trap '[ -z "$temporary" ] || rm -f -- "$temporary"' EXIT
trap 'exit 1' HUP INT TERM
for source do
    output=$destination/${source##*/}
    if [ -e "$output" ] || [ -L "$output" ]; then
        fail "目标已存在，未覆盖：$output"
    fi
    temporary=$(mktemp -- "$destination/.unifier-copy.XXXXXXXX") || fail "无法在共享目录创建临时文件：$destination"
    message=$(cp --reflink=auto --no-dereference -- "$source" "$temporary" 2>&1) || fail "$message"
    [ -f "$temporary" ] && [ ! -L "$temporary" ] || fail "源文件已变化：$source"
    message=$(mv --no-copy --no-target-directory --update=none-fail -- "$temporary" "$output" 2>&1) || fail "$message"
    temporary=
done
