"""
HTML -> PDF conversion, via WeasyPrint.

Chosen over a headless-browser renderer (Playwright/Chromium) specifically because this app
deploys on Render's FREE plan (512MB RAM, no persistent disk -- see render.yaml). A headless
Chromium install is 300-500MB of binaries and a single render can use 200-500MB of RAM, which
doesn't reliably fit alongside the already-running web process on that tier. WeasyPrint is pure
Python + native rendering libraries (no browser engine), much lighter to deploy -- the trade-off is
weaker CSS support than a real browser (no JS execution at all, and grid/flex support is more
limited), which is why report.py's and profile_pdf.py's HTML avoids relying on either for the
PDF-bound documents.
"""
from weasyprint import HTML


def html_to_pdf(html_str, base_url=None):
    """Render an HTML string to PDF bytes. `base_url` lets relative asset paths resolve if the
    HTML ever references any (neither report.py's nor profile_pdf.py's output currently does)."""
    return HTML(string=html_str, base_url=base_url).write_pdf()
