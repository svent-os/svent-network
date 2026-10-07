#!/bin/sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
rm -rf "$here/bin"; mkdir -p "$here/bin"
GOBIN="$here/bin" go install github.com/nicocha30/ligolo-ng/cmd/proxy@latest && mv "$here/bin/proxy" "$here/bin/ligolo-proxy"
