#!/usr/bin/env bash
# Скачивает статические ffmpeg/ffprobe/ffplay для оболочки Tauri (Р-5, этап 8).
#
# Tauri ждёт сайдкары с суффиксом target triple: binaries/ffmpeg-aarch64-apple-darwin.
# Источник — сборочный сервер Martin Riedl (release-ветка, macOS arm64 / x86_64).
# Папка binaries/ в .gitignore: ~200 МБ бинарников в репозитории не место.
set -euo pipefail

cd "$(dirname "$0")/../frontend/src-tauri"
mkdir -p binaries

triple="$(rustc -vV | sed -n 's/^host: //p')"
case "$triple" in
  aarch64-apple-darwin) platform="macos/arm64" ;;
  x86_64-apple-darwin)  platform="macos/amd64" ;;
  x86_64-unknown-linux-gnu) platform="linux/amd64" ;;
  aarch64-unknown-linux-gnu) platform="linux/arm64" ;;
  *) echo "Нет готовых сборок для $triple — положите бинарники в binaries/ вручную" >&2; exit 1 ;;
esac

for name in ffmpeg ffprobe ffplay; do
  target="binaries/${name}-${triple}"
  if [[ -x "$target" ]]; then
    echo "$target уже есть"
    continue
  fi
  url="https://ffmpeg.martin-riedl.de/redirect/latest/${platform}/release/${name}.zip"
  echo "Скачиваю $name: $url"
  tmp="$(mktemp -d)"
  curl -fL --progress-bar -o "$tmp/$name.zip" "$url"
  unzip -q -o "$tmp/$name.zip" -d "$tmp"
  mv "$tmp/$name" "$target"
  chmod +x "$target"
  rm -rf "$tmp"
  "$target" -version | head -1
done
