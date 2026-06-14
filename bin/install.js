#!/usr/bin/env node

const fs = require('fs');
const path = require('path');
const os = require('os');
const readline = require('readline');
const { execSync } = require('child_process');

// Colors (amber/orange theme like Claude Code)
const amber = '\x1b[38;5;214m';
const orange = '\x1b[38;5;208m';
const green = '\x1b[32m';
const yellow = '\x1b[33m';
const dim = '\x1b[2m';
const reset = '\x1b[0m';

// Get version from package.json
const pkg = require('../package.json');

const banner = `
${orange}   ██████╗ █████╗ ██████╗ ██╗
  ██╔════╝██╔══██╗██╔══██╗██║
  ██║     ███████║██████╔╝██║
  ██║     ██╔══██║██╔══██╗██║
  ╚██████╗██║  ██║██║  ██║███████╗
   ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝${reset}

  CARL ${dim}v${pkg.version}${reset}
  Context Augmentation & Reinforcement Layer
`;

// CARL block for CLAUDE.md
const CARL_BLOCK = `<!-- CARL-MANAGED: Do not remove this section -->
## CARL Integration

Follow all rules in <carl-rules> blocks from system-reminders.
These are dynamically injected based on context and MUST be obeyed.
<!-- END CARL-MANAGED -->`;

// Parse args
const args = process.argv.slice(2);
const hasGlobal = args.includes('--global') || args.includes('-g');
const hasLocal = args.includes('--local') || args.includes('-l');
const hasHelp = args.includes('--help') || args.includes('-h');
const skipClaudeMd = args.includes('--skip-claude-md');
const hasSkillsDir = args.includes('--skills-dir');

// Parse --config-dir argument
function parseConfigDirArg() {
  const configDirIndex = args.findIndex(arg => arg === '--config-dir' || arg === '-c');
  if (configDirIndex !== -1) {
    const nextArg = args[configDirIndex + 1];
    if (!nextArg || nextArg.startsWith('-')) {
      console.error(`  ${yellow}--config-dir requires a path argument${reset}`);
      process.exit(1);
    }
    return nextArg;
  }
  const configDirArg = args.find(arg => arg.startsWith('--config-dir=') || arg.startsWith('-c='));
  if (configDirArg) {
    return configDirArg.split('=')[1];
  }
  return null;
}
const explicitConfigDir = parseConfigDirArg();

// Parse --dir argument (used with --skills-dir)
function parseSkillsDirArg() {
  const dirIndex = args.findIndex(arg => arg === '--dir');
  if (dirIndex !== -1) {
    const nextArg = args[dirIndex + 1];
    if (!nextArg || nextArg.startsWith('-')) {
      console.error(`  ${yellow}--dir requires a path argument${reset}`);
      process.exit(1);
    }
    return nextArg;
  }
  return null;
}
const explicitSkillsDir = parseSkillsDirArg();

console.log(banner);

// Show help if requested
if (hasHelp) {
  console.log(`  ${yellow}Usage:${reset} npx carl-core [options]

  ${yellow}Options:${reset}
    ${amber}-g, --global${reset}              Install globally (to ~/.claude and ~/.carl)
    ${amber}-l, --local${reset}               Install locally (to ./.claude and ./.carl)
    ${amber}-c, --config-dir <path>${reset}   Specify custom Claude config directory
    ${amber}--skills-dir [--dir <path>]${reset}
                              Install as a Claude Code skills-directory plugin.
                              Target: --dir <path> or <cwd>/.claude/skills/carl/
    ${amber}--skip-claude-md${reset}          Don't modify CLAUDE.md
    ${amber}-h, --help${reset}                Show this help message

  ${yellow}Examples:${reset}
    ${dim}# Interactive install${reset}
    npx carl-core

    ${dim}# Install globally (recommended)${reset}
    npx carl-core --global

    ${dim}# Install to current project only${reset}
    npx carl-core --local

    ${dim}# Install as a Claude Code skills-directory plugin${reset}
    npx carl-core --skills-dir
    npx carl-core --skills-dir --dir /path/to/.claude/skills/carl/

  ${yellow}What gets installed:${reset}
    hooks/carl-hook.py     - Rule injection hook (v2, JSON-based)
    .carl/carl.json        - Domain rules, decisions, config
    .carl/carl-mcp/        - MCP server for runtime management
    settings.json          - Hook registration (merged)
    .mcp.json              - MCP server registration
    CLAUDE.md              - CARL integration block (optional)

  ${yellow}Skills-dir mode installs:${reset}
    .claude-plugin/plugin.json   - Plugin manifest
    commands/                    - CARL slash commands
    hooks/ + hooks.json          - Hook files
    mcp/ + .mcp.json             - MCP server files

  ${yellow}v1 Migration:${reset}
    If upgrading from v1 (flat-file manifest), run:
    ${dim}bash node_modules/carl-core/bin/migrate-v1-to-v2.sh ~/.carl${reset}
`);
  process.exit(0);
}

