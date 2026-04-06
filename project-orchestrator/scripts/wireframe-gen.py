#!/usr/bin/env python3
"""
wireframe-gen.py - Generate SVG wireframe files from a design spec JSON.

Takes screen spec JSON (output of design-producer.py), outputs one SVG per screen.
Zero external dependencies - uses only xml.etree.ElementTree and stdlib.

Supported components: sidebar, navbar, table, card-grid, form, modal,
breadcrumbs, tabs, buttons, text-block, search-bar, stat-card.
"""

import argparse
import json
import sys
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COLORS = {
    "bg": "#FFFFFF",
    "border": "#D1D5DB",
    "text": "#374151",
    "text_light": "#9CA3AF",
    "primary": "#3B82F6",
    "primary_light": "#DBEAFE",
    "surface": "#F9FAFB",
    "sidebar_bg": "#1F2937",
    "sidebar_text": "#F9FAFB",
    "navbar_bg": "#FFFFFF",
    "card_bg": "#FFFFFF",
    "input_bg": "#FFFFFF",
    "modal_overlay": "#00000066",
    "accent": "#10B981",
    "danger": "#EF4444",
}

FONT = "system-ui, -apple-system, sans-serif"

# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------

def make_svg(width: int, height: int) -> Element:
    svg = Element("svg")
    svg.set("xmlns", "http://www.w3.org/2000/svg")
    svg.set("width", str(width))
    svg.set("height", str(height))
    svg.set("viewBox", f"0 0 {width} {height}")

    # Background
    bg = SubElement(svg, "rect")
    bg.set("width", str(width))
    bg.set("height", str(height))
    bg.set("fill", COLORS["bg"])

    return svg


def add_rect(parent: Element, x: int, y: int, w: int, h: int,
             fill: str = "none", stroke: str = COLORS["border"],
             rx: int = 4, stroke_width: int = 1) -> Element:
    rect = SubElement(parent, "rect")
    rect.set("x", str(x))
    rect.set("y", str(y))
    rect.set("width", str(w))
    rect.set("height", str(h))
    rect.set("fill", fill)
    rect.set("stroke", stroke)
    rect.set("stroke-width", str(stroke_width))
    rect.set("rx", str(rx))
    return rect


def add_text(parent: Element, x: int, y: int, text: str,
             size: int = 14, fill: str = COLORS["text"],
             anchor: str = "start", weight: str = "normal") -> Element:
    t = SubElement(parent, "text")
    t.set("x", str(x))
    t.set("y", str(y))
    t.set("font-family", FONT)
    t.set("font-size", str(size))
    t.set("fill", fill)
    t.set("text-anchor", anchor)
    t.set("font-weight", weight)
    t.text = text
    return t


def add_line(parent: Element, x1: int, y1: int, x2: int, y2: int,
             stroke: str = COLORS["border"], width: int = 1) -> None:
    line = SubElement(parent, "line")
    line.set("x1", str(x1))
    line.set("y1", str(y1))
    line.set("x2", str(x2))
    line.set("y2", str(y2))
    line.set("stroke", stroke)
    line.set("stroke-width", str(width))


# ---------------------------------------------------------------------------
# Component renderers
# ---------------------------------------------------------------------------

