#!/usr/bin/env bash
set -euo pipefail

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
build_root=$(mktemp -d /tmp/dure-deb-build.XXXXXX)
cleanup() {
  find "$build_root" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT INT TERM

source_dir="$build_root/source"
mkdir -p "$source_dir/dure"
cp "$repository_root/pyproject.toml" "$repository_root/setup.py" \
  "$repository_root/README.md" "$repository_root/alembic.ini" "$source_dir/"
cp -a "$repository_root/dure/src" "$repository_root/dure/tests" \
  "$repository_root/dure/packaging" "$source_dir/dure/"
cp -a "$repository_root/dure/debian" "$source_dir/debian"

cd "$source_dir"
dpkg-buildpackage -us -uc -b >&2

version=$(dpkg-parsechangelog -S Version)
built_package="$build_root/dure_${version}_all.deb"
package="$repository_root/../dure_${version}_all.deb"
if [[ ! -f "$built_package" ]]; then
  echo "Expected package was not produced: $built_package" >&2
  exit 1
fi
install -m 0644 "$built_package" "$package"

echo "$package"
