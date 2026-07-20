#!/usr/bin/env python3
"""
Claude Code Session Recovery Tool

Recovers sessions that disappear from the Claude Desktop app after crashes,
BSODs, disk errors, or other corruption events.

HOW IT WORKS:
Claude Code stores session conversation data as JSONL files in:
    ~/.claude/projects/<project-name>/<session-id>.jsonl

The Claude Desktop app maintains a SEPARATE index of sessions in:
    <app-data>/Claude/claude-code-sessions/<org-id>/<user-id>/local_<uuid>.json

When the desktop app's index gets corrupted (e.g., from a BSOD), sessions
disappear from the UI even though the conversation data is fully intact on
disk. This tool rebuilds that index by creating registration files that point
the desktop app back to the existing session data.

USAGE:
    python recover.py list                     # List all sessions on disk
    python recover.py restore                  # Restore missing sessions to Desktop app
    python recover.py restore --dry-run        # Preview what would be restored
    python recover.py list --json              # Output as JSON
    python recover.py list --project "website" # Filter by project name
    python recover.py export                   # Export transcripts to ./exported-sessions/

After running `restore`, restart Claude Desktop to see recovered sessions.

Works on Windows, macOS, and Linux. Requires Python 3.8+.
No external dependencies.
"""

import json
import os
import re
import sys
import glob
import argparse
import datetime
import uuid as uuid_mod
from pathlib import Path


# --- Path Discovery ---

def find_claude_dir():
    """Find the ~/.claude directory."""
    home = Path.home()
    claude_dir = home / ".claude"
    if claude_dir.exists():
        return claude_dir

    # Windows fallback: check under USERNAME
    if sys.platform == "win32":
        username = os.environ.get("USERNAME", "")
        alt = Path(f"C:/Users/{username}/.claude")
        if alt.exists():
            return alt

    print("ERROR: Could not find ~/.claude directory.")
    print(f"  Searched: {home / '.claude'}")
    print("  Pass --claude-dir to specify the path manually.")
    sys.exit(1)


def _desktop_base_candidates():
    """Return candidate 'claude-code-sessions' base dirs for this platform.

    Ordered by likelihood. On Windows this includes the MSIX / Microsoft Store
    packaged-app redirect: when Claude Desktop is installed as a packaged app,
    Windows redirects its %APPDATA%\\Claude writes into a private per-package
    folder under %LOCALAPPDATA%\\Packages\\<PackageFamilyName>\\LocalCache\\
    Roaming\\Claude. A plain (non-packaged) Python process does not see the
    normal %APPDATA%\\Claude path at all, so we must probe the redirect too.
    """
    candidates = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "Claude" / "claude-code-sessions")

        # MSIX / Store packaged-app redirect(s). Glob for any Claude* package.
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            pkg_glob = str(
                Path(local) / "Packages" / "Claude*" / "LocalCache"
                / "Roaming" / "Claude" / "claude-code-sessions"
            )
            candidates.extend(Path(p) for p in glob.glob(pkg_glob))
    elif sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support"
            / "Claude" / "claude-code-sessions"
        )
    else:
        # Linux: XDG_CONFIG_HOME or ~/.config
        xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        candidates.append(Path(xdg) / "Claude" / "claude-code-sessions")

    return candidates


def find_desktop_sessions_dir():
    """Find the Claude Desktop app's session registration directory."""
    existing_base = None

    for base in _desktop_base_candidates():
        if not base.exists():
            continue
        if existing_base is None:
            existing_base = base

        # Find the org/user subdirectory (two levels of UUIDs)
        for org_dir in base.iterdir():
            if org_dir.is_dir():
                for user_dir in org_dir.iterdir():
                    if user_dir.is_dir():
                        return base, user_dir

    # A base exists but has no org/user subdirs yet — report it for diagnostics.
    return existing_base, None


# --- Session Scanning ---