def render_sidebar(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a vertical sidebar."""
    add_rect(svg, x, y, w, h, fill=COLORS["sidebar_bg"], stroke="none", rx=0)

    # Logo area
    add_rect(svg, x + 16, y + 16, w - 32, 32, fill="#374151", stroke="none", rx=4)
    add_text(svg, x + w // 2, y + 37, comp.get("label", "App"),
             size=14, fill=COLORS["sidebar_text"], anchor="middle", weight="bold")

    # Menu items
    items = comp.get("props", {}).get("items", ["Dashboard", "Settings"])
    if isinstance(items, str):
        items = [items]
    iy = y + 72
    for item in items[:12]:
        label = item if isinstance(item, str) else item.get("label", "Item")
        add_rect(svg, x + 8, iy - 4, w - 16, 32, fill="none", stroke="none")
        add_text(svg, x + 24, iy + 16, label, size=13, fill=COLORS["sidebar_text"])
        iy += 40


def render_navbar(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a horizontal navigation bar."""
    nav_h = 56
    add_rect(svg, x, y, w, nav_h, fill=COLORS["navbar_bg"], stroke=COLORS["border"], rx=0)
    add_line(svg, x, y + nav_h, x + w, y + nav_h, stroke=COLORS["border"])

    # Title
    add_text(svg, x + 16, y + 34, comp.get("label", "Navigation"),
             size=16, fill=COLORS["text"], weight="bold")

    # Nav items
    items = comp.get("props", {}).get("items", [])
    if isinstance(items, str):
        items = [items]
    ix = x + 200
    for item in items[:6]:
        label = item if isinstance(item, str) else item.get("label", "Link")
        add_text(svg, ix, y + 34, label, size=13, fill=COLORS["primary"])
        ix += len(label) * 9 + 24


def render_table(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a data table."""
    props = comp.get("props", {})
    columns = props.get("columns", ["Column 1", "Column 2", "Column 3"])
    rows = int(props.get("rows", 5))

    add_rect(svg, x, y, w, h, fill=COLORS["card_bg"], stroke=COLORS["border"])

    # Label
    if comp.get("label"):
        add_text(svg, x + 12, y + 20, comp["label"], size=14, fill=COLORS["text"], weight="bold")
        y += 28

    # Header row
    col_w = w // max(len(columns), 1)
    add_rect(svg, x, y, w, 32, fill=COLORS["surface"], stroke=COLORS["border"], rx=0)
    for i, col in enumerate(columns[:8]):
        label = col if isinstance(col, str) else col.get("label", f"Col {i}")
        add_text(svg, x + i * col_w + 12, y + 21, label, size=12, fill=COLORS["text"], weight="bold")

    # Data rows (placeholder lines)
    for r in range(min(rows, 8)):
        ry = y + 32 + r * 36
        add_line(svg, x, ry + 36, x + w, ry + 36, stroke=COLORS["border"])
        for i in range(min(len(columns), 8)):
            add_rect(svg, x + i * col_w + 12, ry + 10, col_w - 32, 16,
                     fill=COLORS["surface"], stroke="none", rx=2)


def render_card_grid(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a grid of cards."""
    props = comp.get("props", {})
    cols = int(props.get("columns", 3))
    card_count = int(props.get("count", cols * 2))

    if comp.get("label"):
        add_text(svg, x, y + 16, comp["label"], size=14, fill=COLORS["text"], weight="bold")
        y += 28

    gap = 16
    card_w = (w - gap * (cols - 1)) // cols
    card_h = 120

    for i in range(min(card_count, 12)):
        col = i % cols
        row = i // cols
        cx = x + col * (card_w + gap)
        cy = y + row * (card_h + gap)
        add_rect(svg, cx, cy, card_w, card_h, fill=COLORS["card_bg"], stroke=COLORS["border"])
        # Placeholder content
        add_rect(svg, cx + 12, cy + 12, card_w - 24, 16, fill=COLORS["surface"], stroke="none", rx=2)
        add_rect(svg, cx + 12, cy + 36, card_w - 48, 12, fill=COLORS["surface"], stroke="none", rx=2)
        add_rect(svg, cx + 12, cy + 56, card_w - 36, 12, fill=COLORS["surface"], stroke="none", rx=2)


def render_form(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a form with labeled input fields."""
    props = comp.get("props", {})
    fields = props.get("fields", ["Field 1", "Field 2", "Field 3"])

    if comp.get("label"):
        add_text(svg, x, y + 16, comp["label"], size=14, fill=COLORS["text"], weight="bold")
        y += 28

    fy = y
    for field in fields[:10]:
        label = field if isinstance(field, str) else field.get("label", "Field")
        add_text(svg, x, fy + 14, label, size=12, fill=COLORS["text_light"])
        add_rect(svg, x, fy + 20, min(w, 400), 36, fill=COLORS["input_bg"], stroke=COLORS["border"])
        fy += 68

    # Submit button
    btn_w = 120
    add_rect(svg, x, fy + 8, btn_w, 36, fill=COLORS["primary"], stroke="none", rx=6)
    add_text(svg, x + btn_w // 2, fy + 30, "Submit", size=13, fill="#FFFFFF", anchor="middle", weight="bold")


def render_modal(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a modal dialog overlay."""
    modal_w = min(w - 80, 500)
    modal_h = min(h - 80, 300)
    mx = x + (w - modal_w) // 2
    my = y + (h - modal_h) // 2

    # Overlay
    add_rect(svg, x, y, w, h, fill=COLORS["modal_overlay"], stroke="none", rx=0)

    # Modal body
    add_rect(svg, mx, my, modal_w, modal_h, fill=COLORS["bg"], stroke=COLORS["border"], rx=8)

    # Title
    title = comp.get("label", "Modal")
    add_text(svg, mx + 24, my + 32, title, size=16, fill=COLORS["text"], weight="bold")
    add_line(svg, mx, my + 48, mx + modal_w, my + 48)

    # Close button hint
    add_text(svg, mx + modal_w - 32, my + 28, "x", size=18, fill=COLORS["text_light"])

    # Placeholder content
    add_rect(svg, mx + 24, my + 64, modal_w - 48, 16, fill=COLORS["surface"], stroke="none", rx=2)
    add_rect(svg, mx + 24, my + 88, modal_w - 80, 16, fill=COLORS["surface"], stroke="none", rx=2)

    # Action buttons
    btn_y = my + modal_h - 56
    add_rect(svg, mx + modal_w - 240, btn_y, 100, 36, fill=COLORS["surface"], stroke=COLORS["border"], rx=6)
    add_text(svg, mx + modal_w - 190, btn_y + 23, "Cancel", size=13, fill=COLORS["text"], anchor="middle")
    add_rect(svg, mx + modal_w - 124, btn_y, 100, 36, fill=COLORS["primary"], stroke="none", rx=6)
    add_text(svg, mx + modal_w - 74, btn_y + 23, "Confirm", size=13, fill="#FFFFFF", anchor="middle")


def render_breadcrumbs(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render breadcrumb navigation."""
    items = comp.get("props", {}).get("items", ["Home", "Section", "Page"])
    if isinstance(items, str):
        items = [items]

    bx = x
    for i, item in enumerate(items[:6]):
        label = item if isinstance(item, str) else item.get("label", "Crumb")
        color = COLORS["primary"] if i < len(items) - 1 else COLORS["text"]
        add_text(svg, bx, y + 14, label, size=12, fill=color)
        bx += len(label) * 8 + 8
        if i < len(items) - 1:
            add_text(svg, bx, y + 14, "/", size=12, fill=COLORS["text_light"])
            bx += 16


def render_tabs(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render tab navigation."""
    items = comp.get("props", {}).get("items", ["Tab 1", "Tab 2", "Tab 3"])
    if isinstance(items, str):
        items = [items]

    add_line(svg, x, y + 36, x + w, y + 36)

    tx = x
    for i, item in enumerate(items[:8]):
        label = item if isinstance(item, str) else item.get("label", f"Tab {i + 1}")
        tab_w = len(label) * 9 + 32
        fill = COLORS["text"] if i == 0 else COLORS["text_light"]
        add_text(svg, tx + 16, y + 22, label, size=13, fill=fill,
                 weight="bold" if i == 0 else "normal")
        if i == 0:
            add_line(svg, tx, y + 35, tx + tab_w, y + 35, stroke=COLORS["primary"], width=2)
        tx += tab_w


def render_buttons(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a group of buttons."""
    items = comp.get("props", {}).get("items", [comp.get("label", "Button")])
    if isinstance(items, str):
        items = [items]

    bx = x
    for item in items[:5]:
        label = item if isinstance(item, str) else item.get("label", "Button")
        btn_w = len(label) * 9 + 32
        variant = "primary"
        if isinstance(item, dict):
            variant = item.get("variant", "primary")

        if variant == "secondary":
            add_rect(svg, bx, y, btn_w, 36, fill=COLORS["surface"], stroke=COLORS["border"], rx=6)
            add_text(svg, bx + btn_w // 2, y + 23, label, size=13, fill=COLORS["text"], anchor="middle")
        elif variant == "danger":
            add_rect(svg, bx, y, btn_w, 36, fill=COLORS["danger"], stroke="none", rx=6)
            add_text(svg, bx + btn_w // 2, y + 23, label, size=13, fill="#FFFFFF", anchor="middle")
        else:
            add_rect(svg, bx, y, btn_w, 36, fill=COLORS["primary"], stroke="none", rx=6)
            add_text(svg, bx + btn_w // 2, y + 23, label, size=13, fill="#FFFFFF", anchor="middle")
        bx += btn_w + 12


def render_text_block(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a text block placeholder."""
    label = comp.get("label", "Text Block")
    add_text(svg, x, y + 18, label, size=14, fill=COLORS["text"], weight="bold")
    # Placeholder text lines
    for i in range(3):
        line_w = w - (i * 40) if i < 2 else w - 120
        add_rect(svg, x, y + 28 + i * 20, min(line_w, w), 12,
                 fill=COLORS["surface"], stroke="none", rx=2)


def render_search_bar(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a search input."""
    bar_w = min(w, 400)
    add_rect(svg, x, y, bar_w, 40, fill=COLORS["input_bg"], stroke=COLORS["border"], rx=20)
    add_text(svg, x + 40, y + 26, comp.get("props", {}).get("placeholder", "Search..."),
             size=13, fill=COLORS["text_light"])
    # Search icon hint (circle + line)
    add_text(svg, x + 16, y + 26, "🔍", size=14, fill=COLORS["text_light"])


def render_stat_card(svg: Element, comp: dict, x: int, y: int, w: int, h: int) -> None:
    """Render a statistics card."""
    card_w = min(w, 200)
    card_h = 100
    add_rect(svg, x, y, card_w, card_h, fill=COLORS["card_bg"], stroke=COLORS["border"], rx=8)

    label = comp.get("label", "Metric")
    add_text(svg, x + 16, y + 28, label, size=12, fill=COLORS["text_light"])
    add_text(svg, x + 16, y + 58, "0", size=28, fill=COLORS["text"], weight="bold")
    add_rect(svg, x + 16, y + 72, card_w - 32, 8, fill=COLORS["accent"], stroke="none", rx=4)


# Component registry
RENDERERS = {
    "sidebar": render_sidebar,
    "navbar": render_navbar,
    "table": render_table,
    "card-grid": render_card_grid,
    "form": render_form,
    "modal": render_modal,
    "breadcrumbs": render_breadcrumbs,
    "tabs": render_tabs,
    "buttons": render_buttons,
    "text-block": render_text_block,
    "search-bar": render_search_bar,
    "stat-card": render_stat_card,
}


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------

POSITION_MAP = {
    "left": "left",
    "right": "right",
    "top": "top",
    "bottom": "bottom",
    "center": "center",
}


def compute_layout_regions(vp_w: int, vp_h: int, components: list[dict]) -> dict:
    """Compute bounding boxes for each component based on position hints."""
    has_sidebar = any(c.get("type") == "sidebar" for c in components)
    has_navbar = any(c.get("type") == "navbar" for c in components)

    sidebar_w = 240 if has_sidebar else 0
    navbar_h = 56 if has_navbar else 0

    content_x = sidebar_w
    content_y = navbar_h
    content_w = vp_w - sidebar_w
    content_h = vp_h - navbar_h

    regions = {}
    # Fixed regions for sidebar/navbar
    for comp in components:
        cid = comp.get("id", comp.get("type", "unknown"))
        ctype = comp.get("type", "")

        if ctype == "sidebar":
            regions[cid] = (0, 0, sidebar_w, vp_h)
        elif ctype == "navbar":
            regions[cid] = (sidebar_w, 0, content_w, navbar_h)

    # Stack remaining components in content area
    remaining = [c for c in components if c.get("type") not in ("sidebar", "navbar")]
    cy = content_y + 16
    for comp in remaining:
        cid = comp.get("id", comp.get("type", "unknown"))
        ctype = comp.get("type", "")

        # Estimate height based on type
        if ctype == "breadcrumbs":
            ch = 28
        elif ctype == "tabs":
            ch = 44
        elif ctype == "search-bar":
            ch = 48
        elif ctype == "buttons":
            ch = 44
        elif ctype == "text-block":
            ch = 80
        elif ctype == "stat-card":
            ch = 108
        elif ctype == "table":
            rows = int(comp.get("props", {}).get("rows", 5))
            ch = 32 + 28 + min(rows, 8) * 36 + 16
        elif ctype == "card-grid":
            cols = int(comp.get("props", {}).get("columns", 3))
            count = int(comp.get("props", {}).get("count", cols * 2))
            card_rows = (count + cols - 1) // cols
            ch = 28 + card_rows * 136
        elif ctype == "form":
            fields = comp.get("props", {}).get("fields", ["F1", "F2", "F3"])
            ch = 28 + len(fields) * 68 + 52
        elif ctype == "modal":
            ch = content_h  # Modal overlays full content area
        else:
            ch = 100

        padding = 24
        regions[cid] = (content_x + padding, cy, content_w - padding * 2, ch)
        cy += ch + 16

    return regions


def render_screen(screen: dict, vp_w: int, vp_h: int) -> bytes:
    """Render a single screen to SVG bytes."""
    components = screen.get("components", [])
    regions = compute_layout_regions(vp_w, vp_h, components)

    # Auto-expand height to fit all content
    max_bottom = vp_h
    for cid, (rx, ry, rw, rh) in regions.items():
        max_bottom = max(max_bottom, ry + rh + 32)
    actual_h = max_bottom

    svg = make_svg(vp_w, actual_h)

    # Screen title (small, bottom-left)
    title = screen.get("title", screen.get("screen_id", "Screen"))
    add_text(svg, 8, actual_h - 8, f"Screen: {title}", size=10, fill=COLORS["text_light"])

    for comp in components:
        cid = comp.get("id", comp.get("type", "unknown"))
        ctype = comp.get("type", "")
        renderer = RENDERERS.get(ctype)

        if renderer and cid in regions:
            x, y, w, h = regions[cid]
            try:
                renderer(svg, comp, x, y, w, h)
            except Exception as e:
                # Fallback: draw a labeled box
                add_rect(svg, x, y, w, 60, fill=COLORS["surface"], stroke=COLORS["border"])
                add_text(svg, x + 8, y + 24, f"[{ctype}] {comp.get('label', '')}", size=12, fill=COLORS["text_light"])
        elif cid in regions:
            x, y, w, h = regions[cid]
            add_rect(svg, x, y, w, 60, fill=COLORS["surface"], stroke=COLORS["border"])
            add_text(svg, x + 8, y + 24, f"[{ctype}] {comp.get('label', cid)}", size=12, fill=COLORS["text_light"])

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(svg, encoding="unicode").encode("utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate SVG wireframe files from a design spec JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Supported component types:
  sidebar, navbar, table, card-grid, form, modal, breadcrumbs,
  tabs, buttons, text-block, search-bar, stat-card

Input JSON must have a "screens" key containing an array of screen objects.
Each screen should have: screen_id, title, components[].

Output: one SVG file per screen, named {screen_id}.svg
""",
    )
    parser.add_argument("--input", required=True, help="Path to design spec JSON file")
    parser.add_argument("--output-dir", default=".", help="Directory for SVG output (default: current dir)")
    parser.add_argument("--viewport", default="1440x900", help="Viewport dimensions WxH (default: 1440x900)")

    args = parser.parse_args()

    # Parse viewport
    try:
        vp_parts = args.viewport.split("x")
        vp_w, vp_h = int(vp_parts[0]), int(vp_parts[1])
    except (ValueError, IndexError):
        print(f"Error: Invalid viewport format '{args.viewport}'. Use WxH (e.g. 1440x900).", file=sys.stderr)
        sys.exit(1)

    # Load input
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        spec = json.loads(input_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in {input_path}: {e}", file=sys.stderr)
        sys.exit(1)

    if "screens" not in spec:
        print("Error: No 'screens' key found in input JSON.", file=sys.stderr)
        sys.exit(1)

    screens = spec["screens"]
    if not screens:
        print("No screens to render (empty array).")
        sys.exit(0)

    # Output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated = []
    for screen in screens:
        screen_id = screen.get("screen_id", "unknown")
        svg_bytes = render_screen(screen, vp_w, vp_h)

        out_file = output_dir / f"{screen_id}.svg"
        out_file.write_bytes(svg_bytes)
        generated.append(str(out_file))
        print(f"Generated: {out_file}")

    print(f"\n{len(generated)} wireframe(s) generated in {output_dir}")


if __name__ == "__main__":
    main()
