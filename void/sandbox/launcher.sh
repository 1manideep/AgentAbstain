#!/bin/sh
# Runs inside fresh user+mount+pid+net namespaces (see runner.py). Builds an allow-list root:
# only /usr /etc (read-only binds), /bin /lib /lib64 /sbin (symlinks or binds), /proc, /tmp,
# /dev/null and the bind-mounted work dir exist. Everything else on the host is absent.
#   $1 work dir (host path)   $2 new root dir (host path, empty)   "--"   command...
set -e
WORK="$1"; shift
NEWROOT="$1"; shift
[ "$1" = "--" ] && shift
mount --make-rprivate /
mount -t tmpfs -o nosuid,nodev,size=8m tmpfs "$NEWROOT"
cd "$NEWROOT"
mkdir -p work proc tmp old dev
for d in usr bin lib lib64 sbin etc; do
  if [ -L "/$d" ]; then
    ln -s "$(readlink "/$d")" "$d"
  elif [ -d "/$d" ]; then
    mkdir -p "$d"
    mount --rbind "/$d" "$d"
    mount -o remount,ro,bind "$d" "$d" 2>/dev/null || true
  fi
done
mount --bind "$WORK" work
mount -t proc proc proc
mount -t tmpfs -o nosuid,nodev,size=4m tmpfs tmp
touch dev/null && mount --bind /dev/null dev/null || true
pivot_root . old
umount -l /old
cd /work
exec "$@"
