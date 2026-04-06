#!/usr/bin/env python3
"""
design-producer.py - Product brief to design spec via 5-step LLM prompt chain.

Runs: DECOMPOSE -> DESIGN -> STITCH -> CRITIQUE -> REFINE
Output: JSON design spec with screens, flow_map, user_stories, edge_cases, design_notes.

Dependencies: anthropic SDK (sync), Python 3.10+ stdlib.

API key sourcing (in order):
  1. --api-key flag
  2. ANTHROPIC_API_KEY env var
  3. OpenClaw auth-profiles.json (auto-detected from ~/.openclaw)

OAuth tokens (sk-ant-oat*) are auto-detected and the required beta headers
are added automatically.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import anthropic
except ImportError:
    print("Error: 'anthropic' package required. Install with: pip install anthropic", file=sys.stderr)
    sys.exit(1)

DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 32000

# Beta headers required for OAuth access tokens
OAUTH_BETA_HEADERS = [
    "claude-code-20250219",
    "oauth-2025-04-20",
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
]

# Anthropic OAuth token exchange endpoint
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

PROMPT_DECOMPOSE = """\
You are a senior product designer. Given the product brief below, decompose it into:

1. **user_roles** - list of distinct user roles with name, description, permissions summary
2. **workflows** - list of end-to-end workflows each role performs (name, steps, role)
3. **screen_hierarchy** - tree of screens needed (id, title, parent_id or null, purpose, primary_role)

Output ONLY valid JSON with keys: user_roles, workflows, screen_hierarchy.
No markdown fences, no commentary.

<brief>
{brief}
</brief>
"""

PROMPT_DESIGN = """\
You are a UI/UX designer. Given the screen hierarchy and workflows below, produce detailed per-screen specs.

For each screen, output:
- screen_id, title, route (URL path)
- layout: description of spatial arrangement (e.g. "sidebar + main content")
- components: list of UI components, each with:
  - type (one of: sidebar, navbar, table, card-grid, form, modal, breadcrumbs, tabs, buttons, text-block, search-bar, stat-card)
  - id, label, position (top/left/center/right/bottom), props (key-value pairs relevant to the component)
- interactions: list of user interactions (trigger, action, target_screen or null)
- responsive_notes: brief notes on mobile/tablet behavior

Output ONLY valid JSON with key "screens" containing the array.
No markdown fences, no commentary.

<decomposition>
{decomposition}
</decomposition>
"""

PROMPT_STITCH = """\
You are an information architect. Given the screen specs below, produce a coherent flow map.

Output JSON with:
- flow_map: list of edges, each with: from_screen, to_screen, trigger (what user does), condition (optional)
- navigation_structure: the primary nav items (label, screen_id, icon_hint, children[])
- entry_points: list of screens reachable without auth or as landing pages
- user_stories: list of user stories in format "As a [role], I want to [action] so that [benefit]"

Output ONLY valid JSON. No markdown fences, no commentary.

<screens>
{screens}
</screens>
"""

PROMPT_CRITIQUE = """\
You are a senior UX reviewer and accessibility specialist. Review the full design spec below.

Evaluate:
1. UX issues - confusing flows, dead ends, missing feedback, cognitive overload
2. Accessibility - WCAG 2.1 AA compliance gaps, keyboard nav, color contrast, screen reader
3. Consistency - naming inconsistencies, duplicate patterns, missing shared components
4. Edge cases - empty states, error states, loading states, permission boundaries
5. Missing screens - any obvious screens or modals not accounted for

For each issue, provide:
- severity: critical / major / minor
- category: ux / accessibility / consistency / edge_case / missing
- screen_id (or "global")
- description
- recommendation

Output ONLY valid JSON with keys: issues, edge_cases (list of edge case scenarios to handle), summary (string).
No markdown fences, no commentary.

<design>
{design}
</design>
"""

PROMPT_REFINE = """\
You are a senior product designer finalizing a design spec. Apply the critique below to the original design.

