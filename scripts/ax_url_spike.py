#!/usr/bin/env python3
"""Phase 0: AX URL Extraction Spike

Validates feasibility of extracting browser URLs via macOS Accessibility API.
Tests against multiple browsers and documents the exact AX path to the URL bar.

Usage:
    1. Open a browser window to any URL (e.g., github.com)
    2. Run: python scripts/ax_url_spike.py
    3. The script gives you 3 seconds to focus the browser, then probes the AX tree
    4. Switch browsers and run again to test each browser family

    For a specific browser test:
        python scripts/ax_url_spike.py --browser safari
        python scripts/ax_url_spike.py --browser chrome

    To dump the full AX tree (verbose):
        python scripts/ax_url_spike.py --dump

    To test all strategies without the 3s delay (browser already focused):
        python scripts/ax_url_spike.py --no-delay
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import ApplicationServices
    import AppKit
    import Quartz
except ImportError:
    print("ERROR: Requires macOS with pyobjc installed.")
    print("  pip install pyobjc-framework-ApplicationServices pyobjc-framework-AppKit pyobjc-framework-Quartz")
    sys.exit(1)


# --- Known browser bundle IDs (from context.py:258-271) ---
BROWSER_FAMILIES = {
    "safari": {"com.apple.Safari", "com.apple.SafariTechnologyPreview"},
    "chrome": {"com.google.Chrome", "com.google.Chrome.canary"},
    "brave": {"com.brave.Browser"},
    "edge": {"com.microsoft.edgemac"},
    "arc": {"company.thebrowser.Browser"},
    "firefox": {"org.mozilla.firefox", "org.mozilla.firefoxdeveloperedition"},
    "chromium_other": {
        "com.vivaldi.Vivaldi",
        "com.operasoftware.Opera",
    },
}

ALL_BROWSER_BUNDLES = set()
for ids in BROWSER_FAMILIES.values():
    ALL_BROWSER_BUNDLES |= ids

CHROMIUM_BUNDLES = (
    BROWSER_FAMILIES["chrome"]
    | BROWSER_FAMILIES["brave"]
    | BROWSER_FAMILIES["edge"]
    | BROWSER_FAMILIES["arc"]
    | BROWSER_FAMILIES["chromium_other"]
)


@dataclass
class ExtractionResult:
    browser: str
    bundle_id: str
    strategy: str
    url: str | None
    latency_ms: float
    ax_path: str
    notes: str = ""


@dataclass
class SpikeReport:
    timestamp: str = ""
    results: list[ExtractionResult] = field(default_factory=list)
    tree_dump: dict | None = None
    errors: list[str] = field(default_factory=list)


def get_frontmost_app() -> tuple[int, str | None]:
    """Get PID and bundle ID of frontmost app."""
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    pid = app.processIdentifier()
    bundle_id = str(app.bundleIdentifier()) if app.bundleIdentifier() else None
    return pid, bundle_id


def get_focused_window(pid: int) -> ApplicationServices.AXUIElementRef | None:
    """Get AXFocusedWindow for a PID."""
    app_ref = ApplicationServices.AXUIElementCreateApplication(pid)
    ApplicationServices.AXUIElementSetMessagingTimeout(app_ref, 5.0)
    err, window = ApplicationServices.AXUIElementCopyAttributeValue(
        app_ref, "AXFocusedWindow", None
    )
    if err != 0:
        return None
    return window


def get_attr(element, attr_name: str):
    """Read a single AX attribute. Returns None on failure."""
    err, value = ApplicationServices.AXUIElementCopyAttributeValue(
        element, attr_name, None
    )
    if err != 0 or value is None:
        return None
    # Convert NSURL to string
    try:
        if hasattr(value, "absoluteString"):
            return str(value.absoluteString())
    except Exception:
        pass
    # Convert NSString
    try:
        return str(value)
    except Exception:
        return value


def get_children(element) -> list:
    """Get AXChildren of an element."""
    err, children = ApplicationServices.AXUIElementCopyAttributeValue(
        element, "AXChildren", None
    )
    if err != 0 or children is None:
        return []
    return list(children)


def get_role(element) -> str | None:
    return get_attr(element, "AXRole")


def get_subrole(element) -> str | None:
    return get_attr(element, "AXSubrole")


def get_value(element) -> str | None:
    return get_attr(element, "AXValue")


def get_identifier(element) -> str | None:
    return get_attr(element, "AXIdentifier")


def get_description(element) -> str | None:
    return get_attr(element, "AXDescription")


def get_title(element) -> str | None:
    return get_attr(element, "AXTitle")


# =============================================================================
# Strategy 1: AXDocument on the window itself
# =============================================================================

def strategy_ax_document(window) -> tuple[str | None, str]:
    """Try reading AXDocument attribute directly from the window."""
    url = get_attr(window, "AXDocument")
    return url, "AXWindow → AXDocument"


def strategy_ax_url(window) -> tuple[str | None, str]:
    """Try reading AXURL attribute directly from the window."""
    url = get_attr(window, "AXURL")
    if url and hasattr(url, "absoluteString"):
        url = str(url.absoluteString())
    return url, "AXWindow → AXURL"


# =============================================================================
# Strategy 2: Walk toolbar looking for URL text field
# =============================================================================

def find_toolbar_url(window, max_depth: int = 4) -> tuple[str | None, str]:
    """Walk the AX tree looking for toolbar URL field."""
    # First, look for AXToolbar children
    children = get_children(window)

    for child in children:
        role = get_role(child)

        # Direct toolbar
        if role == "AXToolbar":
            url, path = _search_toolbar_for_url(child, "AXWindow → AXToolbar", depth=0, max_depth=max_depth)
            if url:
                return url, path

        # AXGroup that might contain toolbar (Chrome wraps things)
        if role == "AXGroup":
            grandchildren = get_children(child)
            for gc in grandchildren:
                if get_role(gc) == "AXToolbar":
                    url, path = _search_toolbar_for_url(gc, "AXWindow → AXGroup → AXToolbar", depth=0, max_depth=max_depth)
                    if url:
                        return url, path

    return None, "AXToolbar search (not found)"


def _search_toolbar_for_url(
    element, path_prefix: str, depth: int, max_depth: int
) -> tuple[str | None, str]:
    """Recursively search a toolbar subtree for URL-bearing elements."""
    if depth > max_depth:
        return None, path_prefix

    role = get_role(element) or ""
    identifier = get_identifier(element) or ""
    description = get_description(element) or ""
    subrole = get_subrole(element) or ""

    # Check if this element looks like a URL bar
    is_url_field = False
    reason = ""

    # Chromium: AXTextField with identifier containing "address" or "omnibox"
    if role == "AXTextField" and any(
        kw in identifier.lower() for kw in ("address", "omnibox", "url", "location")
    ):
        is_url_field = True
        reason = f"identifier={identifier}"

    # Safari: AXTextField in toolbar, description is "smart search field"
    elif role == "AXTextField" and any(
        kw in description.lower() for kw in ("address", "url", "search", "smart search")
    ):
        is_url_field = True
        reason = f"description={description}"

    # Chromium: AXComboBox (omnibox is sometimes a combobox)
    elif role == "AXComboBox" and any(
        kw in identifier.lower() for kw in ("address", "omnibox", "url")
    ):
        is_url_field = True
        reason = f"combobox identifier={identifier}"

    # Generic: AXWebArea can have AXDocument
    elif role == "AXWebArea":
        url = get_attr(element, "AXURL")
        if url:
            if hasattr(url, "absoluteString"):
                url = str(url.absoluteString())
            return str(url), f"{path_prefix} → AXWebArea → AXURL"

    if is_url_field:
        value = get_value(element)
        current_path = f"{path_prefix} → {role}[{reason}] → AXValue"
        if value:
            return str(value), current_path
        else:
            # Element found but value is empty (e.g., Start Page, new tab)
            return "(FOUND_EMPTY)", current_path

    # Recurse into children
    for child in get_children(element):
        child_role = get_role(child) or "?"
        url, path = _search_toolbar_for_url(
            child,
            f"{path_prefix} → {child_role}",
            depth + 1,
            max_depth,
        )
        if url:
            return url, path

    return None, path_prefix


# =============================================================================
# Strategy 3: Find AXWebArea and read AXURL (bypasses toolbar)
# =============================================================================

def strategy_web_area_url(window, max_depth: int = 6) -> tuple[str | None, str]:
    """Walk tree looking for AXWebArea and read its AXURL."""
    return _find_web_area_url(window, "AXWindow", depth=0, max_depth=max_depth)


def _find_web_area_url(element, path: str, depth: int, max_depth: int) -> tuple[str | None, str]:
    if depth > max_depth:
        return None, path

    role = get_role(element) or ""

    if role == "AXWebArea":
        url = get_attr(element, "AXURL")
        if url:
            if hasattr(url, "absoluteString"):
                url = str(url.absoluteString())
            return str(url), f"{path} → AXWebArea.AXURL"
        # Also try AXDocument
        url = get_attr(element, "AXDocument")
        if url:
            return str(url), f"{path} → AXWebArea.AXDocument"

    for child in get_children(element):
        child_role = get_role(child) or "?"
        url, found_path = _find_web_area_url(
            child, f"{path} → {child_role}", depth + 1, max_depth
        )
        if url:
            return url, found_path

    return None, path


# =============================================================================
# Incognito / Private Browsing Detection
# =============================================================================

def detect_incognito(pid: int, bundle_id: str, window) -> bool:
    """Detect if the browser window is in incognito/private mode."""
    title = get_attr(window, "AXTitle") or ""

    # Chromium family: title ends with "(Incognito)"
    if bundle_id in CHROMIUM_BUNDLES:
        if "(Incognito)" in title or "(Private)" in title:
            return True

    # Safari: title contains "Private Browsing"
    if bundle_id in BROWSER_FAMILIES["safari"]:
        if "Private Browsing" in title:
            return True

    # Firefox: title ends with "(Private Browsing)"
    if bundle_id in BROWSER_FAMILIES["firefox"]:
        if "(Private Browsing)" in title or "Private Browsing" in title:
            return True

    return False


# =============================================================================
# Full AX Tree Dump (for debugging)
# =============================================================================

def dump_ax_tree(element, max_depth: int = 5, depth: int = 0) -> dict:
    """Dump AX tree as a JSON-serializable dict."""
    if depth > max_depth:
        return {"_truncated": True}

    node = {}
    for attr in ["AXRole", "AXSubrole", "AXIdentifier", "AXDescription",
                  "AXTitle", "AXValue", "AXRoleDescription", "AXURL", "AXDocument",
                  "AXPlaceholderValue"]:
        val = get_attr(element, attr)
        if val is not None:
            # Truncate long values
            s = str(val)
            if len(s) > 200:
                s = s[:200] + "..."
            node[attr] = s

    children = get_children(element)
    if children:
        node["_children"] = [
            dump_ax_tree(child, max_depth, depth + 1)
            for child in children[:20]  # limit children to prevent explosion
        ]

    return node


# =============================================================================
# Main spike runner
# =============================================================================

def identify_browser(bundle_id: str) -> str:
    """Map bundle ID to browser family name."""
    for family, ids in BROWSER_FAMILIES.items():
        if bundle_id in ids:
            return family
    return "unknown"


def run_spike(dump_tree: bool = False, delay: bool = True) -> SpikeReport:
    report = SpikeReport(timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"))

    if delay:
        print("⏳ Focus a browser window within 3 seconds...")
        time.sleep(3)

    pid, bundle_id = get_frontmost_app()
    browser = identify_browser(bundle_id or "")

    print(f"\n{'='*60}")
    print(f"  Browser: {browser}")
    print(f"  Bundle ID: {bundle_id}")
    print(f"  PID: {pid}")
    print(f"{'='*60}\n")

    if bundle_id not in ALL_BROWSER_BUNDLES:
        msg = f"Frontmost app '{bundle_id}' is not a known browser. Focus a browser window first."
        print(f"⚠️  {msg}")
        report.errors.append(msg)
        return report

    window = get_focused_window(pid)
    if window is None:
        msg = "Could not get AXFocusedWindow"
        print(f"❌ {msg}")
        report.errors.append(msg)
        return report

    # Check incognito
    is_incognito = detect_incognito(pid, bundle_id, window)
    if is_incognito:
        print("🔒 INCOGNITO/PRIVATE window detected — URL extraction should return None")
        print()

    # Window title
    title = get_attr(window, "AXTitle") or "(no title)"
    print(f"  Window title: {title}")
    print()

    # Run all strategies
    strategies = [
        ("AXDocument on window", lambda: strategy_ax_document(window)),
        ("AXURL on window", lambda: strategy_ax_url(window)),
        ("Toolbar URL field search", lambda: find_toolbar_url(window, max_depth=4)),
        ("AXWebArea AXURL (deep)", lambda: strategy_web_area_url(window, max_depth=6)),
    ]

    print(f"  {'Strategy':<30} {'Latency':>10} {'Result'}")
    print(f"  {'-'*30} {'-'*10} {'-'*40}")

    for name, strategy_fn in strategies:
        t0 = time.perf_counter()
        try:
            url, ax_path = strategy_fn()
        except Exception as e:
            url, ax_path = None, f"ERROR: {e}"
        latency = (time.perf_counter() - t0) * 1000

        if url == "(FOUND_EMPTY)":
            status = "🔍"
            display_url = "(element found, AXValue empty — navigate to a URL first)"
        elif url:
            status = "✅"
            display_url = url[:60] + "..." if len(url) > 60 else url
        else:
            status = "❌"
            display_url = "(None)"
        print(f"  {status} {name:<28} {latency:>8.1f}ms  {display_url}")

        result = ExtractionResult(
            browser=browser,
            bundle_id=bundle_id or "",
            strategy=name,
            url=url,
            latency_ms=round(latency, 2),
            ax_path=ax_path,
            notes="incognito" if is_incognito else "",
        )
        report.results.append(result)

    print()

    # Summary
    successful = [r for r in report.results if r.url and r.url != "(FOUND_EMPTY)"]
    found_empty = [r for r in report.results if r.url == "(FOUND_EMPTY)"]
    if successful:
        best = min(successful, key=lambda r: r.latency_ms)
        print(f"  🏆 Best strategy: {best.strategy}")
        print(f"     Path: {best.ax_path}")
        print(f"     URL:  {best.url}")
        print(f"     Latency: {best.latency_ms:.1f}ms")
    elif found_empty:
        best = found_empty[0]
        print(f"  🔍 Address bar FOUND but empty (Start Page / new tab)")
        print(f"     Path: {best.ax_path}")
        print(f"     Navigate to a URL (e.g., github.com) and re-run.")
    else:
        print("  ⚠️  No strategy succeeded. Use --dump to inspect the AX tree.")

    # Optional tree dump
    if dump_tree:
        print("\n📋 Full AX Tree Dump (depth=5):")
        tree = dump_ax_tree(window, max_depth=5)
        report.tree_dump = tree
        print(json.dumps(tree, indent=2, default=str))

    return report


def main():
    parser = argparse.ArgumentParser(description="AX URL Extraction Spike")
    parser.add_argument("--dump", action="store_true", help="Dump full AX tree")
    parser.add_argument("--no-delay", action="store_true", help="Skip 3s delay")
    parser.add_argument("--browser", help="Expected browser family (for documentation)")
    parser.add_argument("--save", help="Save report to JSON file")
    args = parser.parse_args()

    report = run_spike(dump_tree=args.dump, delay=not args.no_delay)

    if args.save:
        path = Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(report), f, indent=2, default=str)
        print(f"\n📄 Report saved to {path}")

    # Exit code: 0 if any strategy got a URL (including FOUND_EMPTY), 1 if none
    any_found = [r for r in report.results if r.url]
    sys.exit(0 if any_found else 1)


if __name__ == "__main__":
    main()