/**
 * Expand ~ to home directory
 */
function expandTilde(filePath) {
  if (filePath && filePath.startsWith('~/')) {
    return path.join(os.homedir(), filePath.slice(2));
  }
  return filePath;
}

/**
 * Recursively copy directory
 */
function copyDir(srcDir, destDir) {
  fs.mkdirSync(destDir, { recursive: true });

  const entries = fs.readdirSync(srcDir, { withFileTypes: true });

  for (const entry of entries) {
    const srcPath = path.join(srcDir, entry.name);
    const destPath = path.join(destDir, entry.name);

    if (entry.isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

/**
 * Wire hook into settings.json
 */
function wireHook(claudeDir, hookPath) {
  const settingsPath = path.join(claudeDir, 'settings.json');
  let settings = {};

  // Read existing settings if present
  if (fs.existsSync(settingsPath)) {
    try {
      settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
    } catch (e) {
      console.log(`  ${yellow}Warning: Could not parse existing settings.json, creating new${reset}`);
    }
  }

  // Ensure hooks structure exists
  if (!settings.hooks) {
    settings.hooks = {};
  }
  if (!settings.hooks.UserPromptSubmit) {
    settings.hooks.UserPromptSubmit = [];
  }

  // Normalize path to use forward slashes (works on all platforms)
  const normalizedPath = hookPath.replace(/\\/g, '/');
  const hookCommand = `python3 ${normalizedPath}`;

  // Check if CARL hook already exists
  const existingIndex = settings.hooks.UserPromptSubmit.findIndex(h => {
    if (h.command && h.command.includes('carl-hook.py')) return true;
    if (h.hooks && h.hooks.some(inner => inner.command && inner.command.includes('carl-hook.py'))) return true;
    return false;
  });

  const newHookEntry = {
    hooks: [
      {
        type: 'command',
        command: hookCommand
      }
    ]
  };

  if (existingIndex !== -1) {
    settings.hooks.UserPromptSubmit[existingIndex] = newHookEntry;
    console.log(`  ${green}✓${reset} Updated hook in settings.json`);
  } else {
    settings.hooks.UserPromptSubmit.push(newHookEntry);
    console.log(`  ${green}✓${reset} Added hook to settings.json`);
  }

  fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2));
}

/**
 * Wire MCP server into .mcp.json at the workspace root
 * MCPs go in .mcp.json, not settings.json
 */
function wireMcp(workspaceDir, mcpIndexPath) {
  const mcpJsonPath = path.join(workspaceDir, '.mcp.json');
  let mcpConfig = {};

  if (fs.existsSync(mcpJsonPath)) {
    try {
      mcpConfig = JSON.parse(fs.readFileSync(mcpJsonPath, 'utf8'));
    } catch (e) {
      console.log(`  ${yellow}Warning: Could not parse existing .mcp.json, creating new${reset}`);
    }
  }

  if (!mcpConfig.mcpServers) {
    mcpConfig.mcpServers = {};
  }

  const normalizedPath = mcpIndexPath.replace(/\\/g, '/');

  mcpConfig.mcpServers['carl-mcp'] = {
    command: 'node',
    args: [normalizedPath],
    type: 'stdio'
  };

  fs.writeFileSync(mcpJsonPath, JSON.stringify(mcpConfig, null, 2));
  console.log(`  ${green}✓${reset} Wired carl-mcp in .mcp.json`);
}

/**
 * Add CARL block to CLAUDE.md
 */
function addCarlBlock(claudeMdPath) {
  let content = '';
  let fileExists = fs.existsSync(claudeMdPath);

  if (fileExists) {
    content = fs.readFileSync(claudeMdPath, 'utf8');

    const normalizedContent = content.replace(/\r\n/g, '\n');
    if (normalizedContent.includes('<!-- CARL-MANAGED:')) {
      console.log(`  ${dim}CARL block already in CLAUDE.md${reset}`);
      return false;
    }
  } else {
    const parentDir = path.dirname(claudeMdPath);
    if (!fs.existsSync(parentDir)) {
      fs.mkdirSync(parentDir, { recursive: true });
    }
    content = `# Claude Code Configuration\n\n${CARL_BLOCK}\n`;
    fs.writeFileSync(claudeMdPath, content);
    return true;
  }

  const lines = content.split(/\r?\n/);
  let insertIndex = 0;

  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startsWith('#')) {
      insertIndex = i + 1;
      while (insertIndex < lines.length && lines[insertIndex].trim() !== '' && !lines[insertIndex].startsWith('#')) {
        insertIndex++;
      }
      break;
    }
  }

  lines.splice(insertIndex, 0, '', CARL_BLOCK, '');
  fs.writeFileSync(claudeMdPath, lines.join('\n'));

  return true;
}

