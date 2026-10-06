"""
The portal's light theme, in one place.

Both pages (app.PORTAL_HTML and dashboard_page.DASHBOARD_HTML) drop THEME_HEAD into their <head>, so
the palette cannot drift between them.

Two layers of colour:
  - the chrome - surfaces, text, buttons, tabs, focus rings - is the house palette: rust (8F3B3B) and
    crimson (972E2E) for accents, blush (FDDCDC) for selection, warm charcoal (403737) for text and the
    header, and the greys B4B3B3 / DADADA / EBE7E7 / EEF1F1 / FAFCFC for every surface and edge;
  - the data - KPI tiles, violation-type badges, verdicts, telemetry chips - keeps its own semantic
    hues, chosen for what they say: red for speed and alarms, amber for warnings and pending work,
    green for cleared, blue for fleet and night, so the numbers stay legible and distinct.

Text rules: charcoal (ink) on light surfaces; near-white (ice) on solid coloured containers. Every
pairing used here clears WCAG AA.
"""

# Single source of truth. The Tailwind config and the CSS variables below are both generated from it,
# so a colour is written once and reaches utility classes and plain CSS alike.
TOKENS = {
    # surfaces, light to dark: the palette's greys
    "card":        "#FAFCFC",
    "hover":       "#F6F1F1",
    "page":        "#EEF1F1",
    "rail":        "#EBE7E7",
    "head":        "#DADADA",
    "edge":        "#DADADA",
    "edge2":       "#B4B3B3",

    # text on light surfaces: the palette's charcoal, and two lighter steps of it that still pass AA
    "ink":         "#403737",
    "ink2":        "#5F5555",
    "ink3":        "#7A7171",

    # text on solid coloured containers
    "ice":         "#F6EEEE",
    "ice-lt":      "#FFFFFF",

    # --- the palette: chrome accents
    # rust: primary buttons, active tabs, the selected row's bar
    "brand":       "#8F3B3B",
    "brand-deep":  "#6E2C2C",
    "brand-soft":  "#FDDCDC",
    # crimson: icons, focus rings, hover borders
    "accent":      "#972E2E",
    "accent-deep": "#7A2424",
    "accent-soft": "#FBE9E9",
    # charcoal: the header bar, tooltips, neutral tiles
    "slate":       "#403737",
    "slate-deep":  "#2B2424",
    "slate-soft":  "#EFEBEB",

    # --- semantic hues for data, deliberately outside the palette
    # alarm red: speed, violation counts, a False verdict, errors
    "danger":      "#B42318",
    "danger-deep": "#8A1C14",
    "danger-soft": "#FDE8E6",
    # amber: warnings, pending work, a moving vehicle, daytime
    "warn":        "#B45309",
    "warn-deep":   "#8A3F07",
    "warn-soft":   "#FDF0DC",
    # green: a Genuine verdict, ignition on, nothing to report
    "ok":          "#1F7A4D",
    "ok-deep":     "#155C39",
    "ok-soft":     "#E2F3EA",
    # steel blue: the fleet itself, night time
    "info":        "#1E4E79",
    "info-deep":   "#143A5C",
    "info-soft":   "#E3EDF7",
    # violation-type marks that need their own hue
    "plum":        "#6B3D8F",
    "plum-soft":   "#F1E9F7",
    "teal":        "#0F6E6E",
    "teal-soft":   "#DFF1F0",
    "coffee":      "#7F5436",
    "coffee-soft": "#F4ECE5",
    "amber":       "#B45309",
}


def _nest(tokens):
    """{'brand-deep': v} -> {'brand': {'DEFAULT': ..., 'deep': v}} so Tailwind gives bg-brand-deep."""
    out = {}
    for name, value in tokens.items():
        base, _, suffix = name.partition("-")
        if suffix:
            out.setdefault(base, {})[suffix] = value
        else:
            out.setdefault(base, {})["DEFAULT"] = value
    # a token with only a DEFAULT is better off flat, so bg-card stays bg-card
    return {k: (v["DEFAULT"] if list(v) == ["DEFAULT"] else v) for k, v in out.items()}


def _json(obj, indent=10):
    import json
    return json.dumps(obj, indent=2).replace("\n", "\n" + " " * indent)


def _css_vars(tokens):
    return "\n".join(f"      --{name}: {value};" for name, value in tokens.items())


