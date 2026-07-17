#!/usr/bin/env bash
# Make the app user's dev state live under workspace/persistent/ so it survives
# trash/freeze/thaw. Everything OUTSIDE persistent/ dies with the VM; the control
# plane mirrors ONLY persistent/ to the tenant's S3 prefix. So we point the
# dotfiles a developer cares about (git identity, ssh keys, code-server settings,
# their project tree) at durable storage via symlinks.
#
# Runs as uid app (1000) at boot, AFTER the persistent restore has populated the
# folder. Idempotent: existing correct symlinks are left alone; a real file that
# shadows a target is moved into persistent once, then linked.
set -euo pipefail
HOME_DIR="${HOME:-/home/app}"
PERSIST="$HOME_DIR/workspace/persistent"
HOME_STATE="$PERSIST/home"   # durable mirror of selected dotfiles/dirs

mkdir -p "$HOME_STATE/.ssh" "$HOME_STATE/.config" "$HOME_STATE/projects" \
         "$HOME_STATE/.local/share/code-server" "$HOME_STATE/.config/code-server"
chmod 0700 "$HOME_STATE/.ssh"

# link_into <name>: ensure $HOME/<name> is a symlink to $HOME_STATE/<name>.
# If a non-symlink already exists at $HOME/<name>, migrate its contents into the
# durable copy once (never clobber durable data), then replace with the link.
link_into() {
  local name="$1" src="$HOME_DIR/$1" dst="$HOME_STATE/$1"
  mkdir -p "$(dirname "$src")"   # ensure the parent exists so the symlink can be placed
  if [ -L "$src" ]; then
    # already a symlink; repoint only if it drifted
    [ "$(readlink -f "$src" 2>/dev/null)" = "$dst" ] || { rm -f "$src"; ln -s "$dst" "$src"; }
    return
  fi
  if [ -e "$src" ]; then
    # real file/dir shadowing the target: migrate contents in, then link
    if [ -d "$src" ] && [ -d "$dst" ]; then
      cp -an "$src/." "$dst/" 2>/dev/null || true
      rm -rf "$src"
    elif [ ! -e "$dst" ]; then
      mv "$src" "$dst"
    else
      rm -rf "$src"
    fi
  fi
  ln -s "$dst" "$src"
}

link_into .gitconfig
link_into .ssh
link_into projects
# code-server keeps user settings + installed extensions here; persisting them
# means a dev's editor setup survives a trashed VM.
link_into .local/share/code-server
link_into .config/code-server

echo "[persistent-home] dev state linked into $HOME_STATE" >&2