/**
 * Install MCP server
 */
function installMcp(carlDir, src) {
  const mcpDest = path.join(carlDir, 'carl-mcp');
  const mcpSrc = path.join(src, 'mcp');

  if (!fs.existsSync(mcpSrc)) {
    console.log(`  ${yellow}Warning: MCP source not found, skipping${reset}`);
    return null;
  }

  // Copy MCP files
  copyDir(mcpSrc, mcpDest);
  console.log(`  ${green}✓${reset} Installed carl-mcp`);

  // Run npm install for MCP dependencies
  try {
    execSync('npm install --production --silent', {
      cwd: mcpDest,
      stdio: 'pipe'
    });
    console.log(`  ${green}✓${reset} Installed MCP dependencies`);
  } catch (e) {
    console.log(`  ${yellow}Warning: npm install failed for carl-mcp. Run manually:${reset}`);
    console.log(`  ${dim}cd ${mcpDest} && npm install${reset}`);
  }

  return path.join(mcpDest, 'index.js');
}

/**
 * Install to the specified directory
 */
function install(isGlobal, addToClaudeMd = true) {
  const src = path.join(__dirname, '..');
  const configDir = expandTilde(explicitConfigDir) || expandTilde(process.env.CLAUDE_CONFIG_DIR);
  const defaultGlobalDir = configDir || path.join(os.homedir(), '.claude');

  const claudeDir = isGlobal
    ? defaultGlobalDir
    : path.join(process.cwd(), '.claude');

  const carlDir = isGlobal
    ? path.join(os.homedir(), '.carl')
    : path.join(process.cwd(), '.carl');

  const locationLabel = isGlobal
    ? claudeDir.replace(os.homedir(), '~')
    : claudeDir.replace(process.cwd(), '.');

  const carlLabel = isGlobal
    ? carlDir.replace(os.homedir(), '~')
    : carlDir.replace(process.cwd(), '.');

  console.log(`  Installing to ${amber}${locationLabel}${reset} and ${amber}${carlLabel}${reset}\n`);

  // 1. Copy hook script
  const hooksDir = path.join(claudeDir, 'hooks');
  fs.mkdirSync(hooksDir, { recursive: true });
  const hookSrc = path.join(src, 'hooks', 'carl-hook.py');
  const hookDest = path.join(hooksDir, 'carl-hook.py');
  fs.copyFileSync(hookSrc, hookDest);
  fs.chmodSync(hookDest, '755');
  console.log(`  ${green}✓${reset} Installed hooks/carl-hook.py (v2)`);

  // 2. Copy .carl-template to .carl (carl.json + sessions/)
  const carlTemplateSrc = path.join(src, '.carl-template');
  if (!fs.existsSync(carlDir)) {
    copyDir(carlTemplateSrc, carlDir);
    // Stamp the install date in carl.json
    const carlJsonPath = path.join(carlDir, 'carl.json');
    if (fs.existsSync(carlJsonPath)) {
      try {
        const carlJson = JSON.parse(fs.readFileSync(carlJsonPath, 'utf8'));
        carlJson.last_modified = new Date().toISOString();
        fs.writeFileSync(carlJsonPath, JSON.stringify(carlJson, null, 2));
      } catch (e) {
        // Non-critical, continue
      }
    }
    console.log(`  ${green}✓${reset} Created ${carlLabel}/carl.json`);
  } else if (!fs.existsSync(path.join(carlDir, 'carl.json'))) {
    // .carl/ exists but no carl.json — v1 user, copy template
    const carlJsonSrc = path.join(carlTemplateSrc, 'carl.json');
    const carlJsonDest = path.join(carlDir, 'carl.json');
    fs.copyFileSync(carlJsonSrc, carlJsonDest);
    console.log(`  ${green}✓${reset} Added carl.json to existing ${carlLabel}`);
    console.log(`  ${yellow}Note: Existing v1 files detected. Run migrate-v1-to-v2.sh to convert.${reset}`);
  } else {
    console.log(`  ${dim}${carlLabel}/carl.json already exists, skipping${reset}`);
  }

  // 3. Install MCP server
  const mcpIndexPath = installMcp(carlDir, src);

  // 4. Wire hook into settings.json
  wireHook(claudeDir, hookDest);

  // 5. Wire MCP into .mcp.json (at workspace root, not settings.json)
  if (mcpIndexPath) {
    const mcpRoot = isGlobal ? os.homedir() : process.cwd();
    wireMcp(mcpRoot, mcpIndexPath);
  }

  // 6. Add CARL block to CLAUDE.md (if requested)
  if (addToClaudeMd && !skipClaudeMd) {
    const claudeMdPath = path.join(claudeDir, 'CLAUDE.md');
    if (addCarlBlock(claudeMdPath)) {
      console.log(`  ${green}✓${reset} Added CARL block to CLAUDE.md`);
    }
  }

  console.log(`
  ${green}Done!${reset} Restart Claude Code to activate.

  ${amber}What's installed:${reset}
    ${dim}${carlLabel}/carl.json${reset}      - Your rules, decisions, and config
    ${dim}${carlLabel}/carl-mcp/${reset}      - MCP server for runtime management
    ${dim}${locationLabel}/hooks/${reset}      - Rule injection hook

  ${amber}MCP tools available:${reset}
    ${dim}carl_v2_list_domains${reset}    - View all domains
    ${dim}carl_v2_add_rule${reset}        - Add a rule to a domain
    ${dim}carl_v2_log_decision${reset}    - Log a decision

  ${amber}Upgrading from v1?${reset}
    ${dim}bash node_modules/carl-core/bin/migrate-v1-to-v2.sh ${carlLabel}${reset}
`);
}

