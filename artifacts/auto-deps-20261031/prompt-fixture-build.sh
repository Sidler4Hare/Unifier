set -eu
root=/tmp/unifier-native-prompt-fixture
mkdir -p "$root/DEBIAN" "$root/usr/share/unifier-native-prompt"
printf 'Package: unifier-native-prompt\nVersion: 1.0\nArchitecture: all\nMaintainer: Unifier test\nDescription: isolated native prompt acceptance fixture\n' > "$root/DEBIAN/control"
printf 'fixture\n' > "$root/usr/share/unifier-native-prompt/marker"
cat > "$root/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -eu
if [ "$1" = configure ]; then
    printf 'NATIVE_FIXTURE_CHOICE:'
    read -r answer < /dev/tty
    printf '%s\n' "$answer" > /var/lib/unifier-native-prompt-answer
    stty size < /dev/tty >> /var/lib/unifier-native-prompt-answer
fi
SH
chmod 755 "$root/DEBIAN/postinst"
dpkg-deb --build "$root" /tmp/unifier-native-prompt.deb
