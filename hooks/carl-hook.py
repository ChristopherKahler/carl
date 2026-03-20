#!/usr/bin/env python3
# CARL_HOOK_VERSION=1.0.2
"""
CARL - Context Augmentation & Reinforcement Layer
Smart Injector Hook (v2)

Injects rules based on keyword matching against user prompt.

What gets injected:
1. Context bracket rules (FRESH/MODERATE/DEPLETED) - based on token usage
2. GLOBAL domain rules - every prompt when GLOBAL_STATE=active (no keyword matching)
3. ALWAYS_ON domain rules - every prompt when {DOMAIN}_ALWAYS_ON=true
4. Matched domain rules - when recall keywords found in user prompt
5. Domain summary showing what was loaded and why

Exclusion support:
- GLOBAL_EXCLUDE=word1,word2 - if any found, skip ALL domain matching
- {DOMAIN}_EXCLUDE=word1,word2 - if any found, skip that specific domain

This replaces the passive manifest approach with active keyword detection.
"""
import json
import sys
import re
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


CARL_FOLDER = '.carl'
SESSIONS_FOLDER = 'sessions'
MAX_CONTEXT = 200000
STALE_SESSION_HOURS = 24  # Cleanup sessions older than this
DEBUG = False  # Set to True to log to stderr


def debug_log(msg: str):
    """Log debug messages to stderr."""
    if DEBUG:
        print(f"[CARL] {msg}", file=sys.stderr)


# =============================================================================
# SESSION MANAGEMENT (Phase 1: Per-Session Override System)
# =============================================================================

def get_sessions_path(carl_path: Path) -> Path:
    """Get the sessions directory path, creating if needed."""
    sessions_path = carl_path / SESSIONS_FOLDER
    sessions_path.mkdir(parents=True, exist_ok=True)
    return sessions_path


