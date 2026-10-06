#!/bin/sh
set -eu

interpreter=$1
rpc_fixture=$2
native_cli=$3

for check in tests/rust/check_*.py; do
    case "$check" in
        */check_binding.py) "$interpreter" -I "$check" "$rpc_fixture" "$native_cli" ;;
        *_cli.py) "$interpreter" -I "$check" "$native_cli" ;;
        *) "$interpreter" -I "$check" ;;
    esac
done