/**
 * Rewrite framework path references in a file content string:
 * @~/.claude/<x>-framework/ and @./.claude/<x>-framework/ -> ${CLAUDE_PLUGIN_ROOT}/<x>-framework/
 */
function rewriteFrameworkRefs(content) {
  return content
    .replace(/@~\/\.claude\/([\w-]+-framework)\//g, '@${CLAUDE_PLUGIN_ROOT}/$1/')
    .replace(/@\.\/\.claude\/([\w-]+-framework)\//g, '@${CLAUDE_PLUGIN_ROOT}/$1/');
}

/**
 * Install as a Claude Code skills-directory plugin.
 *
 * Layout inside targetDir (.claude/skills/carl/ by default):
 *   .claude-plugin/plugin.json        -- plugin manifest
 *   commands/                         -- CARL slash commands (if any)
 *   hooks/carl-hook.py                -- hook script
 *   hooks/install-mcp-deps.py         -- SessionStart deps installer (idempotent, fail-open)
 *   hooks/hooks.json                  -- hook registration (uses ${CLAUDE_PLUGIN_ROOT})
 *   mcp/                              -- MCP server files
 *   .mcp.json                         -- MCP registration (uses ${CLAUDE_PLUGIN_ROOT}, NODE_PATH)
 */
function installSkillsDir() {
  const src = path.join(__dirname, '..');
  const short = 'carl';

  // Resolve target directory
  const targetDir = explicitSkillsDir
    ? path.resolve(expandTilde(explicitSkillsDir))
    : path.join(process.cwd(), '.claude', 'skills', short);

  console.log(`  Installing skills-dir plugin to ${amber}${targetDir}${reset}\n`);

  // Read package.json for version + description
  const pkgJson = JSON.parse(fs.readFileSync(path.join(src, 'package.json'), 'utf8'));

  // 1. Create .claude-plugin/plugin.json
  const pluginDir = path.join(targetDir, '.claude-plugin');
  fs.mkdirSync(pluginDir, { recursive: true });
  const pluginJson = {
    name: short,
    version: pkgJson.version,
    description: pkgJson.description || 'Context Augmentation & Reinforcement Layer'
  };
  fs.writeFileSync(path.join(pluginDir, 'plugin.json'), JSON.stringify(pluginJson, null, 2));
  console.log(`  ${green}✓${reset} Created .claude-plugin/plugin.json`);

  // 2. Copy commands/ (if present in source)
  const cmdsSrc = path.join(src, 'commands');
  if (fs.existsSync(cmdsSrc)) {
    copyDir(cmdsSrc, path.join(targetDir, 'commands'));
    console.log(`  ${green}✓${reset} Copied commands/`);
  }

  // 3. Copy hooks/carl-hook.py and write install-mcp-deps.py
  const hooksDest = path.join(targetDir, 'hooks');
  fs.mkdirSync(hooksDest, { recursive: true });
  const hookSrc = path.join(src, 'hooks', 'carl-hook.py');
  fs.copyFileSync(hookSrc, path.join(hooksDest, 'carl-hook.py'));
  fs.chmodSync(path.join(hooksDest, 'carl-hook.py'), '755');
  console.log(`  ${green}✓${reset} Copied hooks/carl-hook.py`);

  // Write the SessionStart MCP deps installer script.
  // Uses literal ${CLAUDE_PLUGIN_ROOT} and ${CLAUDE_PLUGIN_DATA} tokens
  // (not JS template interpolation) so they are resolved at hook-run time.
  // The ESM loader does not read NODE_PATH, so we also symlink
  //   ${CLAUDE_PLUGIN_ROOT}/mcp/node_modules -> ${CLAUDE_PLUGIN_DATA}/node_modules
  // so Node's walk-up ESM resolver finds @modelcontextprotocol/sdk.
  const installDepsScript = `#!/usr/bin/env python3
"""
CARL MCP deps installer — SessionStart hook
Installs @modelcontextprotocol/sdk (and other deps from mcp/package.json) into
CLAUDE_PLUGIN_DATA so the skills-dir MCP server can boot without shipping
node_modules.

The MCP (mcp/index.js) uses ESM imports; Node's ESM resolver does NOT read
NODE_PATH.  After installing into CLAUDE_PLUGIN_DATA, this script creates a
symlink at CLAUDE_PLUGIN_ROOT/mcp/node_modules -> CLAUDE_PLUGIN_DATA/node_modules
so the ESM walk-up resolver finds the packages.  NODE_PATH in .mcp.json is kept
as belt-and-suspenders for any CJS callers.

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
`;
  const installerPath = path.join(hooksDest, 'install-mcp-deps.py');
  fs.writeFileSync(installerPath, installDepsScript);
  fs.chmodSync(installerPath, '755');
  console.log(`  ${green}✓${reset} Wrote hooks/install-mcp-deps.py`);

  // 4. Write hooks.json with ${CLAUDE_PLUGIN_ROOT} reference
  //    Keep UserPromptSubmit carl-hook entry; add SessionStart deps-installer entry.
  //    Use string concatenation (not template literals) so ${CLAUDE_PLUGIN_ROOT} is
  //    written verbatim into the file rather than being interpolated by JS.
  const hooksJson = {
    hooks: {
      UserPromptSubmit: [
        {
          hooks: [
            {
              type: 'command',
              command: 'python3 ${CLAUDE_PLUGIN_ROOT}/hooks/carl-hook.py'
            }
          ]
        }
      ],
      SessionStart: [
        {
          hooks: [
            {
              type: 'command',
              command: 'python3 ${CLAUDE_PLUGIN_ROOT}/hooks/install-mcp-deps.py'
            }
          ]
        }
      ]
    }
  };
  let hooksJsonStr = JSON.stringify(hooksJson, null, 2);
  hooksJsonStr = rewriteFrameworkRefs(hooksJsonStr);
  fs.writeFileSync(path.join(hooksDest, 'hooks.json'), hooksJsonStr);
  console.log(`  ${green}✓${reset} Wrote hooks.json`);

  // 5. Copy mcp/ directory
  const mcpSrc = path.join(src, 'mcp');
  if (fs.existsSync(mcpSrc)) {
    copyDir(mcpSrc, path.join(targetDir, 'mcp'));
    console.log(`  ${green}✓${reset} Copied mcp/`);
  }

  // 6. Write .mcp.json with ${CLAUDE_PLUGIN_ROOT} reference and NODE_PATH env.
  //    NODE_PATH is belt-and-suspenders for CJS callers; the operative fix for the
  //    ESM server is the mcp/node_modules symlink created by install-mcp-deps.py.
  const mcpJson = {
    mcpServers: {
      'carl-mcp': {
        command: 'node',
        args: ['${CLAUDE_PLUGIN_ROOT}/mcp/index.js'],
        type: 'stdio',
        env: {
          CLAUDE_PROJECT_DIR: '${CLAUDE_PROJECT_DIR}',
          NODE_PATH: '${CLAUDE_PLUGIN_DATA}/node_modules'
        }
      }
    }
  };
  let mcpJsonStr = JSON.stringify(mcpJson, null, 2);
  mcpJsonStr = rewriteFrameworkRefs(mcpJsonStr);
  fs.writeFileSync(path.join(targetDir, '.mcp.json'), mcpJsonStr);
  console.log(`  ${green}✓${reset} Wrote .mcp.json`);

  console.log(`
  ${green}Done!${reset} Skills-dir plugin installed.

  ${amber}Next steps:${reset}
    ${dim}• Loads as ${short}@skills-dir next session (no marketplace/install needed)${reset}
    ${dim}• Requires workspace trust to activate${reset}
    ${dim}• For Claude Code Cloud: commit the .claude/skills/${short}/ directory${reset}
    ${dim}• On first session start, install-mcp-deps.py installs MCP deps automatically${reset}

  ${amber}What's installed at:${reset} ${targetDir}
    ${dim}.claude-plugin/plugin.json${reset}       - Plugin manifest
    ${dim}commands/${reset}                        - CARL slash commands
    ${dim}hooks/carl-hook.py${reset}               - Rule injection hook
    ${dim}hooks/install-mcp-deps.py${reset}        - SessionStart MCP deps installer
    ${dim}hooks/hooks.json${reset}                 - Hook registration
    ${dim}mcp/${reset}                             - MCP server files
    ${dim}.mcp.json${reset}                        - MCP registration
`);
}

/**
 * Prompt for install location
 */
function promptLocation() {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout
  });

  const configDir = expandTilde(explicitConfigDir) || expandTilde(process.env.CLAUDE_CONFIG_DIR);
  const globalPath = configDir || path.join(os.homedir(), '.claude');
  const globalLabel = globalPath.replace(os.homedir(), '~');

  console.log(`  ${yellow}Where would you like to install?${reset}

  ${amber}1${reset}) Global ${dim}(${globalLabel} + ~/.carl)${reset} - available in all projects
  ${amber}2${reset}) Local  ${dim}(./.claude + ./.carl)${reset} - this project only
`);

  rl.question(`  Choice ${dim}[1]${reset}: `, (answer) => {
    rl.close();
    const choice = answer.trim() || '1';
    const isGlobal = choice !== '2';

    // Ask about CLAUDE.md modification
    const rl2 = readline.createInterface({
      input: process.stdin,
      output: process.stdout
    });

    console.log(`
  ${yellow}Add CARL integration block to CLAUDE.md?${reset}
  ${dim}This helps Claude recognize and follow CARL rules.${reset}

  ${amber}1${reset}) Yes ${dim}(recommended)${reset}
  ${amber}2${reset}) No, I'll add it manually
`);

    rl2.question(`  Choice ${dim}[1]${reset}: `, (answer2) => {
      rl2.close();
      const addBlock = (answer2.trim() || '1') !== '2';
      install(isGlobal, addBlock);
    });
  });
}

// Main
if (hasGlobal && hasLocal) {
  console.error(`  ${yellow}Cannot specify both --global and --local${reset}`);
  process.exit(1);
} else if (explicitConfigDir && hasLocal) {
  console.error(`  ${yellow}Cannot use --config-dir with --local${reset}`);
  process.exit(1);
} else if (hasSkillsDir && (hasGlobal || hasLocal)) {
  console.error(`  ${yellow}Cannot combine --skills-dir with --global or --local${reset}`);
  process.exit(1);
} else if (hasSkillsDir) {
  installSkillsDir();
} else if (hasGlobal) {
  install(true, !skipClaudeMd);
} else if (hasLocal) {
  install(false, !skipClaudeMd);
} else {
  promptLocation();
}