def load_session_config(carl_path: Path, session_id: str) -> Optional[dict]:
    """
    Load session config from .carl/sessions/{session_id}.json
    Returns None if session doesn't exist.
    """
    if not session_id:
        return None

    sessions_path = get_sessions_path(carl_path)
    session_file = sessions_path / f"{session_id}.json"

    if not session_file.exists():
        return None

    try:
        with open(session_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        debug_log(f"Error loading session config: {e}")
        return None


def save_session_config(carl_path: Path, session_config: dict) -> bool:
    """
    Save session config to .carl/sessions/{uuid}.json
    Returns True on success.
    """
    session_id = session_config.get('uuid')
    if not session_id:
        return False

    sessions_path = get_sessions_path(carl_path)
    session_file = sessions_path / f"{session_id}.json"

    try:
        with open(session_file, 'w') as f:
            json.dump(session_config, f, indent=2)
        return True
    except IOError as e:
        debug_log(f"Error saving session config: {e}")
        return False


def get_manifest_domains(carl_path: Path) -> list[str]:
    """
    Read manifest and extract all domain names.
    Returns list of domain names found (e.g., ['GLOBAL', 'DEVELOPMENT', 'PROJECTS']).
    """
    domains = []
    manifest_path = carl_path / 'manifest'

    if not manifest_path.exists():
        return domains

    try:
        with open(manifest_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key = line.split('=', 1)[0].strip()
                # Extract domain name from keys like DOMAIN_STATE, DOMAIN_ALWAYS_ON
                if key.endswith('_STATE'):
                    domain = key[:-6]  # Remove '_STATE'
                    if domain not in domains:
                        domains.append(domain)
    except Exception:
        pass

    return domains


def create_session_config(session_id: str, cwd: str, carl_path: Path) -> dict:
    """
    Create a new session config with default values.
    All overrides default to null (inherit from global).
    Dynamically builds overrides based on domains in manifest.
    """
    now = datetime.now().isoformat()
    label = Path(cwd).name or "unknown"

    # Build overrides dynamically from manifest domains
    overrides = {"DEVMODE": None}
    for domain in get_manifest_domains(carl_path):
        overrides[f"{domain}_STATE"] = None

    return {
        "uuid": session_id,
        "started": now,
        "cwd": cwd,
        "label": label,
        "title": None,  # User-editable session title
        "prompt_count": 0,  # Track prompts for first-prompt detection
        "last_activity": now,
        "overrides": overrides
    }


def update_session_activity(carl_path: Path, session_id: str) -> None:
    """Update last_activity timestamp for a session."""
    session_config = load_session_config(carl_path, session_id)
    if session_config:
        session_config['last_activity'] = datetime.now().isoformat()
        save_session_config(carl_path, session_config)


def cleanup_stale_sessions(carl_path: Path) -> list[str]:
    """
    Remove session configs older than STALE_SESSION_HOURS.
    Returns list of cleaned up session IDs.
    """
    cleaned = []
    sessions_path = get_sessions_path(carl_path)
    threshold = datetime.now() - timedelta(hours=STALE_SESSION_HOURS)

    for session_file in sessions_path.glob("*.json"):
        try:
            with open(session_file, 'r') as f:
                config = json.load(f)

            # Check last_activity timestamp
            last_activity_str = config.get('last_activity', '')
            if last_activity_str:
                # Handle both formats: with and without timezone
                try:
                    last_activity = datetime.fromisoformat(last_activity_str.replace('Z', '+00:00'))
                    # Make naive for comparison if needed
                    if last_activity.tzinfo:
                        last_activity = last_activity.replace(tzinfo=None)
                except ValueError:
                    last_activity = datetime.min

                if last_activity < threshold:
                    session_file.unlink()
                    cleaned.append(config.get('uuid', session_file.stem))
                    debug_log(f"Cleaned stale session: {session_file.stem}")
        except (json.JSONDecodeError, IOError):
            # Invalid session file, remove it
            session_file.unlink()
            cleaned.append(session_file.stem)

    return cleaned


def merge_manifest_with_session(
    domains: dict,
    global_exclude: list[str],
    devmode: bool,
    session_config: Optional[dict]
) -> tuple[dict, list[str], bool]:
    """
    Merge global manifest settings with session-specific overrides.
    Session overrides (non-null values) take precedence.

    Returns: (merged_domains, global_exclude, effective_devmode)
    """
    if session_config is None:
        return domains, global_exclude, devmode

    overrides = session_config.get('overrides', {})

    # Override DEVMODE if session has explicit setting
    effective_devmode = devmode
    if overrides.get('DEVMODE') is not None:
        effective_devmode = overrides['DEVMODE']
        debug_log(f"Session override DEVMODE: {effective_devmode}")

    # Override domain states
    merged_domains = {}
    for domain, config in domains.items():
        merged_config = config.copy()

        # Check for session override of this domain's state
        state_key = f"{domain}_STATE"
        if overrides.get(state_key) is not None:
            merged_config['state'] = overrides[state_key]
            debug_log(f"Session override {state_key}: {overrides[state_key]}")

        merged_domains[domain] = merged_config

    return merged_domains, global_exclude, effective_devmode


def generate_title_from_transcript(session_id: str, cwd: str) -> Optional[str]:
    """
    Generate a session title from the first user message in the transcript.
    Returns truncated first message or None if can't read.

    Cross-platform compatible:
    - Windows: C:\\Users\\Chris\\project -> %USERPROFILE%\\.claude\\projects\\C--Users-Chris-project\\
    - Linux:   /home/user/project -> ~/.claude/projects/-home-user-project/
    - macOS:   /Users/user/project -> ~/.claude/projects/-Users-user-project/

    The function detects the OS and cwd format to find the correct transcript location.
    """
    import platform

    if not session_id or not cwd:
        return None

    transcript_file = None
    current_os = platform.system()  # 'Windows', 'Linux', 'Darwin' (macOS)

    # Determine if cwd is a Windows-style path (has backslash or drive letter)
    is_windows_path = '\\' in cwd or (len(cwd) >= 2 and cwd[1] == ':')

    if is_windows_path:
        # Windows path format: C:\Users\Chris\project -> C--Users-Chris-project
        # Normalize: replace :\ with --, then \ with -
        normalized = cwd.replace(':\\', '--').replace('\\', '-')

        # Determine Claude home directory based on where we're running
        if current_os == 'Windows':
            # Running natively on Windows
            win_home = Path.home()
        else:
            # Running in WSL/Linux accessing Windows paths
            # Extract from path like C:\Users\Chris\...
            parts = cwd.split('\\')
            if len(parts) >= 3 and parts[1].lower() == 'users':
                win_home = Path(f"/mnt/{parts[0][0].lower()}/Users/{parts[2]}")
            else:
                return None

        # Search for transcript file, trying progressively shorter paths
        search_path = normalized
        for _ in range(10):
            candidate = win_home / '.claude' / 'projects' / search_path / f'{session_id}.jsonl'
            if candidate.exists():
                transcript_file = candidate
                break
            # Try parent path
            if '-' in search_path:
                search_path = search_path.rsplit('-', 1)[0]
            else:
                break

    else:
        # Unix-style path (Linux or macOS): /home/user/project or /Users/user/project
        # Format: path with / replaced by - and leading - added
        # e.g., /home/user/project -> -home-user-project
        # e.g., /Users/user/project -> -Users-user-project
        home = Path.home()
        search_path = Path(cwd)

        for _ in range(10):
            # Convert path to Claude's project directory format
            project_dir = str(search_path).replace('/', '-').lstrip('-')
            candidate = home / '.claude' / 'projects' / f'-{project_dir}' / f'{session_id}.jsonl'
            if candidate.exists():
                transcript_file = candidate
                break
            # Try parent directory
            if search_path.parent == search_path:
                break
            search_path = search_path.parent

    if not transcript_file:
        return None

    try:
        # Read first few lines looking for user message
        with open(transcript_file, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    # Look for user message - format: {"type": "user", "message": {"role": "user", "content": "..."}}
                    if entry.get('type') == 'user':
                        message = entry.get('message', {})
                        content = message.get('content', '')
                        if isinstance(content, list):
                            # Handle content blocks
                            for block in content:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    content = block.get('text', '')
                                    break
                                elif isinstance(block, str):
                                    content = block
                                    break
                        if content and isinstance(content, str):
                            # Clean and truncate
                            title = content.strip()
                            # Remove common prefixes
                            title = title.lstrip('#').strip()
                            # Take first line only
                            title = title.split('\n')[0].strip()
                            # Truncate to reasonable length
                            if len(title) > 60:
                                title = title[:57] + '...'
                            return title if title else None
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        debug_log(f"Error reading transcript for title: {e}")

    return None


def get_or_create_session(carl_path: Path, session_id: str, cwd: str) -> Optional[dict]:
    """
    Get existing session config or create new one.
    Also triggers stale session cleanup periodically.
    """
    if not session_id:
        return None

    # Load existing session
    session_config = load_session_config(carl_path, session_id)

    if session_config:
        # Update activity timestamp and prompt count
        session_config['last_activity'] = datetime.now().isoformat()
        session_config['prompt_count'] = session_config.get('prompt_count', 0) + 1

        # Auto-generate title on prompts 2-5 if not already set
        # Retries on subsequent prompts in case transcript wasn't ready on prompt 2
        prompt_count = session_config['prompt_count']
        if 2 <= prompt_count <= 5 and not session_config.get('title'):
            title = generate_title_from_transcript(session_id, cwd)
            if title:
                session_config['title'] = title
                debug_log(f"Auto-generated title on prompt {prompt_count}: {title}")
            elif prompt_count == 5:
                debug_log("Title generation failed after 4 attempts, giving up")

        save_session_config(carl_path, session_config)
        return session_config

    # Create new session
    session_config = create_session_config(session_id, cwd, carl_path)
    save_session_config(carl_path, session_config)
    debug_log(f"Created new session: {session_id}")

    # Cleanup stale sessions on new session creation (not every prompt)
    cleaned = cleanup_stale_sessions(carl_path)
    if cleaned:
        debug_log(f"Cleaned {len(cleaned)} stale sessions")

    return session_config


# =============================================================================
# CONTEXT PERCENTAGE AND BRACKET
# =============================================================================

def get_context_percentage(input_data: dict) -> Optional[float]:
    """
    Get current context percentage remaining by reading session JSONL file.
    Returns None if cannot determine.
    """
    session_id = input_data.get('sessionId', '') or input_data.get('session_id', '')
    cwd = input_data.get('cwd', '')

    if not session_id or not cwd:
        return None

    # Walk up directory tree to find where session file exists
    # (Claude may be running from subdirectory but session is at workspace root)
    home = str(Path.home())
    search_path = Path(cwd)
    session_file = None

    for _ in range(10):  # Max 10 levels up
        project_dir = str(search_path).replace('/', '-').lstrip('-')
        candidate = Path(home) / '.claude' / 'projects' / f'-{project_dir}' / f'{session_id}.jsonl'
        if candidate.exists():
            session_file = candidate
            break
        if search_path.parent == search_path:  # Hit root
            break
        search_path = search_path.parent

    if session_file is None:
        return None

    try:
        # Read last 20 lines looking for usage data
        result = subprocess.run(
            ['tail', '-20', str(session_file)],
            capture_output=True, text=True, timeout=5
        )

        latest_tokens = 0
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            try:
                data = json.loads(line)
                usage = data.get('message', {}).get('usage', {})
                if usage:
                    input_tokens = usage.get('input_tokens', 0)
                    cache_tokens = usage.get('cache_read_input_tokens', 0)
                    tokens = input_tokens + cache_tokens
                    if tokens > 0:
                        latest_tokens = tokens
            except json.JSONDecodeError:
                continue

        if latest_tokens > 0:
            context_remaining = 100 - (latest_tokens * 100 // MAX_CONTEXT)
            return float(max(0, context_remaining))

    except Exception:
        pass

    return None


def get_active_bracket(context_remaining: Optional[float]) -> str:
    """
    Determine bracket from context percentage.
    Returns FRESH, MODERATE, DEPLETED, or CRITICAL.
    """
    if context_remaining is None:
        return "FRESH"  # Default for fresh/unknown sessions

    if context_remaining >= 60:
        return "FRESH"
    elif context_remaining >= 40:
        return "MODERATE"
    elif context_remaining >= 25:
        return "DEPLETED"
    else:
        return "CRITICAL"


def parse_context_file(context_path: Path) -> tuple[dict[str, bool], dict[str, list[str]]]:
    """
    Parse .carl/context file for bracket rules.
    Returns (bracket_flags, bracket_rules) where:
    - bracket_flags: {BRACKET: enabled_bool}
    - bracket_rules: {BRACKET: [rule1, rule2, ...]}
    """
    bracket_flags = {}
    bracket_rules = {}

    try:
        with open(context_path, 'r') as f:
            lines = f.readlines()
    except Exception:
        return bracket_flags, bracket_rules

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()

        # Detect bracket flags: {BRACKET}_RULES = true/false
        if key.endswith('_RULES') and '_RULE_' not in key:
            bracket_name = key[:-6]
            bracket_flags[bracket_name] = value.lower() in ['true', 'yes', '1']

        # Detect rules: {BRACKET}_RULE_{N} = rule text
        elif '_RULE_' in key and value:
            bracket_name = key.split('_RULE_')[0]
            if bracket_name not in bracket_rules:
                bracket_rules[bracket_name] = []
            bracket_rules[bracket_name].append(value)

    return bracket_flags, bracket_rules


def find_carl_files(cwd: str) -> dict[str, Path]:
    """
    Find all files in .carl/ folder by walking up directory tree.
    Returns dict mapping file type to path.
    """
    carl_files = {}

    # Walk up directory tree to find .carl (like session-context hooks do)
    search_path = Path(cwd)
    carl_path = None

    for _ in range(10):  # Max 10 levels up
        candidate = search_path / CARL_FOLDER
        if candidate.exists() and (candidate / 'manifest').exists():
            carl_path = candidate
            break
        if search_path.parent == search_path:  # Hit root
            break
        search_path = search_path.parent

    # Fallback: check ~/.carl (global install) if not found via cwd traversal
    if carl_path is None:
        global_carl = Path.home() / CARL_FOLDER
        if global_carl.exists() and (global_carl / 'manifest').exists():
            carl_path = global_carl

    if carl_path is None:
        return carl_files

    for f in carl_path.iterdir():
        if f.is_file() and not f.name.startswith('.'):
            # Normalize name: strip .env extension so both 'domain' and 'domain.env' work
            name = f.stem if f.suffix.lower() == '.env' else f.name
            carl_files[name] = f
            carl_files[name.lower()] = f  # lowercase key for case-insensitive lookup

    return carl_files


def parse_manifest(manifest_path: Path) -> tuple[dict, list[str], bool]:
    """
    Parse the manifest file to extract domain configurations and global exclusions.
    Returns tuple of (domains dict, global_exclude list, devmode bool)
    domains: {DOMAIN: {state, always_on, recall, recall_list, exclude_list}}
    """
    domains = {}
    global_exclude = []
    devmode = False

    try:
        with open(manifest_path, 'r') as f:
            lines = f.readlines()
    except Exception:
        return domains, global_exclude, devmode

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()

        # Devmode toggle
        if key == 'DEVMODE':
            devmode = value.lower() in ['true', 'yes', '1', 'on']
            debug_log(f"Devmode: {devmode}")
            continue

        # Global exclusion list
        if key == 'GLOBAL_EXCLUDE':
            global_exclude = [kw.strip().lower() for kw in value.split(',') if kw.strip()]
            debug_log(f"Global exclusions: {global_exclude}")
            continue

        if key.endswith('_STATE'):
            domain = key[:-6]
            if domain not in domains:
                domains[domain] = {}
            domains[domain]['state'] = value.lower() in ['active', 'true', 'yes', '1']

        elif key.endswith('_ALWAYS_ON'):
            domain = key[:-10]
            if domain not in domains:
                domains[domain] = {}
            domains[domain]['always_on'] = value.lower() in ['true', 'yes', '1']

        elif key.endswith('_RECALL'):
            domain = key[:-7]
            if domain not in domains:
                domains[domain] = {}
            domains[domain]['recall'] = value
            # Parse recall keywords into list for matching
            domains[domain]['recall_list'] = [kw.strip().lower() for kw in value.split(',')]

        elif key.endswith('_EXCLUDE'):
            domain = key[:-8]
            if domain not in domains:
                domains[domain] = {}
            domains[domain]['exclude_list'] = [kw.strip().lower() for kw in value.split(',') if kw.strip()]
            debug_log(f"{domain} exclusions: {domains[domain]['exclude_list']}")

    return domains, global_exclude, devmode


def parse_domain_rules(domain_path: Path, domain_name: str) -> list[str]:
    """
    Parse a domain file and extract its rules.
    Returns list of rule strings.
    """
    rules = []

    try:
        with open(domain_path, 'r') as f:
            lines = f.readlines()
    except Exception:
        return rules

    prefix = f"{domain_name}_RULE_"

    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()

        if key.startswith(prefix) and value:
            rules.append(value)

    return rules


def detect_star_commands(user_prompt: str) -> list[str]:
    """
    Detect *xyz command patterns in user prompt.
    Returns list of command names (without asterisk, uppercase).
    Example: "*brief *discuss" -> ["BRIEF", "DISCUSS"]
    """
    pattern = r'\*([a-zA-Z]+)'
    matches = re.findall(pattern, user_prompt)
    return [m.upper() for m in matches]


def parse_command_rules(commands_path: Path, command_names: list[str]) -> dict[str, list[str]]:
    """
    Parse commands file and extract rules for specific commands.
    Only returns rules for the requested command names.
    Returns: {COMMAND: [rules]}
    """
    command_rules = {}

    try:
        with open(commands_path, 'r') as f:
            lines = f.readlines()
    except Exception:
        return command_rules

    for command in command_names:
        prefix = f"{command}_RULE_"
        rules = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue

            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()

            if key.startswith(prefix) and value:
                rules.append(value)

        if rules:
            command_rules[command] = rules

    return command_rules


def check_exclusions(prompt_lower: str, exclude_list: list[str]) -> list[str]:
    """
    Check if any exclusion keywords are in the prompt.
    Returns list of matched exclusion keywords.
    """
    matched_exclusions = []
    for keyword in exclude_list:
        pattern = re.escape(keyword)
        if re.search(pattern, prompt_lower):
            matched_exclusions.append(keyword)
    return matched_exclusions


def match_domains_to_prompt(
    domains: dict,
    user_prompt: str,
    global_exclude: list[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """
    Match recall keywords against user prompt, respecting exclusions.
    Returns tuple of:
    - matched: {DOMAIN: [matched_keywords]}
    - excluded: {DOMAIN: [exclusion_keywords_found]}
    - global_excluded: [global_exclusion_keywords_found]
    """
    matched = {}
    excluded = {}
    prompt_lower = user_prompt.lower()

    # Check global exclusions first
    global_excluded = check_exclusions(prompt_lower, global_exclude)
    if global_excluded:
        debug_log(f"Global exclusion triggered: {global_excluded}")
        return matched, excluded, global_excluded

    for domain, config in domains.items():
        # Skip if inactive or always_on (always_on handled separately)
        if not config.get('state', False) or config.get('always_on', False):
            continue

        recall_list = config.get('recall_list', [])
        if not recall_list:
            continue

        # Check per-domain exclusions
        exclude_list = config.get('exclude_list', [])
        if exclude_list:
            domain_exclusions = check_exclusions(prompt_lower, exclude_list)
            if domain_exclusions:
                excluded[domain] = domain_exclusions
                debug_log(f"Domain {domain} excluded by: {domain_exclusions}")
                continue

        # Check each keyword
        domain_matches = []
        for keyword in recall_list:
            # Use word boundary matching to avoid partial matches (e.g. "co" in "github.com")
            pattern = r'\b' + re.escape(keyword) + r'\b'
            if re.search(pattern, prompt_lower):
                domain_matches.append(keyword)

        if domain_matches:
            matched[domain] = domain_matches
            debug_log(f"Matched {domain}: {domain_matches}")

    return matched, excluded, global_excluded


def format_output(
    domains: dict,
    always_on_rules: dict[str, list[str]],
    matched_rules: dict[str, list[str]],
    matched_keywords: dict[str, list[str]],
    excluded_domains: dict[str, list[str]],
    global_excluded: list[str],
    devmode: bool,
    bracket: str = "FRESH",
    context_remaining: Optional[float] = None,
    bracket_rules: Optional[list[str]] = None,
    command_rules: Optional[dict[str, list[str]]] = None,
    global_disabled: bool = False,
    domains_with_files: Optional[set[str]] = None,
    context_enabled: bool = True
) -> str:
    """
    Format the injected rules as XML context block.
    """
    output = "\n<carl-rules>\n"

    # Context bracket status (only if CONTEXT domain is enabled)
    if context_enabled:
        if context_remaining is not None:
            is_critical = bracket == "CRITICAL"
            if is_critical:
                output += f"⚠️ CONTEXT CRITICAL: {context_remaining:.0f}% remaining ⚠️\n"
                output += "Recommend: compact session OR spawn fresh agent for remaining work\n\n"
            output += f"CONTEXT BRACKET: [{bracket}] ({context_remaining:.0f}% remaining)\n"
        else:
            output += f"CONTEXT BRACKET: [{bracket}] (fresh session)\n"

        # Bracket-specific rules
        if bracket_rules:
            output += f"\n[{bracket}] CONTEXT RULES:\n"
            for i, rule in enumerate(bracket_rules, 1):
                output += f"  {i}. {rule}\n"

        output += "\n"

    # COMMANDS - Explicit *command invocations (highest priority)
    if command_rules:
        output += "🎯 ACTIVE COMMANDS 🎯\n"
        output += "="*60 + "\n"
        output += "EXPLICIT COMMAND INVOCATION - EXECUTE THESE INSTRUCTIONS:\n\n"
        for cmd, rules in command_rules.items():
            output += f"[*{cmd.lower()}] COMMAND:\n"
            for i, rule in enumerate(rules):
                output += f"  {i}. {rule}\n"
            output += "\n"
        output += "="*60 + "\n\n"

    # DEVMODE instruction - ALWAYS EXPLICIT (both true AND false states)
    if devmode:
        output += "⚠️ DEVMODE=true ⚠️\n"
        output += "="*60 + "\n"
        output += "CRITICAL INSTRUCTION: You MUST append a debug section at the\n"
        output += "end of EVERY response. This is NON-NEGOTIABLE.\n"
        output += "\n"
        output += "Format your debug section EXACTLY like this:\n"
        output += "---\n"
        output += "```\n"
        output += "🔧 CARL DEVMODE\n"
        output += "Domains Loaded: [list domains that were loaded]\n"
        output += "Rules Applied: [list specific rule #s that influenced response]\n"
        output += "Tools Used: [list any tools called]\n"
        output += "Governance: [explain decision-making, approach taken]\n"
        output += "Gaps/Issues: [note any missing rules or inconsistencies]\n"
        output += "```\n"
        output += "---\n"
        output += "="*60 + "\n\n"
    else:
        output += "🚫 DEVMODE=false 🚫\n"
        output += "User has DISABLED debug output. Do NOT append any CARL DEVMODE\n"
        output += "debug section to your responses. Respond normally without debug blocks.\n\n"

    # GLOBAL domain disabled - explicit instruction to not apply from memory
    if global_disabled:
        output += "⛔ GLOBAL RULES DISABLED ⛔\n"
        output += "="*60 + "\n"
        output += "CRITICAL: User has INTENTIONALLY disabled GLOBAL domain rules.\n"
        output += "Do NOT apply any previously-seen GLOBAL rules from conversation memory.\n"
        output += "This is an explicit override - await future activation to resume.\n"
        output += "="*60 + "\n\n"

    # Summary of what was loaded
    output += "LOADED DOMAINS:\n"

    for domain in always_on_rules:
        output += f"  [{domain}] always_on ({len(always_on_rules[domain])} rules)\n"

    for domain in matched_rules:
        keywords = matched_keywords.get(domain, [])
        output += f"  [{domain}] matched: {', '.join(keywords)} ({len(matched_rules[domain])} rules)\n"

    if not always_on_rules and not matched_rules:
        output += "  (none)\n"

    # Show exclusions if any triggered
    if global_excluded:
        output += f"\nGLOBAL EXCLUSION ACTIVE: {', '.join(global_excluded)}\n"
        output += "  (All domain matching skipped)\n"

    if excluded_domains:
        output += "\nEXCLUDED DOMAINS:\n"
        for domain, exclusions in excluded_domains.items():
            output += f"  [{domain}] excluded by: {', '.join(exclusions)}\n"

    # ALWAYS_ON domain rules
    for domain, rules in always_on_rules.items():
        output += f"\n[{domain}] RULES:\n"
        for i, rule in enumerate(rules):
            output += f"  {i}. {rule}\n"

    # Matched domain rules
    for domain, rules in matched_rules.items():
        output += f"\n[{domain}] RULES:\n"
        for i, rule in enumerate(rules):
            output += f"  {i}. {rule}\n"

    # Available domains not loaded (only show if domain file exists)
    unloaded = []
    for domain, config in domains.items():
        if config.get('state', False) and not config.get('always_on', False):
            if domain not in matched_rules and domain not in excluded_domains:
                # Only show if domain file actually exists in .carl/
                if domains_with_files is None or domain.lower() in domains_with_files:
                    recall = config.get('recall', '')
                    unloaded.append(f"{domain} ({recall})")

    if unloaded:
        output += "\nAVAILABLE (not loaded):\n"
        for item in unloaded:
            output += f"  {item}\n"
        output += "Use drl_get_domain_rules(domain) to load manually if needed.\n"

    output += "</carl-rules>\n"

    return output


def main():
    """Main hook execution."""
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    # Get working directory from hook input
    cwd = input_data.get('cwd', str(Path.home()))

    # Get Claude's session UUID for per-session tracking
    session_id = input_data.get('sessionId', '') or input_data.get('session_id', '')

    # Get user prompt - try multiple possible field names
    user_prompt = (
        input_data.get('prompt', '') or
        input_data.get('userInput', '') or
        input_data.get('message', '') or
        input_data.get('input', '')
    )

    debug_log(f"User prompt: {user_prompt[:100]}...")

    # Find all files in .carl/ folder
    carl_files = find_carl_files(cwd)

    # Must have manifest
    if 'manifest' not in carl_files:
        sys.exit(0)

    # Get carl_path from manifest location
    carl_path = carl_files['manifest'].parent

    # Get or create session config (registers new sessions, updates activity)
    session_config = get_or_create_session(carl_path, session_id, cwd)

    # Parse manifest (returns domains, global_exclude, devmode)
    domains, global_exclude, devmode = parse_manifest(carl_files['manifest'])

    if not domains:
        sys.exit(0)

    # Merge manifest with session-specific overrides
    domains, global_exclude, devmode = merge_manifest_with_session(
        domains, global_exclude, devmode, session_config
    )

    # Get context percentage and bracket
    context_remaining = get_context_percentage(input_data)
    bracket = get_active_bracket(context_remaining)
    debug_log(f"Context: {context_remaining}% remaining, bracket: {bracket}")

    # Parse context file for bracket rules (respects CONTEXT_STATE toggle)
    # Check session overrides directly (may not be in domains dict if not in manifest)
    bracket_rules_list = []
    context_enabled = domains.get('CONTEXT', {}).get('state', True)
    if session_config:
        overrides = session_config.get('overrides', {})
        if overrides.get('CONTEXT_STATE') is not None:
            context_enabled = overrides['CONTEXT_STATE']
            debug_log(f"Session override CONTEXT_STATE: {context_enabled}")
    if 'context' in carl_files and context_enabled:
        bracket_flags, all_bracket_rules = parse_context_file(carl_files['context'])
        # Use DEPLETED rules for CRITICAL bracket
        rules_bracket = "DEPLETED" if bracket == "CRITICAL" else bracket
        # Only include if bracket is enabled
        if bracket_flags.get(rules_bracket, True):
            bracket_rules_list = all_bracket_rules.get(rules_bracket, [])
    elif not context_enabled:
        debug_log("CONTEXT domain disabled via CONTEXT_STATE")

    # Detect if GLOBAL domain is intentionally disabled
    global_disabled = False
    if 'GLOBAL' in domains:
        global_config = domains['GLOBAL']
        if not global_config.get('state', True):
            global_disabled = True
            debug_log("GLOBAL domain intentionally disabled")

    # Load ALWAYS_ON domain rules (respects state override)
    # GLOBAL is inherently always_on - no keyword matching, just state check
    always_on_rules = {}
    for domain, config in domains.items():
        is_always_on = config.get('always_on', False) or domain == 'GLOBAL'
        if is_always_on and config.get('state', True):
            domain_file = carl_files.get(domain.lower())
            if domain_file:
                rules = parse_domain_rules(domain_file, domain)
                if rules:
                    always_on_rules[domain] = rules

    # Detect explicit *commands (special handling, 1:1 injection, respects COMMANDS_STATE)
    # Check session overrides directly (may not be in domains dict if not in manifest)
    command_rules = {}
    commands_enabled = domains.get('COMMANDS', {}).get('state', True)
    if session_config:
        overrides = session_config.get('overrides', {})
        if overrides.get('COMMANDS_STATE') is not None:
            commands_enabled = overrides['COMMANDS_STATE']
            debug_log(f"Session override COMMANDS_STATE: {commands_enabled}")
    if user_prompt and 'commands' in carl_files and commands_enabled:
        star_commands = detect_star_commands(user_prompt)
        if star_commands:
            command_rules = parse_command_rules(carl_files['commands'], star_commands)
            debug_log(f"Commands detected: {star_commands}, rules loaded: {list(command_rules.keys())}")
    elif not commands_enabled:
        debug_log("COMMANDS domain disabled via COMMANDS_STATE")

    # Match domains to user prompt (with exclusion support)
    # Exclude COMMANDS domain from regular matching (handled specially above)
    matched_keywords = {}
    matched_rules = {}
    excluded_domains = {}
    global_excluded = []

    if user_prompt:
        matched_keywords, excluded_domains, global_excluded = match_domains_to_prompt(
            domains, user_prompt, global_exclude
        )

        # Load rules for matched domains (skip COMMANDS - handled separately)
        for domain in matched_keywords:
            if domain == 'COMMANDS':
                continue  # Commands handled via star_commands detection
            domain_file = carl_files.get(domain.lower())
            if domain_file:
                rules = parse_domain_rules(domain_file, domain)
                if rules:
                    matched_rules[domain] = rules

    # Remove COMMANDS from matched if present (prevent double-loading)
    matched_keywords.pop('COMMANDS', None)

    # Build set of domain files that exist (lowercase for matching)
    domains_with_files = {name.lower() for name in carl_files.keys() if name != 'manifest' and name != 'context'}

    # Format output
    context = format_output(
        domains,
        always_on_rules,
        matched_rules,
        matched_keywords,
        excluded_domains,
        global_excluded,
        devmode,
        bracket,
        context_remaining,
        bracket_rules_list,
        command_rules,
        global_disabled,
        domains_with_files,
        context_enabled
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context
        }
    }

    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
