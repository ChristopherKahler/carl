#!/usr/bin/env python3
"""
install-mcp-deps.py — SessionStart hook for the CARL native plugin.

Bootstraps @modelcontextprotocol/sdk into CLAUDE_PLUGIN_DATA so the MCP
server (mcp/index.js) can boot without shipping node_modules.

The MCP uses ESM imports; Node's ESM resolver does NOT read NODE_PATH.
After installing into CLAUDE_PLUGIN_DATA this script creates a symlink at
CLAUDE_PLUGIN_ROOT/mcp/node_modules -> CLAUDE_PLUGIN_DATA/node_modules
so the ESM walk-up resolver finds the packages.  NODE_PATH in .mcp.json is
kept as belt-and-suspenders for any CJS callers.

This script is idempotent: if the sentinel directory already exists AND the
symlink is already in place, it exits 0 immediately.
It is fail-open: any error prints a warning to stderr and exits 0 so the
session is never blocked.
"""
import os
import sys
import shutil
import subprocess


def warn(msg):
    print(f"[carl-install-mcp-deps] WARNING: {msg}", file=sys.stderr)


def main():
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()

    if not plugin_root:
        warn("CLAUDE_PLUGIN_ROOT is unset; cannot install MCP deps. Skipping.")
        return

    if not plugin_data:
        # Fall back to a dir inside the plugin root so at least something works
        plugin_data = os.path.join(plugin_root, ".mcp-deps")
        warn(f"CLAUDE_PLUGIN_DATA is unset; falling back to {plugin_data}")

    sentinel = os.path.join(plugin_data, "node_modules", "@modelcontextprotocol", "sdk")
    symlink_path = os.path.join(plugin_root, "mcp", "node_modules")
    nm_target = os.path.join(plugin_data, "node_modules")

    # Ensure symlink is in place (idempotent, even on re-runs after a partial first run)
    def ensure_symlink():
        try:
            if os.path.islink(symlink_path):
                current = os.readlink(symlink_path)
                if current == nm_target:
                    return  # already correct
                os.unlink(symlink_path)
            elif os.path.exists(symlink_path):
                # Something else is there (directory from a previous strategy) — leave it
                return
            os.symlink(nm_target, symlink_path)
        except Exception as e:
            warn(f"Could not create node_modules symlink: {e}")

    # Fast exit if already installed
    if os.path.isdir(sentinel):
        ensure_symlink()
        return

    # Copy mcp/package.json into plugin_data so npm install can read deps
    mcp_pkg_src = os.path.join(plugin_root, "mcp", "package.json")
    if not os.path.isfile(mcp_pkg_src):
        warn(f"mcp/package.json not found at {mcp_pkg_src}; cannot install deps.")
        return

    try:
        os.makedirs(plugin_data, exist_ok=True)
        dest_pkg = os.path.join(plugin_data, "package.json")
        shutil.copy2(mcp_pkg_src, dest_pkg)

        # Also copy lockfile if present (for reproducible installs)
        for lockfile in ("package-lock.json", "npm-shrinkwrap.json"):
            src_lock = os.path.join(plugin_root, "mcp", lockfile)
            if os.path.isfile(src_lock):
                shutil.copy2(src_lock, os.path.join(plugin_data, lockfile))
                break

        # Run npm install into plugin_data
        npm = shutil.which("npm")
        if not npm:
            warn("npm not found in PATH; cannot install MCP deps. "
                 "Install node/npm and restart Claude Code.")
            return

        result = subprocess.run(
            [npm, "install", "--omit=dev", "--prefix", plugin_data],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            warn(f"npm install exited {result.returncode}: {result.stderr.strip()}")
            return

        # Create the symlink so ESM walk-up resolution works
        ensure_symlink()

    except Exception as e:
        warn(f"Unexpected error during MCP dep install: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[carl-install-mcp-deps] WARNING: unhandled error: {e}", file=sys.stderr)