def extract_preview(filepath, max_bytes=50000):
    """Extract the first human message from a session JSONL file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_bytes)

        for line in content.split("\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line.strip())
                t = obj.get("type", "")

                # Format 1: queue-operation (Claude Desktop / Cowork)
                if t == "queue-operation" and obj.get("operation") == "enqueue":
                    c = obj.get("content", "")
                    if c and len(c) > 3:
                        return c[:120].replace("\n", " ").strip()

                # Format 2: direct human message (Claude Code CLI)
                if t == "human" or obj.get("role") == "human":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict):
                        content_field = msg.get("content", "")
                        if isinstance(content_field, str) and len(content_field) > 3:
                            return content_field[:120].replace("\n", " ").strip()
                        elif isinstance(content_field, list):
                            for c in content_field:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    text = c["text"][:120].replace("\n", " ").strip()
                                    if len(text) > 3:
                                        return text

                # Format 3: summary field
                if obj.get("summary"):
                    return obj["summary"][:120].replace("\n", " ").strip()

            except json.JSONDecodeError:
                continue

    except Exception as e:
        return f"(error: {e})"

    return "(no preview available)"


def derive_title(preview, max_len=60):
    """Create a short title from the preview text."""
    # Strip XML-like tags
    title = preview
    if title.startswith("<"):
        # Try to get text after the tag
        import re
        title = re.sub(r"<[^>]+>", "", title).strip()

    title = title[:max_len].replace("\n", " ").strip()
    if len(preview) > max_len:
        title += "..."
    return title if title else "(recovered session)"


def extract_cwd(filepath, max_bytes=20000):
    """Extract the working directory from a session JSONL file.

    The JSONL data contains the actual cwd used when the session was created.
    This is far more reliable than trying to reverse-engineer the path from
    the project folder name (which uses -- as separator and is ambiguous with
    hyphens in actual path segments).
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(max_bytes)

        for line in content.split("\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line.strip())
                cwd = obj.get("cwd") or obj.get("workingDirectory") or obj.get("originCwd")
                if cwd:
                    return cwd
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    return None


def derive_work_dir_fallback(project_name):
    """Fallback: guess filesystem path from project folder name.

    Only used when cwd can't be extracted from the JSONL data. This is
    unreliable because hyphens in folder names are ambiguous — the project
    folder name uses -- as the path separator, but single hyphens could be
    either literal hyphens or path separators.
    """
    work_dir = project_name.replace("--", os.sep)
    if sys.platform == "win32":
        if len(work_dir) > 1 and work_dir[1] == os.sep:
            work_dir = work_dir[0] + ":" + work_dir[1:]
    else:
        if work_dir and work_dir[0] != "/":
            work_dir = "/" + work_dir.replace("\\", "/")
    return work_dir


def scan_sessions(claude_dir, project_filter=None):
    """Find all session JSONL files on disk."""
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        print(f"ERROR: No projects directory at {projects_dir}")
        sys.exit(1)

    sessions = []
    pattern = str(projects_dir / "*" / "*.jsonl")

    for filepath in glob.glob(pattern):
        filepath = Path(filepath)
        session_id = filepath.stem
        project = filepath.parent.name

        if project_filter and project_filter.lower() not in project.lower():
            continue

        # Skip non-UUID filenames
        if len(session_id) < 30 or "-" not in session_id:
            continue

        stat = filepath.stat()
        modified = datetime.datetime.fromtimestamp(stat.st_mtime)
        size_kb = stat.st_size // 1024
        preview = extract_preview(filepath)

        # Get actual cwd from session data; fall back to deriving from folder name
        work_dir = extract_cwd(filepath) or derive_work_dir_fallback(project)

        sessions.append({
            "id": session_id,
            "project": project,
            "work_dir": work_dir,
            "date": modified.strftime("%Y-%m-%d %H:%M"),
            "date_sort": modified,
            "size_kb": size_kb,
            "preview": preview,
            "filepath": str(filepath),
        })

    sessions.sort(key=lambda s: s["date_sort"], reverse=True)
    return sessions


# --- Commands ---

def cmd_list(args, claude_dir):
    """List all sessions found on disk."""
    sessions = scan_sessions(claude_dir, args.project)

    if args.json:
        output = [{k: v for k, v in s.items() if k != "date_sort"} for s in sessions]
        print(json.dumps(output, indent=2))
        return

    if not sessions:
        print("No sessions found on disk.")
        return

    print(f"\n{'=' * 100}")
    print(f"  Found {len(sessions)} Claude Code sessions on disk")
    print(f"{'=' * 100}\n")

    for i, s in enumerate(sessions, 1):
        size_str = f"{s['size_kb']}KB" if s['size_kb'] < 1024 else f"{s['size_kb'] / 1024:.1f}MB"
        print(f"  {i:2}. [{s['date']}]  {size_str:>8}  {s['project']}")
        print(f"      Preview: {s['preview'][:90]}")
        print(f"      Resume:  claude --resume {s['id']}")
        print()

    print(f"{'=' * 100}")
    print(f"  To resume any session from CLI:")
    print(f"    cd <project-dir> && claude --resume <session-id>")
    print(f"")
    print(f"  To restore missing sessions to Claude Desktop:")
    print(f"    python recover.py restore")
    print(f"{'=' * 100}\n")


def cmd_restore(args, claude_dir):
    """Restore missing sessions to the Claude Desktop app."""
    sessions = scan_sessions(claude_dir, args.project)

    if not sessions:
        print("No sessions found on disk.")
        return

    # Find the desktop app's session directory
    base_dir, user_dir = find_desktop_sessions_dir()

    if user_dir is None:
        print("ERROR: Could not find Claude Desktop session directory.")
        print("  The desktop app may not be installed, or hasn't been used yet.")
        if base_dir:
            print(f"  Found base dir: {base_dir}")
            print(f"  But no org/user subdirectories exist inside it.")
        print("")
        print("  Make sure Claude Desktop is installed and you've opened at least")
        print("  one Code session through it, then try again.")
        sys.exit(1)

    print(f"Desktop session dir: {user_dir}")

    # Find already-registered session IDs
    registered = set()
    template = None
    for reg_file in user_dir.glob("local_*.json"):
        try:
            with open(reg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            cli_id = data.get("cliSessionId", "")
            if cli_id:
                registered.add(cli_id)
            if template is None:
                template = data  # Use first found as template
        except (json.JSONDecodeError, OSError):
            continue

    # Find sessions that need registration
    to_restore = [s for s in sessions if s["id"] not in registered]

    if not to_restore:
        print(f"\nAll {len(sessions)} sessions are already registered. Nothing to restore.")
        return

    print(f"\n  Total sessions on disk:  {len(sessions)}")
    print(f"  Already registered:      {len(registered)}")
    print(f"  Sessions to restore:     {len(to_restore)}")

    if args.dry_run:
        print(f"\n  DRY RUN - would restore these sessions:\n")
        for i, s in enumerate(to_restore, 1):
            print(f"    {i:2}. [{s['date']}] {s['project']}")
            print(f"        {derive_title(s['preview'])}")
        print(f"\n  Run without --dry-run to restore.")
        return

    # Restore sessions
    print(f"\n  Restoring...\n")
    restored = 0
    for s in to_restore:
        local_id = str(uuid_mod.uuid4())
        title = derive_title(s["preview"])

        stat = Path(s["filepath"]).stat()
        created_ts = int(stat.st_ctime * 1000)
        modified_ts = int(stat.st_mtime * 1000)

        session_data = {
            "sessionId": f"local_{local_id}",
            "cliSessionId": s["id"],
            "cwd": s["work_dir"],
            "originCwd": s["work_dir"],
            "createdAt": created_ts,
            "lastActivityAt": modified_ts,
            "model": template.get("model", "claude-sonnet-4-20250514") if template else "claude-sonnet-4-20250514",
            "effort": template.get("effort", "medium") if template else "medium",
            "isArchived": False,
            "title": title,
            "permissionMode": template.get("permissionMode", "default") if template else "default",
            "completedTurns": 1,
        }

        outpath = user_dir / f"local_{local_id}.json"
        with open(outpath, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2)

        restored += 1
        print(f"    Restored: {title[:55]:55} ({s['id'][:8]}...)")

    print(f"\n  Restored {restored} sessions.")
    print(f"  Restart Claude Desktop to see them.")


def cmd_export(args, claude_dir):
    """Export session transcripts to readable text files."""
    sessions = scan_sessions(claude_dir, args.project)

    if not sessions:
        print("No sessions found.")
        return

    output_dir = Path(args.export_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    exported = 0
    for s in sessions:
        messages = extract_transcript(s["filepath"])
        if not messages:
            continue

        safe_date = s["date"].replace(":", "").replace(" ", "_")
        safe_name = f"{safe_date}_{s['project']}_{s['id'][:8]}.txt"
        outpath = output_dir / safe_name

        with open(outpath, "w", encoding="utf-8") as f:
            f.write(f"Session: {s['id']}\n")
            f.write(f"Project: {s['project']}\n")
            f.write(f"Date:    {s['date']}\n")
            f.write(f"CWD:     {s['work_dir']}\n")
            f.write(f"Preview: {s['preview']}\n")
            f.write(f"{'=' * 80}\n\n")

            for role, content in messages:
                f.write(f"--- {role} ---\n")
                f.write(content)
                f.write("\n\n")

        exported += 1
        print(f"  Exported: {outpath.name}")

    print(f"\nExported {exported} sessions to {output_dir}/")


def cmd_search(args, claude_dir):
    """Search across all sessions for a keyword or pattern."""
    query = args.query
    case_insensitive = not args.case_sensitive

    if args.regex:
        try:
            flags = re.IGNORECASE if case_insensitive else 0
            pattern = re.compile(query, flags)
        except re.error as e:
            print(f"ERROR: Invalid regex pattern: {e}")
            sys.exit(1)
    else:
        pattern = None

    # Build title map from desktop app registration files
    titles = {}
    _, user_dir = find_desktop_sessions_dir()
    if user_dir:
        for reg_file in user_dir.glob("local_*.json"):
            try:
                with open(reg_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cli_id = data.get("cliSessionId", "")
                if cli_id:
                    titles[cli_id] = data.get("title", "")
            except (json.JSONDecodeError, OSError):
                continue

    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        print(f"ERROR: No projects directory at {projects_dir}")
        sys.exit(1)

    results = []
    file_pattern = str(projects_dir / "*" / "*.jsonl")

    for filepath in glob.glob(file_pattern):
        filepath = Path(filepath)
        session_id = filepath.stem
        project = filepath.parent.name

        if len(session_id) < 30 or "-" not in session_id:
            continue

        if args.project and args.project.lower() not in project.lower():
            continue

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            continue

        # Count matches
        if pattern:
            matches = pattern.findall(content)
            match_count = len(matches)
        else:
            search_content = content.lower() if case_insensitive else content
            search_query = query.lower() if case_insensitive else query
            match_count = search_content.count(search_query)

        if match_count == 0:
            continue

        # Extract matching context snippets
        snippets = []
        if pattern:
            for m in pattern.finditer(content):
                start = max(0, m.start() - 60)
                end = min(len(content), m.end() + 60)
                snippet = content[start:end].replace("\n", " ").strip()
                # Highlight the match
                snippet = snippet.replace(m.group(), f"**{m.group()}**")
                snippets.append(snippet)
                if len(snippets) >= args.context_count:
                    break
        else:
            idx = 0
            sq = query.lower() if case_insensitive else query
            sc = content.lower() if case_insensitive else content
            while len(snippets) < args.context_count:
                pos = sc.find(sq, idx)
                if pos == -1:
                    break
                start = max(0, pos - 60)
                end = min(len(content), pos + len(query) + 60)
                snippet = content[start:end].replace("\n", " ").strip()
                # Highlight match (preserve original case)
                match_text = content[pos:pos + len(query)]
                snippet_lower = snippet.lower()
                match_pos = snippet_lower.find(sq)
                if match_pos >= 0:
                    original_match = snippet[match_pos:match_pos + len(query)]
                    snippet = snippet[:match_pos] + f"**{original_match}**" + snippet[match_pos + len(query):]
                snippets.append(snippet)
                idx = pos + len(query)

        stat = filepath.stat()
        modified = datetime.datetime.fromtimestamp(stat.st_mtime)

        title = titles.get(session_id, "")
        if not title:
            title = extract_preview(filepath, max_bytes=10000)[:60]

        results.append({
            "session_id": session_id,
            "project": project,
            "title": title,
            "date": modified.strftime("%Y-%m-%d"),
            "match_count": match_count,
            "snippets": snippets,
        })

    # Sort by match count (most relevant first)
    results.sort(key=lambda r: r["match_count"], reverse=True)

    if not results:
        print(f'\nNo sessions contain "{query}".')
        return

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print(f"\n{'=' * 100}")
    print(f'  Found "{query}" in {len(results)} sessions ({sum(r["match_count"] for r in results)} total matches)')
    print(f"{'=' * 100}\n")

    for i, r in enumerate(results, 1):
        print(f"  {i:2}. [{r['date']}]  {r['match_count']:>4} matches  {r['title'][:60]}")
        print(f"      Project: {r['project']}")
        print(f"      Resume:  claude --resume {r['session_id']}")
        if r["snippets"]:
            for snippet in r["snippets"]:
                # Truncate long snippets for display
                display = snippet[:120]
                if len(snippet) > 120:
                    display += "..."
                print(f"      > {display}")
        print()

    print(f"{'=' * 100}\n")


def extract_transcript(filepath):
    """Extract a readable transcript from a session JSONL file."""
    messages = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.strip())
                    t = obj.get("type", "")
                    role = obj.get("role", "")

                    if t == "queue-operation" and obj.get("operation") == "enqueue":
                        c = obj.get("content", "")
                        if c:
                            messages.append(("Human", c[:5000]))
                    elif t == "human" or role == "human":
                        msg = obj.get("message", {})
                        if isinstance(msg, dict):
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                messages.append(("Human", content[:5000]))
                            elif isinstance(content, list):
                                texts = [c["text"] for c in content
                                         if isinstance(c, dict) and c.get("type") == "text"]
                                if texts:
                                    messages.append(("Human", "\n".join(texts)[:5000]))
                    elif t == "assistant" or role == "assistant":
                        msg = obj.get("message", {})
                        if isinstance(msg, dict):
                            content = msg.get("content", "")
                            if isinstance(content, str):
                                messages.append(("Assistant", content[:5000]))
                            elif isinstance(content, list):
                                texts = [c["text"] for c in content
                                         if isinstance(c, dict) and c.get("type") == "text"]
                                if texts:
                                    messages.append(("Assistant", "\n".join(texts)[:5000]))

                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    return messages


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Recover Claude Code sessions that disappeared from Claude Desktop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python recover.py list                      List all sessions on disk
  python recover.py list --json               Output as JSON for scripting
  python recover.py list --project "website"  Filter by project name
  python recover.py search "voicebox"         Find sessions mentioning voicebox
  python recover.py search "API key" -n 5     Show 5 context snippets per match
  python recover.py search "def.*main" -r     Search with regex
  python recover.py restore                   Re-register sessions in Desktop app
  python recover.py restore --dry-run         Preview what would be restored
  python recover.py export                    Export transcripts to text files
        """,
    )

    parser.add_argument("--claude-dir", help="Override ~/.claude directory path")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # list
    list_parser = subparsers.add_parser("list", help="List all sessions found on disk")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")
    list_parser.add_argument("--project", help="Filter by project name substring")

    # restore
    restore_parser = subparsers.add_parser("restore", help="Restore missing sessions to Desktop app")
    restore_parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    restore_parser.add_argument("--project", help="Filter by project name substring")

    # search
    search_parser = subparsers.add_parser("search", help="Search across all sessions")
    search_parser.add_argument("query", help="Search term or regex pattern")
    search_parser.add_argument("-r", "--regex", action="store_true", help="Treat query as regex")
    search_parser.add_argument("-c", "--case-sensitive", action="store_true", help="Case-sensitive search")
    search_parser.add_argument("-n", "--context-count", type=int, default=3, help="Number of context snippets per session (default: 3)")
    search_parser.add_argument("--project", help="Filter by project name substring")
    search_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # export
    export_parser = subparsers.add_parser("export", help="Export session transcripts to text files")
    export_parser.add_argument("--export-dir", default="./exported-sessions", help="Output directory")
    export_parser.add_argument("--project", help="Filter by project name substring")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    claude_dir = Path(args.claude_dir) if args.claude_dir else find_claude_dir()

    if args.command == "list":
        cmd_list(args, claude_dir)
    elif args.command == "search":
        cmd_search(args, claude_dir)
    elif args.command == "restore":
        cmd_restore(args, claude_dir)
    elif args.command == "export":
        cmd_export(args, claude_dir)


if __name__ == "__main__":
    main()