Rules:
- Fix all critical and major issues
- Address minor issues where straightforward
- Add missing edge case handling to relevant screens (empty states, error states, loading)
- Ensure navigation consistency
- Do NOT remove screens or features - only improve them
- Add design_notes explaining key decisions made during refinement

Output the COMPLETE final design as valid JSON with these exact keys:
- screens: array of refined screen specs (same format as input, with fixes applied)
- flow_map: refined flow edges
- user_stories: refined user stories
- edge_cases: list of edge case objects (scenario, screen_id, handling)
- design_notes: list of design decision strings
- metadata: object with model, timestamp, brief_hash (first 8 chars of brief content hash), steps_completed

Output ONLY valid JSON. No markdown fences, no commentary.

<original_design>
{original_design}
</original_design>

<critique>
{critique}
</critique>
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str, verbose: bool) -> None:
    if verbose:
        print(f"[design-producer] {msg}", file=sys.stderr)


def call_llm(client: anthropic.Anthropic, model: str, prompt: str, step_name: str,
             verbose: bool, is_oauth: bool = False) -> dict:
    """Call Anthropic API and parse JSON response."""
    log(f"Step {step_name}: sending request to {model}...", verbose)
    t0 = time.time()

    kwargs: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "timeout": 600.0,
        "messages": [{"role": "user", "content": prompt}],
    }

    # OAuth tokens require Claude Code system prompt identity
    if is_oauth:
        kwargs["system"] = [
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": "Output ONLY valid JSON. No markdown fences, no commentary."},
        ]

    response = client.messages.create(**kwargs)

    text = response.content[0].text.strip()
    elapsed = time.time() - t0
    log(f"Step {step_name}: received response ({len(text)} chars, {elapsed:.1f}s)", verbose)

    # Strip markdown fences if model adds them despite instructions
    import re
    fence_match = re.match(r'^```(?:json)?\s*\n(.*?)```\s*$', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    elif text.startswith("```"):
        first_nl = text.index("\n")
        last_fence = text.rfind("```")
        if last_fence > first_nl:
            text = text[first_nl + 1:last_fence].strip()
        else:
            # No closing fence (truncated response) - just strip the opening line
            text = text[first_nl + 1:].strip()

    # Handle stop_reason=max_tokens: if response was truncated, warn
    if response.stop_reason == "max_tokens":
        log(f"WARNING: Step {step_name} hit max_tokens limit - output may be truncated", True)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        log(f"Step {step_name}: JSON parse error - {e}", True)
        log(f"Raw output (first 500 chars): {text[:500]}", True)
        raise SystemExit(f"Step {step_name} returned invalid JSON. Aborting.")


def compute_brief_hash(brief: str) -> str:
    import hashlib
    return hashlib.sha256(brief.encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def resolve_api_key(explicit_key: str | None, verbose: bool) -> str:
    """Resolve Anthropic API key from explicit flag, env var, or OpenClaw auth-profiles."""
    if explicit_key:
        return explicit_key

    env_key = os.environ.get("ANTHROPIC_API_KEY")
    if env_key:
        return env_key

    # Try OpenClaw auth-profiles.json
    openclaw_home = os.environ.get("OPENCLAW_HOME", os.path.expanduser("~/.openclaw"))
    profiles_paths = [
        Path(openclaw_home) / "agents" / "main" / "agent" / "auth-profiles.json",
    ]
    for pp in profiles_paths:
        if pp.exists():
            try:
                data = json.loads(pp.read_text())
                profiles = data.get("profiles", {})
                for name, profile in profiles.items():
                    if "anthropic" in name.lower() and profile.get("token"):
                        if verbose:
                            print(f"[design-producer] Using API key from {pp} ({name})", file=sys.stderr)
                        return profile["token"]
            except (json.JSONDecodeError, KeyError):
                pass

    print("Error: No Anthropic API key found. Set ANTHROPIC_API_KEY, pass --api-key, "
          "or ensure OpenClaw auth-profiles.json exists.", file=sys.stderr)
    sys.exit(1)


def create_client(api_key: str, verbose: bool) -> tuple[anthropic.Anthropic, bool]:
    """Create Anthropic client. Returns (client, is_oauth).

    OAuth tokens (sk-ant-oat*) use Bearer auth with Claude Code identity headers.
    Regular API keys use x-api-key auth.
    """
    is_oauth = api_key.startswith("sk-ant-oat")
    if is_oauth:
        if verbose:
            print("[design-producer] OAuth token detected, using Bearer auth with Claude Code headers", file=sys.stderr)
        beta = ",".join(OAUTH_BETA_HEADERS)
        client = anthropic.Anthropic(
            api_key=None,
            auth_token=api_key,
            default_headers={
                "anthropic-beta": beta,
                "user-agent": "claude-cli/2.1.75",
                "x-app": "cli",
                "accept": "application/json",
                "anthropic-dangerous-direct-browser-access": "true",
            },
        )
        return client, True
    return anthropic.Anthropic(api_key=api_key), False


def run_pipeline(brief: str, model: str, api_key: str, verbose: bool) -> dict:
    """Execute the 5-step design pipeline."""
    client, is_oauth = create_client(api_key, verbose)

    # Step 1: DECOMPOSE
    decomposition = call_llm(
        client, model,
        PROMPT_DECOMPOSE.format(brief=brief),
        "DECOMPOSE", verbose, is_oauth=is_oauth,
    )
    log(f"DECOMPOSE: {len(decomposition.get('screen_hierarchy', []))} screens, "
        f"{len(decomposition.get('workflows', []))} workflows", verbose)

    # Step 2: DESIGN
    screens_result = call_llm(
        client, model,
        PROMPT_DESIGN.format(decomposition=json.dumps(decomposition, indent=2)),
        "DESIGN", verbose, is_oauth=is_oauth,
    )

    # Step 3: STITCH
    stitch_result = call_llm(
        client, model,
        PROMPT_STITCH.format(screens=json.dumps(screens_result, indent=2)),
        "STITCH", verbose, is_oauth=is_oauth,
    )

    # Merge for full design spec
    full_design = {
        "screens": screens_result.get("screens", []),
        "flow_map": stitch_result.get("flow_map", []),
        "user_stories": stitch_result.get("user_stories", []),
        "navigation_structure": stitch_result.get("navigation_structure", []),
        "entry_points": stitch_result.get("entry_points", []),
    }

    # Step 4: CRITIQUE
    critique = call_llm(
        client, model,
        PROMPT_CRITIQUE.format(design=json.dumps(full_design, indent=2)),
        "CRITIQUE", verbose, is_oauth=is_oauth,
    )
    log(f"CRITIQUE: {len(critique.get('issues', []))} issues found", verbose)

    # Step 5: REFINE
    try:
        refined = call_llm(
            client, model,
            PROMPT_REFINE.format(
                original_design=json.dumps(full_design, indent=2),
                critique=json.dumps(critique, indent=2),
            ),
            "REFINE", verbose, is_oauth=is_oauth,
        )
        steps_completed = ["DECOMPOSE", "DESIGN", "STITCH", "CRITIQUE", "REFINE"]
    except SystemExit:
        log("REFINE step failed (likely truncated output). Using pre-REFINE design with critique notes.", True)
        refined = full_design
        refined["design_notes"] = critique.get("issues", [])
        refined["edge_cases"] = critique.get("edge_cases", [])
        refined["user_stories"] = full_design.get("user_stories", [])
        steps_completed = ["DECOMPOSE", "DESIGN", "STITCH", "CRITIQUE", "REFINE_SKIPPED"]

    # Ensure metadata
    if "metadata" not in refined:
        refined["metadata"] = {}
    refined["metadata"].update({
        "model": model,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "brief_hash": compute_brief_hash(brief),
        "steps_completed": steps_completed,
        "critique_summary": critique.get("summary", ""),
        "issues_found": len(critique.get("issues", [])),
    })

    return refined


# ---------------------------------------------------------------------------
# Wireframe integration
# ---------------------------------------------------------------------------

def run_wireframes(design: dict, output_dir: Path, viewport: str, verbose: bool) -> list[str]:
    """Invoke wireframe-gen.py for each screen in the design."""
    script_dir = Path(__file__).parent
    wireframe_script = script_dir / "wireframe-gen.py"

    if not wireframe_script.exists():
        log(f"Warning: wireframe-gen.py not found at {wireframe_script}", True)
        return []

    # Write screen specs to temp file
    spec_file = output_dir / "_design_spec.json"
    spec_file.write_text(json.dumps(design, indent=2))

    cmd = [
        sys.executable, str(wireframe_script),
        "--input", str(spec_file),
        "--output-dir", str(output_dir / "wireframes"),
        "--viewport", viewport,
    ]

    log(f"Running wireframe generator: {' '.join(cmd)}", verbose)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"Wireframe generation failed: {result.stderr}", True)
        return []

    # Collect generated SVG files
    wf_dir = output_dir / "wireframes"
    if wf_dir.exists():
        svgs = sorted(str(p) for p in wf_dir.glob("*.svg"))
        log(f"Generated {len(svgs)} wireframe SVGs", verbose)
        return svgs
    return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a design spec from a product brief using a 5-step LLM prompt chain.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Steps:
  1. DECOMPOSE  - Break brief into workflows, user roles, screen hierarchy
  2. DESIGN     - Per-screen element specs (layout, components, interactions)
  3. STITCH     - Coherent flow map with navigation structure
  4. CRITIQUE   - UX issues, accessibility, consistency review
  5. REFINE     - Apply critique fixes, output final design

Output JSON keys: screens, flow_map, user_stories, edge_cases, design_notes, metadata

API key is resolved from (in order): --api-key flag, ANTHROPIC_API_KEY env var,
or OpenClaw auth-profiles.json (~/.openclaw/agents/main/agent/auth-profiles.json).

OAuth tokens (sk-ant-oat*) are auto-detected and required beta headers are added.
""",
    )
    parser.add_argument("--brief", required=True, help="Path to product brief file (text/markdown)")
    parser.add_argument("--output-dir", default=".", help="Directory for output files (default: current dir)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Anthropic model to use (default: {DEFAULT_MODEL})")
    parser.add_argument("--api-key", default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument("--wireframes", action="store_true", help="Also generate SVG wireframes for each screen")
    parser.add_argument("--viewport", default="1440x900", help="Viewport for wireframes (default: 1440x900)")
    parser.add_argument("--verbose", action="store_true", help="Print progress to stderr")

    args = parser.parse_args()

    # Read brief
    brief_path = Path(args.brief)
    if not brief_path.exists():
        print(f"Error: Brief file not found: {brief_path}", file=sys.stderr)
        sys.exit(1)

    brief = brief_path.read_text().strip()
    if not brief:
        print("Error: Brief file is empty.", file=sys.stderr)
        sys.exit(1)

    log(f"Brief loaded: {len(brief)} chars from {brief_path}", args.verbose)

    # Run pipeline
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    api_key = resolve_api_key(args.api_key, args.verbose)
    design = run_pipeline(brief, args.model, api_key, args.verbose)

    # Save JSON output
    output_file = output_dir / "design-spec.json"
    output_file.write_text(json.dumps(design, indent=2))
    log(f"Design spec written to {output_file}", args.verbose)

    # Generate wireframes if requested
    if args.wireframes:
        svgs = run_wireframes(design, output_dir, args.viewport, args.verbose)
        if svgs:
            design["metadata"]["wireframes"] = svgs

            # Re-save with wireframe paths
            output_file.write_text(json.dumps(design, indent=2))

    # Also print to stdout
    print(json.dumps(design, indent=2))


if __name__ == "__main__":
    main()
