#!/usr/bin/env bash
# Self-check for persistent-home.sh: dev state must end up as symlinks into
# workspace/persistent/home, migrating pre-existing real files without clobbering
# durable data. Run: bash tests/test_persistent_home.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$HERE/rootfs/opt/capsule/persistent-home.sh"
HOME="$(mktemp -d)"; export HOME
mkdir -p "$HOME/workspace/persistent"

# Case 1: fresh home -> targets become symlinks into persistent/home.
bash "$SCRIPT"
for name in .gitconfig .ssh projects; do
  [ -L "$HOME/$name" ] || { echo "FAIL: $name is not a symlink"; exit 1; }
  case "$(readlink "$HOME/$name")" in
    "$HOME/workspace/persistent/home/$name") ;;
    *) echo "FAIL: $name points at $(readlink "$HOME/$name")"; exit 1;;
  esac
done

# Case 2: a real file shadowing a target is migrated in, then linked.
rm "$HOME/.gitconfig"
printf 'x' > "$HOME/.gitconfig"
bash "$SCRIPT"
[ -L "$HOME/.gitconfig" ] || { echo "FAIL: .gitconfig not relinked"; exit 1; }
[ "$(cat "$HOME/.gitconfig")" = "x" ] || { echo "FAIL: .gitconfig content lost"; exit 1; }

# Case 3: durable data is never clobbered when a real dir also exists.
rm "$HOME/projects"
mkdir -p "$HOME/projects"; printf 'live' > "$HOME/projects/new.txt"
printf 'durable' > "$HOME/workspace/persistent/home/projects/old.txt"
bash "$SCRIPT"
[ -L "$HOME/projects" ] || { echo "FAIL: projects not relinked"; exit 1; }
[ "$(cat "$HOME/projects/old.txt")" = "durable" ] || { echo "FAIL: durable file lost"; exit 1; }
[ "$(cat "$HOME/projects/new.txt")" = "live" ] || { echo "FAIL: migrated file lost"; exit 1; }

# Case 4: idempotent — a second run leaves correct symlinks untouched.
bash "$SCRIPT"
[ -L "$HOME/.ssh" ] && [ "$(readlink "$HOME/.ssh")" = "$HOME/workspace/persistent/home/.ssh" ] \
  || { echo "FAIL: not idempotent"; exit 1; }

rm -rf "$HOME"
echo "PASS: persistent-home.sh"
