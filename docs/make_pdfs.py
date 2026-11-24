"""Render the proposal and technical note to PDF (Markdown -> styled HTML -> headless Chrome/Edge).
Run from the repo root:  python docs/make_pdfs.py
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import markdown

DOCS = Path(__file__).resolve().parent
CSS = """
@page { size: A4; margin: 16mm 15mm; }
body { font-family: "Segoe UI", Inter, Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #0f172a; }
h1 { font-size: 19pt; margin: 0 0 6pt; color: #0f1b33; border-bottom: 3px solid #ff5a36; padding-bottom: 6pt; }
h2 { font-size: 13pt; margin: 14pt 0 4pt; color: #0f1b33; }
p, li { margin: 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 9.5pt; page-break-inside: avoid; }
th, td { border: 1px solid #e2e8f0; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #f1f5f9; }
code { font-family: Consolas, monospace; font-size: 9pt; background: #f1f5f9; padding: 0 3px; border-radius: 3px; }
.foot { margin-top: 14pt; font-size: 8.5pt; color: #64748b; border-top: 1px solid #e2e8f0; padding-top: 6pt; }
"""


def browser() -> str:
    for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chrome")):
        if c and Path(c).exists():
            return c
    raise SystemExit("Chrome or Edge not found")


def main() -> None:
    exe = browser()
    with tempfile.TemporaryDirectory() as tmp:
        prof = Path(tmp) / "prof"
        for md_name, pdf_name in (("PROPOSAL.md", "SeatSense_Proposal.pdf"), ("TECHNICAL_NOTE.md", "SeatSense_Technical_Note.pdf")):
            body = markdown.markdown((DOCS / md_name).read_text(encoding="utf-8"), extensions=["tables", "fenced_code"])
            html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{pdf_name[:-4]}</title><style>{CSS}</style></head>"
                    f"<body>{body}<div class='foot'>SeatSense · Transport Stack Hackathon 2026 · Problem 2 · Team InnoVerse, BIT Mesra · "
                    f"github.com/MG782469/SeatSense</div></body></html>")
            src = Path(tmp) / (md_name + ".html")
            src.write_text(html, encoding="utf-8")
            out = DOCS / pdf_name
            subprocess.run([exe, "--headless=new", "--disable-gpu", f"--user-data-dir={prof}", "--no-pdf-header-footer",
                            f"--print-to-pdf={out}", src.as_uri()], check=True, capture_output=True, timeout=120)
            print(out.name, out.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
