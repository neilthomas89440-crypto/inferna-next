#!/usr/bin/env bash
# Regenerate gRPC stubs from proto/cluster.proto into the server and worker packages.
# Generated files are committed; CI runs `git diff --exit-code` after this script.
set -euo pipefail

cd "$(dirname "$0")/.."

for target in server/inferna_server/proto worker/inferna_worker/proto; do
  mkdir -p "$target"
done

# The generated *_grpc.py imports the sibling pb2 module by top-level name;
# rewrite it to a relative import so the package works from anywhere.
fix_grpc_import() {
  sed -i 's/^import cluster_pb2 as cluster__pb2$/from . import cluster_pb2 as cluster__pb2/' "$1/cluster_pb2_grpc.py"
}

echo "==> server stubs"
(
  cd server
  uv run python -m grpc_tools.protoc \
    -I ../proto \
    --python_out=inferna_server/proto \
    --grpc_python_out=inferna_server/proto \
    --pyi_out=inferna_server/proto \
    ../proto/cluster.proto
)
fix_grpc_import server/inferna_server/proto

echo "==> worker stubs"
(
  cd worker
  uv run python -m grpc_tools.protoc \
    -I ../proto \
    --python_out=inferna_worker/proto \
    --grpc_python_out=inferna_worker/proto \
    --pyi_out=inferna_worker/proto \
    ../proto/cluster.proto
)
fix_grpc_import worker/inferna_worker/proto

echo "OK: stubs regenerated"
