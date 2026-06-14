#!/usr/bin/env python3
# CARL_HOOK_VERSION=2.0.0
"""
CARL - Context Augmentation & Reinforcement Layer
Smart Injector Hook (v2 — JSON-based)

Reads from .carl/carl.json instead of manifest + domain files.
All other behavior identical to v1 (carl-hook.py).

Drop-in replacement: swap hook path in settings.json when ready.
Legacy carl-hook.py reads manifest + domain files (unchanged).

What gets injected:
1. Context bracket rules (FRESH/MODERATE/DEPLETED) — from carl.json config
2. GLOBAL domain rules (always_on)
3. Matched domain rules (keyword matching)
4. Domain decisions (per-domain)
5. Star commands — from carl.json config
"""
import json
import os
import sys
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


CARL_FOLDER = '.carl'
CARL_JSON = 'carl.json'
SESSIONS_FOLDER = 'sessions'
MAX_CONTEXT = 1000000
STALE_SESSION_HOURS = 24
DEBUG = False

ENABLE_CONTEXT_DEDUP = True
FORCE_EMIT_EVERY_N = 5


def debug_log(msg: str):
    if DEBUG:
        print(f"[CARL-v2] {msg}", file=sys.stderr)


def compute_context_signature(
    bracket: str,
    devmode: bool,
    always_on_domains: list,
    matched_domains: list,
    command_names: list
) -> str:
    parts = [
        "b:{}".format(bracket),
        "d:{}".format(devmode),
        "a:{}".format(','.join(sorted(always_on_domains))),
        "m:{}".format(','.join(sorted(matched_domains))),
        "c:{}".format(','.join(sorted(command_names)))
    ]
    return "|".join(parts)


# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

def get_sessions_path(carl_path: Path) -> Path:
    sessions_path = carl_path / SESSIONS_FOLDER
    sessions_path.mkdir(parents=True, exist_ok=True)
    return sessions_path


