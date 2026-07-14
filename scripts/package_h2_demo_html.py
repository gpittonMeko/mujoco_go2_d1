#!/usr/bin/env python3
"""Build a single styled HTML manual from docs/h2_demo_mail_package/*.md"""
from __future__ import annotations

import re
from pathlib import Path

import markdown
from markdown.extensions.tables import TableExtension
from markdown.extensions.fenced_code import FencedCodeExtension

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "docs" / "h2_demo_mail_package"
OUT = REPO / "docs" / "H2_Demo_Documentazione.html"

ORDER = [
    "LEGGIMI.md",
    "01_LA_DEMO.md",
    "02_ACCENSIONE_ACCESSI_MANI.md",
    "03_FILE_SUL_ROBOT.md",
    "04_RIFERIMENTI.md",
    "README_THOR.md",
]

CSS = """
:root {
  --bg: #f6f7f9;
  --card: #ffffff;
  --text: #1a1d21;
  --muted: #5c6570;
  --accent: #0b57d0;
  --border: #e2e6ea;
  --code-bg: #f0f3f7;
  --warn: #fff8e1;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
  font-size: 15px;
  line-height: 1.6;
  color: var(--text);
  background: var(--bg);
}
.layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  min-height: 100vh;
}
nav {
  background: #1e2430;
  color: #e8ecf1;
  padding: 1.25rem 1rem;
  position: sticky;
  top: 0;
  height: 100vh;
  overflow-y: auto;
}
nav h2 {
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #9aa3ad;
  margin: 0 0 0.75rem 0.5rem;
}
nav a {
  display: block;
  color: #dce3eb;
  text-decoration: none;
  padding: 0.45rem 0.65rem;
  border-radius: 6px;
  font-size: 0.92rem;
  margin-bottom: 2px;
}
nav a:hover { background: rgba(255,255,255,0.08); }
main {
  padding: 2rem 2.5rem 4rem;
  max-width: 920px;
}
article {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 2rem 2.25rem;
  margin-bottom: 2rem;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
}
article h1 {
  font-size: 1.75rem;
  margin-top: 0;
  border-bottom: 2px solid var(--accent);
  padding-bottom: 0.5rem;
}
article h2 { margin-top: 2rem; font-size: 1.25rem; }
article h3 { margin-top: 1.5rem; font-size: 1.05rem; }
article p, article li { color: var(--text); }
article blockquote {
  margin: 1rem 0;
  padding: 0.75rem 1rem;
  background: var(--warn);
  border-left: 4px solid #f9a825;
  border-radius: 0 6px 6px 0;
}
article code {
  background: var(--code-bg);
  padding: 0.15em 0.4em;
  border-radius: 4px;
  font-size: 0.9em;
  font-family: Consolas, "Cascadia Code", monospace;
}
article pre {
  background: #1e2430;
  color: #e8ecf1;
  padding: 1rem 1.15rem;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 0.88rem;
  line-height: 1.45;
}
article pre code {
  background: none;
  padding: 0;
  color: inherit;
}
article table {
  width: 100%;
  border-collapse: collapse;
  margin: 1rem 0;
  font-size: 0.92rem;
}
article th, article td {
  border: 1px solid var(--border);
  padding: 0.55rem 0.75rem;
  text-align: left;
}
article th { background: #f0f3f7; font-weight: 600; }
article a { color: var(--accent); }
article hr { border: none; border-top: 1px solid var(--border); margin: 2rem 0; }
@media (max-width: 900px) {
  .layout { grid-template-columns: 1fr; }
  nav { position: static; height: auto; }
  main { padding: 1rem; }
}
"""

MD = markdown.Markdown(extensions=[TableExtension(), FencedCodeExtension()])


def slugify(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"[\s_]+", "-", text).strip("-") or "section"


def md_to_html(src: str) -> str:
    MD.reset()
    return MD.convert(src)


def title_from_md(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def main() -> int:
    if not PKG.is_dir():
        print(f"Missing {PKG}")
        return 1

    sections: list[tuple[str, str, str]] = []
    for name in ORDER:
        path = PKG / name
        if not path.is_file():
            print(f"Skip missing {name}")
            continue
        raw = path.read_text(encoding="utf-8")
        title = title_from_md(raw, name.replace(".md", "").replace("_", " "))
        sec_id = slugify(path.stem)
        body = md_to_html(raw)
        sections.append((sec_id, title, body))

    nav_links = "\n".join(
        f'    <a href="#{sid}">{title[:60]}</a>' for sid, title, _ in sections
    )
    articles = "\n".join(
        f'<article id="{sid}">\n{body}\n</article>' for sid, _, body in sections
    )

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>H2 Demo — Documentazione</title>
  <style>{CSS}</style>
</head>
<body>
  <div class="layout">
    <nav>
      <h2>Indice</h2>
{nav_links}
    </nav>
    <main>
{articles}
    </main>
  </div>
</body>
</html>
"""

    OUT.write_text(html, encoding="utf-8")
    print(f"Created: {OUT}")
    print(f"Size: {OUT.stat().st_size // 1024} KB — apri con doppio click nel browser")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
