#!/usr/bin/env bash
set -euo pipefail
base_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$base_dir/../.." && pwd)"
context_path="$project_root/runtime/base-context"
revision=74421fd01047290635742d3fb85f7fd7eebbde86
if [[ ! -e "$context_path" ]]; then
  mkdir -p "$context_path"
  git -C "$context_path" init --quiet
  git -C "$context_path" remote add origin https://github.com/frappe/frappe_docker.git
  git -C "$context_path" fetch --depth 1 origin "$revision"
  git -C "$context_path" checkout --detach --quiet FETCH_HEAD
fi
[[ "$(git -C "$context_path" rev-parse HEAD)" == "$revision" ]] || {
  echo '构建上下文版本不符；保留现有目录后使用独立上下文。' >&2
  exit 1
}
context_changes="$(git -C "$context_path" status --porcelain=v1 --untracked-files=all --ignored=matching --ignore-submodules=none)"
[[ -z "$context_changes" ]] || {
  echo '构建上下文工作树有修改或额外文件；保留现有目录后使用独立上下文。' >&2
  exit 1
}
docker build --progress plain --file "$base_dir/Containerfile" \
  --build-arg FRAPPE_BRANCH=v16.55.2 --build-arg PYTHON_VERSION=3.14 \
  --build-arg NODE_VERSION=24 --build-arg INSTALL_CHROMIUM=false \
  --tag "${1:-research-group-frappe-base:v16.55.2}" "$context_path"
