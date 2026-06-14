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

console.log(banner);

// Show help if requested
if (hasHelp) {
  console.log(`  ${yellow}Usage:${reset} npx carl-core [options]

  ${yellow}Options:${reset}
    ${amber}-g, --global${reset}              Install globally (to ~/.claude and ~/.carl)
    ${amber}-l, --local${reset}               Install locally (to ./.claude and ./.carl)
    ${amber}-c, --config-dir <path>${reset}   Specify custom Claude config directory
    ${amber}--skip-claude-md${reset}          Don't modify CLAUDE.md
    ${amber}-h, --help${reset}                Show this help message

  ${yellow}Examples:${reset}
    ${dim}# Interactive install${reset}
    npx carl-core

    ${dim}# Install globally (recommended)${reset}
    npx carl-core --global

    ${dim}# Install to current project only${reset}
    npx carl-core --local

  ${yellow}What gets installed:${reset}
    hooks/carl-hook.py     - Rule injection hook (v2, JSON-based)
    .carl/carl.json        - Domain rules, decisions, config
    .carl/carl-mcp/        - MCP server for runtime management
    settings.json          - Hook registration (merged)
    .mcp.json              - MCP server registration
    CLAUDE.md              - CARL integration block (optional)

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
 * Copy a single file, substituting ${CLAUDE_PLUGIN_ROOT} with pluginRootSub
 * in text files (UTF-8 decodable). Binary files are copied byte-for-byte.
 */
function copyFileWithMacroSub(srcPath, destPath, pluginRootSub) {
  const _TEXT_EXTS = new Set(['.js', '.mjs', '.py', '.json', '.md', '.txt', '.sh', '.yaml', '.yml', '.toml']);
  const ext = path.extname(destPath).toLowerCase();
  const isText = ext === '' || _TEXT_EXTS.has(ext);
  if (isText && pluginRootSub) {
    let content;
    try {
      content = fs.readFileSync(srcPath, 'utf8');
    } catch (_e) {
      // Not valid UTF-8 — copy raw
      fs.copyFileSync(srcPath, destPath);
      return;
    }
    const rewritten = content.split('${CLAUDE_PLUGIN_ROOT}').join(pluginRootSub);
    fs.writeFileSync(destPath, rewritten, 'utf8');
  } else {
    fs.copyFileSync(srcPath, destPath);
  }
}

/**
 * Recursively copy directory, substituting ${CLAUDE_PLUGIN_ROOT} in text files.
 */
function copyDir(srcDir, destDir, pluginRootSub) {
  fs.mkdirSync(destDir, { recursive: true });

  const entries = fs.readdirSync(srcDir, { withFileTypes: true });

  for (const entry of entries) {
    const srcPath = path.join(srcDir, entry.name);
    const destPath = path.join(destDir, entry.name);

    if (entry.isDirectory()) {
      copyDir(srcPath, destPath, pluginRootSub);
    } else {
      copyFileWithMacroSub(srcPath, destPath, pluginRootSub);
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
function installMcp(carlDir, src, pluginRootSub) {
  const mcpDest = path.join(carlDir, 'carl-mcp');
  const mcpSrc = path.join(src, 'mcp');

  if (!fs.existsSync(mcpSrc)) {
    console.log(`  ${yellow}Warning: MCP source not found, skipping${reset}`);
    return null;
  }

  // Copy MCP files (substitute ${CLAUDE_PLUGIN_ROOT} with install base)
  copyDir(mcpSrc, mcpDest, pluginRootSub);
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

  // The effective plugin root for substituting ${CLAUDE_PLUGIN_ROOT} in copied
  // text files. In plugin mode this macro is resolved by Claude Code; in npx
  // mode we substitute the actual claudeDir so no literal macro remains.
  const pluginRootSub = claudeDir;

  // 1. Copy hook script
  const hooksDir = path.join(claudeDir, 'hooks');
  fs.mkdirSync(hooksDir, { recursive: true });
  const hookSrc = path.join(src, 'hooks', 'carl-hook.py');
  const hookDest = path.join(hooksDir, 'carl-hook.py');
  copyFileWithMacroSub(hookSrc, hookDest, pluginRootSub);
  fs.chmodSync(hookDest, '755');
  console.log(`  ${green}✓${reset} Installed hooks/carl-hook.py (v2)`);

  // 2. Copy .carl-template to .carl (carl.json + sessions/)
  const carlTemplateSrc = path.join(src, '.carl-template');
  if (!fs.existsSync(carlDir)) {
    copyDir(carlTemplateSrc, carlDir, pluginRootSub);
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
    copyFileWithMacroSub(carlJsonSrc, carlJsonDest, pluginRootSub);
    console.log(`  ${green}✓${reset} Added carl.json to existing ${carlLabel}`);
    console.log(`  ${yellow}Note: Existing v1 files detected. Run migrate-v1-to-v2.sh to convert.${reset}`);
  } else {
    console.log(`  ${dim}${carlLabel}/carl.json already exists, skipping${reset}`);
  }

  // 3. Install MCP server
  const mcpIndexPath = installMcp(carlDir, src, pluginRootSub);

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
} else if (hasGlobal) {
  install(true, !skipClaudeMd);
} else if (hasLocal) {
  install(false, !skipClaudeMd);
} else {
  promptLocation();
}