def _favicon(glyph):
    """
    A browser-tab icon drawn from the theme's own colours: the rounded tile of the page's logo mark,
    in the same brand gradient, with one white glyph on it. Inlined as a data URI because the app
    serves no static files, so there is nothing to keep in sync and nothing extra to fetch.
    """
    from urllib.parse import quote
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
        f"<stop offset='0' stop-color='{TOKENS['brand-deep']}'/>"
        f"<stop offset='.55' stop-color='{TOKENS['brand']}'/>"
        f"<stop offset='1' stop-color='{TOKENS['accent']}'/>"
        "</linearGradient></defs>"
        "<rect width='32' height='32' rx='7' fill='url(#g)'/>"
        f"<path d='{glyph}' fill='#FFFFFF'/>"
        "</svg>"
    )
    return f'  <link rel="icon" href="data:image/svg+xml,{quote(svg, safe="")}">\n'


# A shield for the portal and a bar chart for the dashboard - the same marks their headers already use
FAVICON_SHIELD = _favicon("M16 5.2l9 3.4v7.6c0 5.2-3.9 8.9-9 10.6-5.1-1.7-9-5.4-9-10.6V8.6z")
FAVICON_CHART = _favicon("M7.5 19h4.2v8H7.5zm6.4-7h4.2v15h-4.2zm6.4-6h4.2v21h-4.2z")


THEME_HEAD = f"""  <script>
    // Semantic colour names for the utility classes; see theme.py for what each one is for.
    tailwind.config = {{ theme: {{ extend: {{ colors: {_json(_nest(TOKENS))} }} }} }};
  </script>
  <style>
    /* Light theme: the house palette for chrome, semantic hues for data; see theme.py. */
    :root {{
{_css_vars(TOKENS)}
    }}
    /* Root size follows the window (width and height), so every rem-based size scales with it */
    html {{ font-size: clamp(12px, min(1vw, 1.8vh), 18px); }}
    body {{
      font-family: 'Inter', sans-serif;
      margin: 0;
      color: var(--ink);
      background-color: var(--page);
      /* a faint rust wash over the ice-grey page, so the canvas is warm without being tinted */
      background:
        radial-gradient(ellipse 65% 50% at 50% -10%, rgba(143, 59, 59, 0.07), transparent 70%),
        radial-gradient(ellipse 70% 60% at 85% 10%, rgba(253, 220, 220, 0.35), transparent 70%),
        linear-gradient(180deg, var(--card) 0%, var(--page) 45%, #E8EBEB 100%);
      background-attachment: fixed;
    }}
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: var(--rail); }}
    ::-webkit-scrollbar-thumb {{ background: var(--edge2); border-radius: 4px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: var(--ink3); }}

    /* Tooltip: a dark chip, so it reads as an overlay rather than another card */
    .tip {{
      position: fixed; z-index: 60; max-width: 34rem; pointer-events: none;
      background: var(--slate); color: var(--ice-lt); border: 1px solid var(--slate-deep);
      border-radius: 0.625rem; padding: 0.55rem 0.75rem; font-size: 0.8rem; line-height: 1.4;
      box-shadow: 0 0.75rem 2rem rgba(64, 55, 55, 0.28); opacity: 0; transition: opacity 120ms ease;
      white-space: pre-wrap; overflow-wrap: anywhere;
    }}
    .tip.show {{ opacity: 1; }}

    .glow-loader-wrapper {{ position: relative; width: 100%; min-height: 10rem; display: flex; align-items: center; justify-content: center; }}
    .glow-loader-wrapper::before {{ content: ''; position: absolute; width: 3.5rem; height: 3.5rem; border-radius: 50%; border: 0.3rem solid var(--edge); box-sizing: border-box; }}
    .glow-loader-container {{
      position: relative; border-radius: 50%; height: 3.5rem; width: 3.5rem;
      animation: rotate_3922 1.1s linear infinite;
      background: conic-gradient(from 0deg, transparent 15%, var(--brand-soft) 45%, var(--brand) 75%, var(--accent) 100%);
      -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 0.3rem), #fff calc(100% - 0.25rem));
      mask: radial-gradient(farthest-side, transparent calc(100% - 0.3rem), #fff calc(100% - 0.25rem));
    }}
    /* A selected table row: blush tint, with the rust bar drawn inside its first cell, so it sits in
       the cell's padding (never over the text) and no table border or overflow can clip it */
    tr.row-sel > td {{ background: var(--brand-soft); border-top: 1px solid rgba(143, 59, 59, 0.22); border-bottom: 1px solid rgba(143, 59, 59, 0.22); }}
    tr.row-sel > td:first-child {{ box-shadow: inset 0.25rem 0 0 var(--brand); color: var(--brand-deep); font-weight: 700; }}
    tr.row-sel:hover > td {{ background: #FBD3D3; }}
    /* The telemetry message an alert was raised at: the same shape in alarm red */
    tr.row-mark > td {{ background: var(--danger-soft); }}
    tr.row-mark > td:first-child {{ box-shadow: inset 0.25rem 0 0 var(--danger); color: var(--danger-deep); font-weight: 700; }}

    @keyframes rotate_3922 {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
  </style>
"""
