"""Собирает docs/*.md в один PDF (Mermaid и формулы рендерятся в headless Edge/Chrome).

Запуск: python docs/build_pdf.py
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
PARTS = ["01_architecture.md", "02_data_schema.md", "03_analytics_metrics.md", "04_examples.md", "05_risks_limitations.md"]
OUT = DOCS / "AI-агент_госзакупки_документация.pdf"
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chromium", "google-chrome"]

CSS = """
@page { size: A4; margin: 14mm 13mm; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 9.6pt; color: #1d2330; line-height: 1.42; }
h1 { font-size: 18pt; color: #12305a; border-bottom: 2px solid #12305a; padding-bottom: 3px; page-break-before: always; }
h1.first { page-break-before: avoid; }
h2 { font-size: 12.5pt; color: #1f4f8a; margin-top: 16px; }
h3 { font-size: 10.5pt; color: #1f4f8a; margin: 10px 0 4px; }
table { border-collapse: collapse; width: 100%; margin: 6px 0 10px; font-size: 8.3pt; }
th, td { border: 1px solid #cfd6e2; padding: 2px 5px; text-align: left; vertical-align: top; overflow-wrap: break-word; }
h2, h3 { page-break-after: avoid; }
th { background: #e8edf5; }
tr { page-break-inside: avoid; }
code { font-family: Consolas, monospace; font-size: 8.4pt; background: #f1f4f9; padding: 0 2px; }
pre { background: #f4f6fa; border: 1px solid #dde3ec; padding: 6px 8px; white-space: pre-wrap; font-size: 7.8pt; }
pre.mermaid { background: none; border: none; text-align: center; page-break-inside: avoid; }
hr { border: none; border-top: 1px dashed #b8c2d3; margin: 14px 0; }
a { color: #1f4f8a; text-decoration: none; }
.cover { text-align: center; margin-top: 180px; }
.cover h1 { border: none; font-size: 24pt; page-break-before: avoid; }
"""

HEAD = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>{css}</style>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"></script>
</head><body>"""

TAIL = r"""<script>
mermaid.initialize({startOnLoad: true, theme: 'neutral', flowchart: {useMaxWidth: true}});
renderMathInElement(document.body, {delimiters: [{left: '$$', right: '$$', display: true},
                                                 {left: '\\(', right: '\\)', display: false}]});
</script></body></html>"""


def convert(md_text):
    keep = {}

    def stash(html):
        key = f"@@BLOCK{len(keep)}@@"
        keep[key] = html
        return key

    md_text = re.sub(r"```mermaid\n(.*?)```", lambda m: stash(f'<pre class="mermaid">{m.group(1)}</pre>'), md_text, flags=re.S)
    md_text = re.sub(r"\$\$(.+?)\$\$", lambda m: stash(f"$${m.group(1)}$$"), md_text, flags=re.S)
    md_text = re.sub(r"(?<![\w\\])\$([^$\n]+?)\$", lambda m: stash(f"\\({m.group(1)}\\)"), md_text)
    # Python-Markdown, в отличие от GitHub, требует пустую строку перед списком
    md_text = re.sub(r"(?m)^((?![ \t]*(?:[-*]|\d+\.)[ \t]|[ \t]*$|\|).+)\n([ \t]*(?:[-*]|\d+\.)[ \t])", r"\1\n\n\2", md_text)
    html = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    html = html.replace("<details>", "<details open>")
    for key, value in keep.items():
        html = html.replace(f"<p>{key}</p>", value).replace(key, value)
    return html


def main():
    body = ['<div class="cover"><h1>AI-агент анализа государственных закупок Республики Казахстан</h1>'
            '<p>Пакет документов по ТЗ п. 12</p><p>Данные: OWS v3, 27 организаций, 2024–2026</p></div>']
    body += [convert((DOCS / name).read_text(encoding="utf-8")) for name in PARTS]
    page = HEAD.format(css=CSS) + "\n".join(body) + TAIL
    tmp = Path(tempfile.mkdtemp()) / "docs.html"
    tmp.write_text(page, encoding="utf-8")
    browser = next((b for b in BROWSERS if Path(b).exists() or shutil.which(b)), None)
    if not browser:
        raise SystemExit(f"Нет Edge/Chrome; HTML: {tmp}")
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--user-data-dir={tmp.parent / 'profile'}", "--virtual-time-budget=20000",
                    f"--print-to-pdf={OUT}", tmp.as_uri()], check=True, capture_output=True)
    print(f"PDF: {OUT} ({OUT.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