def load_session_config(carl_path: Path, session_id: str) -> Optional[dict]:
    if not session_id:
        return None
    sessions_path = get_sessions_path(carl_path)
    session_file = sessions_path / f"{session_id}.json"
    if not session_file.exists():
        return None
    try:
        with open(session_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def save_session_config(carl_path: Path, session_config: dict) -> bool:
    session_id = session_config.get('uuid')
    if not session_id:
        return False
    sessions_path = get_sessions_path(carl_path)
    session_file = sessions_path / f"{session_id}.json"
    try:
        with open(session_file, 'w') as f:
            json.dump(session_config, f, indent=2)
        return True
    except IOError:
        return False


def create_session_config(session_id: str, cwd: str, carl_data: dict) -> dict:
    now = datetime.now().isoformat()
    label = Path(cwd).name or "unknown"
    overrides = {"DEVMODE": None}
    for domain_name in carl_data.get('domains', {}).keys():
        overrides[f"{domain_name}_STATE"] = None
    return {
        "uuid": session_id,
        "started": now,
        "cwd": cwd,
        "label": label,
        "title": None,
        "prompt_count": 0,
        "last_activity": now,
        "overrides": overrides,
        "last_context_signature": None
    }


def update_session_activity(carl_path: Path, session_id: str) -> None:
    session_config = load_session_config(carl_path, session_id)
    if session_config:
        session_config['last_activity'] = datetime.now().isoformat()
        save_session_config(carl_path, session_config)


def cleanup_stale_sessions(carl_path: Path) -> list:
    cleaned = []
    sessions_path = get_sessions_path(carl_path)
    threshold = datetime.now() - timedelta(hours=STALE_SESSION_HOURS)
    for session_file in sessions_path.glob("*.json"):
        try:
            with open(session_file, 'r') as f:
                config = json.load(f)
            last_activity = config.get('last_activity', config.get('started', ''))
            if last_activity:
                last_dt = datetime.fromisoformat(last_activity)
                if last_dt < threshold:
                    session_file.unlink()
                    cleaned.append(config.get('uuid', session_file.stem))
        except Exception:
            pass
    return cleaned


def merge_config_with_session(
    domains: dict,
    global_exclude: list,
    devmode: bool,
    session_config: Optional[dict]
) -> tuple:
    if not session_config:
        return domains, global_exclude, devmode
    overrides = session_config.get('overrides', {})
    if overrides.get('DEVMODE') is not None:
        devmode = overrides['DEVMODE']
    for domain_name in list(domains.keys()):
        key = f"{domain_name}_STATE"
        if overrides.get(key) is not None:
            domains[domain_name]['state'] = overrides[key]
    return domains, global_exclude, devmode


def get_or_create_session(carl_path: Path, session_id: str, cwd: str, carl_data: dict) -> Optional[dict]:
    if not session_id:
        return None
    session_config = load_session_config(carl_path, session_id)
    if session_config is None:
        session_config = create_session_config(session_id, cwd, carl_data)
        cleanup_stale_sessions(carl_path)
    session_config['prompt_count'] = session_config.get('prompt_count', 0) + 1
    session_config['last_activity'] = datetime.now().isoformat()
    save_session_config(carl_path, session_config)
    return session_config


# =============================================================================
# CONTEXT PERCENTAGE & BRACKETS
# =============================================================================

def get_context_percentage(input_data: dict) -> Optional[float]:
    try:
        tokens_remaining = input_data.get('context', {}).get('tokensRemaining')
        tokens_used = input_data.get('context', {}).get('tokensUsed')
        if tokens_remaining is not None and tokens_used is not None:
            total = tokens_remaining + tokens_used
            if total > 0:
                return (tokens_remaining / total) * 100
        turn = input_data.get('context', {}).get('conversationTurnNumber', 1)
        if turn and turn > 1:
            estimated_used = turn * 3000
            remaining = MAX_CONTEXT - estimated_used
            if remaining > 0:
                return (remaining / MAX_CONTEXT) * 100
    except Exception:
        pass
    return None


def get_active_bracket(context_remaining: Optional[float]) -> str:
    if context_remaining is None:
        return "FRESH"
    if context_remaining >= 70:
        return "FRESH"
    elif context_remaining >= 40:
        return "MODERATE"
    elif context_remaining >= 15:
        return "DEPLETED"
    else:
        return "CRITICAL"


# =============================================================================
# V2: JSON-BASED DATA LOADING
# =============================================================================

def find_all_carl_scopes(cwd: str) -> list:
    """Find all .carl/ directories with carl.json by walking up directory tree."""
    scopes = []
    search_path = Path(cwd)
    for i in range(10):
        candidate = search_path / CARL_FOLDER
        carl_json_path = candidate / CARL_JSON
        if candidate.exists() and carl_json_path.exists():
            label = "project" if i == 0 else "scope-{}".format(search_path.name)
            scopes.append((candidate, label))
            debug_log("Found .carl/ scope at {} ({})".format(candidate, label))
        if search_path.parent == search_path:
            break
        search_path = search_path.parent
    return scopes


def load_carl_json(carl_path: Path) -> Optional[dict]:
    """Load carl.json from a .carl/ directory. Returns None if not found."""
    carl_json_path = carl_path / CARL_JSON
    if not carl_json_path.exists():
        return None
    try:
        with open(carl_json_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def carl_json_to_domains(carl_data: dict) -> dict:
    """
    Convert carl.json domains to the internal domains dict format
    that match_domains_to_prompt() expects.
    """
    domains = {}
    for name, domain_data in carl_data.get('domains', {}).items():
        state_str = domain_data.get('state', 'inactive')
        domains[name] = {
            'state': state_str in ('active', True, 'true'),
            'always_on': domain_data.get('always_on', False),
            'recall': ', '.join(domain_data.get('recall', [])),
            'recall_list': [kw.lower() for kw in domain_data.get('recall', [])],
            'exclude_list': [kw.lower() for kw in domain_data.get('exclude', [])],
        }
    return domains


def get_domain_rules_from_json(carl_data: dict, domain_name: str) -> list:
    """Extract rule texts from carl.json for a given domain."""
    domain = carl_data.get('domains', {}).get(domain_name, {})
    return [r.get('text', '') for r in domain.get('rules', []) if r.get('text')]


def get_domain_decisions_from_json(carl_data: dict, domain_name: str) -> list:
    """Extract decisions from carl.json for a given domain."""
    domain = carl_data.get('domains', {}).get(domain_name, {})
    return [d for d in domain.get('decisions', []) if d.get('status', 'active') == 'active']


def get_all_decisions_summary(carl_data: dict) -> str:
    """
    Generate standalone <decisions> summary across ALL domains.
    Replaces the former get-decisions-summary.py hook.
    """
    if not carl_data:
        return ''

    domain_summaries = []
    total_active = 0
    total_archived = 0

    for dname, ddata in carl_data.get('domains', {}).items():
        decisions = ddata.get('decisions', [])
        active = len([d for d in decisions if d.get('status', 'active') == 'active'])
        archived = len([d for d in decisions if d.get('status') == 'archived'])
        if active > 0:
            domain_summaries.append(f"{dname.lower()} ({active})")
        total_active += active
        total_archived += archived

    if not domain_summaries and total_active == 0:
        return '<decisions>No decisions logged yet. Use carl_log_decision tool to start.</decisions>'

    lines = [
        '<decisions>',
        'Domains: {}'.format(', '.join(domain_summaries)),
        'Total: {} active, {} archived'.format(total_active, total_archived),
        'Tools: carl_search_decisions(keyword), carl_get_decisions(domain), carl_log_decision(domain, decision, rationale, recall)',
        '</decisions>',
    ]
    return '\n'.join(lines)


def merge_carl_scopes(scopes: list) -> tuple:
    """
    Merge multiple .carl/ scopes from carl.json files.
    Returns (carl_data, carl_path, domains, global_exclude, devmode)
    All data comes from carl.json — no flat file fallbacks.
    """
    if not scopes:
        return None, None, {}, [], False

    merged_carl_data = {"config": {}, "domains": {}, "staging": []}
    merged_global_exclude = []
    merged_devmode = False
    merged_context_brackets = {}
    merged_commands = {}

    for carl_path, _ in reversed(scopes):
        carl_data = load_carl_json(carl_path)
        if carl_data:
            config = carl_data.get('config', {})
            if config.get('devmode') is not None:
                merged_devmode = config['devmode']
            if config.get('global_exclude'):
                merged_global_exclude = config['global_exclude']

            # Merge context brackets (more-specific overrides)
            for bracket_name, bracket_config in config.get('context_brackets', {}).items():
                merged_context_brackets[bracket_name] = bracket_config

            # Merge star commands (more-specific overrides)
            for cmd_name, cmd_rules in config.get('commands', {}).items():
                merged_commands[cmd_name] = cmd_rules

            for dname, dconfig in carl_data.get('domains', {}).items():
                merged_carl_data['domains'][dname] = dconfig

            merged_carl_data['staging'].extend(carl_data.get('staging', []))

    merged_carl_data['config'] = {
        'devmode': merged_devmode,
        'post_compact_gate': True,
        'global_exclude': merged_global_exclude,
        'context_brackets': merged_context_brackets,
        'commands': merged_commands
    }

    merged_domains = carl_json_to_domains(merged_carl_data)

    primary_carl_path = scopes[0][0]
    return merged_carl_data, primary_carl_path, merged_domains, merged_global_exclude, merged_devmode


# =============================================================================
# STAR COMMANDS (detection only — rules come from carl.json)
# =============================================================================

def detect_star_commands(user_prompt: str) -> list:
    matches = re.findall(r'\*(\w+)', user_prompt)
    return [m.upper() for m in matches] if matches else []


# =============================================================================
# MATCHING & EXCLUSIONS
# =============================================================================

def check_exclusions(prompt_lower: str, exclude_list: list) -> list:
    matched_exclusions = []
    for keyword in exclude_list:
        pattern = re.escape(keyword)
        if re.search(pattern, prompt_lower):
            matched_exclusions.append(keyword)
    return matched_exclusions


def match_domains_to_prompt(
    domains: dict,
    user_prompt: str,
    global_exclude: list
) -> tuple:
    matched = {}
    excluded = {}
    prompt_lower = user_prompt.lower()
    global_excluded = check_exclusions(prompt_lower, global_exclude)
    if global_excluded:
        return matched, excluded, global_excluded
    for domain, config in domains.items():
        if not config.get('state', False) or config.get('always_on', False):
            continue
        recall_list = config.get('recall_list', [])
        if not recall_list:
            continue
        exclude_list = config.get('exclude_list', [])
        if exclude_list:
            domain_exclusions = check_exclusions(prompt_lower, exclude_list)
            if domain_exclusions:
                excluded[domain] = domain_exclusions
                continue
        domain_matches = []
        for keyword in recall_list:
            pattern = re.escape(keyword)
            if re.search(pattern, prompt_lower):
                domain_matches.append(keyword)
        if domain_matches:
            matched[domain] = domain_matches
    return matched, excluded, global_excluded


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================

def format_output(
    domains: dict,
    always_on_rules: dict,
    matched_rules: dict,
    matched_keywords: dict,
    excluded_domains: dict,
    global_excluded: list,
    devmode: bool,
    bracket: str = "FRESH",
    context_remaining: Optional[float] = None,
    bracket_rules: Optional[list] = None,
    command_rules: Optional[dict] = None,
    global_disabled: bool = False,
    domains_with_rules: Optional[set] = None,
    context_enabled: bool = True,
    domain_decisions: Optional[dict] = None
) -> str:
    output = "\n<carl-rules>\n"

    if context_enabled:
        if context_remaining is not None:
            is_critical = bracket == "CRITICAL"
            if is_critical:
                output += f"⚠️ CONTEXT CRITICAL: {context_remaining:.0f}% remaining ⚠️\n"
                output += "Recommend: compact session OR spawn fresh agent for remaining work\n\n"
            output += f"CONTEXT BRACKET: [{bracket}] ({context_remaining:.0f}% remaining)\n"
        else:
            output += f"CONTEXT BRACKET: [{bracket}] (fresh session)\n"
        if bracket_rules:
            output += f"\n[{bracket}] CONTEXT RULES:\n"
            for i, rule in enumerate(bracket_rules, 1):
                output += f"  {i}. {rule}\n"
        output += "\n"

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

    if devmode:
        output += "⚠️ DEVMODE=true ⚠️\n"
        output += "="*60 + "\n"
        output += "MANDATORY: Append a DEVMODE block at the end of EVERY response.\n"
        output += "NEVER skip it. NEVER forget it. NEVER omit it for any reason.\n"
        output += "NEVER fabricate data in the block — only report what you actually received.\n"
        output += "NEVER invent rule numbers, citation counts, or applied counts.\n"
        output += "NEVER write a DEVMODE block from memory — always derive it from THIS prompt's context.\n\n"
        output += "This is a signal dashboard the user reads to tune their context systems.\n\n"
        output += "How to fill each field:\n"
        output += "- CARL: If you received <carl-rules> THIS prompt → list domain names + rule count from the injection.\n"
        output += "        If you received <carl-status dedup=\"true\"> → write 'dedup (prompt N)' using the prompt number from the tag.\n"
        output += "- Rules: Total = count of rules in the last <carl-rules> injection. applied = how many you consciously followed in THIS response. 0 is fine and expected.\n"
        output += "- Decisions: Total = count from <decisions> tag. applied = how many influenced THIS response. Usually 0.\n"
        output += "- Signals: List every hook tag you received THIS prompt (e.g. pulse, active, backlog, calendar, time, machine, context, carl-rules, carl-status, decisions, psmm). Only list tags actually present.\n"
        output += "- Applied: Which of those signals actually shaped your response content. 'none' is the honest default.\n"
        output += "- Tools: Which Claude Code tools you called in THIS response. 'none' if you made no tool calls.\n"
        output += "- Gaps: If you noticed missing context or something that should have been injected but wasn't. Usually 'none'.\n\n"
        output += "Format EXACTLY (keep under 8 lines, no rationale, no prose):\n"
        output += "---\n```\n"
        output += "🔧 CARL DEVMODE\n"
        output += "CARL: [see rules above]\n"
        output += "Rules: [total] | applied: [count, or 0]\n"
        output += "Decisions: [total] | applied: [count, or 0]\n"
        output += "Signals: [tags received this prompt]\n"
        output += "Applied: [which signals shaped response, or 'none']\n"
        output += "Tools: [tools used, or 'none']\n"
        output += "Gaps: [one-line, or 'none']\n"
        output += "```\n---\n"
        output += "="*60 + "\n\n"
    else:
        output += "🚫 DEVMODE=false 🚫\n"
        output += "User has DISABLED debug output. Do NOT append any CARL DEVMODE\n"
        output += "debug section to your responses. Respond normally without debug blocks.\n\n"

    if global_disabled:
        output += "⛔ GLOBAL RULES DISABLED ⛔\n"
        output += "="*60 + "\n"
        output += "CRITICAL: User has INTENTIONALLY disabled GLOBAL domain rules.\n"
        output += "Do NOT apply any previously-seen GLOBAL rules from conversation memory.\n"
        output += "This is an explicit override - await future activation to resume.\n"
        output += "="*60 + "\n\n"

    output += "LOADED DOMAINS:\n"
    for domain in always_on_rules:
        output += f"  [{domain}] always_on ({len(always_on_rules[domain])} rules)\n"
    for domain in matched_rules:
        keywords = matched_keywords.get(domain, [])
        output += f"  [{domain}] matched: {', '.join(keywords)} ({len(matched_rules[domain])} rules)\n"
    if not always_on_rules and not matched_rules:
        output += "  (none)\n"

    if global_excluded:
        output += f"\nGLOBAL EXCLUSION ACTIVE: {', '.join(global_excluded)}\n"
        output += "  (All domain matching skipped)\n"
    if excluded_domains:
        output += "\nEXCLUDED DOMAINS:\n"
        for domain, exclusions in excluded_domains.items():
            output += f"  [{domain}] excluded by: {', '.join(exclusions)}\n"

    for domain, rules in always_on_rules.items():
        output += f"\n[{domain}] RULES:\n"
        for i, rule in enumerate(rules):
            output += f"  {i}. {rule}\n"
        if domain_decisions and domain in domain_decisions:
            decisions = domain_decisions[domain]
            output += f"\n[{domain}] DECISIONS ({len(decisions)}):\n"
            for d in decisions:
                output += "  - {} ({}): {}\n".format(
                    d.get('decision', ''), d.get('date', ''), d.get('rationale', ''))

    for domain, rules in matched_rules.items():
        output += f"\n[{domain}] RULES:\n"
        for i, rule in enumerate(rules):
            output += f"  {i}. {rule}\n"
        if domain_decisions and domain in domain_decisions:
            decisions = domain_decisions[domain]
            output += f"\n[{domain}] DECISIONS ({len(decisions)}):\n"
            for d in decisions:
                output += "  - {} ({}): {}\n".format(
                    d.get('decision', ''), d.get('date', ''), d.get('rationale', ''))

    unloaded = []
    for domain, config in domains.items():
        if config.get('state', False) and not config.get('always_on', False):
            if domain not in matched_rules and domain not in excluded_domains:
                if domains_with_rules is None or domain in domains_with_rules:
                    recall = config.get('recall', '')
                    unloaded.append(f"{domain} ({recall})")
    if unloaded:
        output += "\nAVAILABLE (not loaded):\n"
        for item in unloaded:
            output += f"  {item}\n"
        output += "Use drl_get_domain_rules(domain) to load manually if needed.\n"

    output += "</carl-rules>\n"
    return output


# =============================================================================
# MAIN (v2 — reads carl.json instead of manifest + domain files)
# =============================================================================

def main():
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    cwd = os.environ.get('CLAUDE_PROJECT_DIR') or input_data.get('cwd') or str(Path.home())
    session_id = input_data.get('sessionId', '') or input_data.get('session_id', '')
    user_prompt = (
        input_data.get('prompt', '') or
        input_data.get('userInput', '') or
        input_data.get('message', '') or
        input_data.get('input', '')
    )

    # Find all .carl/ scopes
    scopes = find_all_carl_scopes(cwd)
    if not scopes:
        sys.exit(0)

    # Merge scopes — carl.json based (all data from JSON, no flat files)
    carl_data, carl_path, domains, global_exclude, devmode = merge_carl_scopes(scopes)

    if not domains or carl_path is None:
        sys.exit(0)

    # Session management
    session_config = get_or_create_session(carl_path, session_id, cwd, carl_data or {})

    # Merge with session overrides
    domains, global_exclude, devmode = merge_config_with_session(
        domains, global_exclude, devmode, session_config
    )

    # Context percentage and bracket
    context_remaining = get_context_percentage(input_data)
    bracket = get_active_bracket(context_remaining)

    # Context bracket rules from carl.json config
    bracket_rules_list = []
    context_enabled = True
    if session_config:
        overrides = session_config.get('overrides', {})
        if overrides.get('CONTEXT_STATE') is not None:
            context_enabled = overrides['CONTEXT_STATE']
    if carl_data and context_enabled:
        context_brackets = carl_data.get('config', {}).get('context_brackets', {})
        rules_bracket = "DEPLETED" if bracket == "CRITICAL" else bracket
        bracket_config = context_brackets.get(rules_bracket, {})
        if bracket_config.get('enabled', True):
            bracket_rules_list = bracket_config.get('rules', [])

    # GLOBAL disabled check
    global_disabled = False
    if 'GLOBAL' in domains:
        if not domains['GLOBAL'].get('state', True):
            global_disabled = True

    # Load ALWAYS_ON domain rules from carl.json
    always_on_rules = {}
    if carl_data:
        for domain, config in domains.items():
            is_always_on = config.get('always_on', False) or domain == 'GLOBAL'
            if is_always_on and config.get('state', True):
                rules = get_domain_rules_from_json(carl_data, domain)
                if rules:
                    always_on_rules[domain] = rules

    # Star commands from carl.json config
    command_rules = {}
    if user_prompt and carl_data:
        all_commands = carl_data.get('config', {}).get('commands', {})
        if all_commands:
            star_commands = detect_star_commands(user_prompt)
            for cmd in star_commands:
                if cmd in all_commands:
                    cmd_data = all_commands[cmd]
                    if isinstance(cmd_data, dict) and 'rules' in cmd_data:
                        command_rules[cmd] = cmd_data['rules']
                    elif isinstance(cmd_data, list):
                        command_rules[cmd] = cmd_data
                    else:
                        command_rules[cmd] = cmd_data

    # Match domains to prompt
    matched_keywords = {}
    matched_rules = {}
    excluded_domains = {}
    global_excluded = []

    if user_prompt:
        matched_keywords, excluded_domains, global_excluded = match_domains_to_prompt(
            domains, user_prompt, global_exclude
        )
        if carl_data:
            for domain in matched_keywords:
                if domain == 'COMMANDS':
                    continue
                rules = get_domain_rules_from_json(carl_data, domain)
                if rules:
                    matched_rules[domain] = rules

    matched_keywords.pop('COMMANDS', None)

    # Load decisions from carl.json
    domain_decisions = {}
    if carl_data:
        for domain in list(always_on_rules.keys()) + list(matched_rules.keys()):
            decisions = get_domain_decisions_from_json(carl_data, domain)
            if decisions:
                domain_decisions[domain] = decisions

    # Build set of domains that have rules (for AVAILABLE listing)
    domains_with_rules = set()
    if carl_data:
        for dname, ddata in carl_data.get('domains', {}).items():
            if ddata.get('rules'):
                domains_with_rules.add(dname)

    # Context deduplication
    current_signature = compute_context_signature(
        bracket, devmode,
        list(always_on_rules.keys()),
        list(matched_rules.keys()),
        list(command_rules.keys())
    )

    prompt_count = session_config.get('prompt_count', 1) if session_config else 1
    last_signature = session_config.get('last_context_signature') if session_config else None

    should_emit_full = True
    if ENABLE_CONTEXT_DEDUP and session_config:
        signature_changed = (current_signature != last_signature)
        force_emit = (prompt_count % FORCE_EMIT_EVERY_N == 0)
        first_prompt = (prompt_count <= 1)
        if not signature_changed and not force_emit and not first_prompt:
            should_emit_full = False
        else:
            session_config['last_context_signature'] = current_signature
            save_session_config(carl_path, session_config)

    # Generate decisions summary (always, regardless of dedup)
    decisions_summary = get_all_decisions_summary(carl_data) if carl_data else ''

    if should_emit_full:
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
            domains_with_rules,
            context_enabled,
            domain_decisions
        )

        if decisions_summary:
            context += "\n" + decisions_summary + "\n"

        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context
            }
        }
        print(json.dumps(output))
    else:
        loaded = list(always_on_rules.keys()) + list(matched_rules.keys())
        status = "<carl-status dedup=\"true\" prompt=\"{}\" domains=\"{}\">\n".format(
            prompt_count, ','.join(loaded) if loaded else 'GLOBAL'
        )
        status += "CARL rules were NOT injected this prompt (dedup: signature unchanged).\n"
        status += "Prior injection is still in your context window — operate on those rules.\n"

        if devmode:
            status += "\n"
            status += "DEVMODE=true — MANDATORY DEVMODE BLOCK REQUIRED.\n"
            status += "NEVER skip the DEVMODE block. NEVER fabricate data in it. NEVER write it from memory.\n"
            status += "Derive every field from THIS prompt's context tags.\n"
            status += "CARL line: 'dedup (prompt {})'\n".format(prompt_count)
            status += "Rules: total from last full injection | applied: count that shaped THIS response (0 is fine)\n"
            status += "Decisions: total from <decisions> tag | applied: count (usually 0)\n"
            status += "Signals: list every hook tag received THIS prompt\n"
            status += "Applied: which signals shaped response, or 'none'\n"
            status += "Tools: tools called THIS response, or 'none'\n"
            status += "Gaps: missing context, or 'none'\n"
            status += "Format: ```🔧 CARL DEVMODE``` block, under 8 lines, at END of EVERY response.\n"
        else:
            status += "DEVMODE=false — do NOT append any debug block.\n"

        status += "</carl-status>\n"

        if decisions_summary:
            status += "\n" + decisions_summary + "\n"

        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": status
            }
        }
        print(json.dumps(output))

    sys.exit(0)


if __name__ == "__main__":
    main()
