"""HTML -> PDF conversion for the Material presentation resume.

The designed resume is authored as HTML/CSS (gradients, CSS grid, flexbox, rounded
chips). Only a real browser engine reproduces that faithfully, so the preferred
backend is headless Chrome/Chromium; WeasyPrint is a lighter fallback for servers
without a browser.

Every backend is optional. If none is available the caller simply keeps the .html
artifact and the user prints to PDF from their browser, so resume generation must
never fail because of a missing PDF toolchain.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Chrome needs a moment for webfonts/layout; a resume is small so this is generous.
_CHROME_TIMEOUT = 60

_CHROME_ENV = "JOBHUNTER_CHROME"

_MAC_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
_PATH_CANDIDATES = [
    "google-chrome-stable", "google-chrome", "chromium-browser", "chromium",
    "brave-browser", "microsoft-edge", "chrome",
]
_LINUX_CANDIDATES = [
    "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser", "/usr/bin/chromium",
    "/snap/bin/chromium",
]


def find_chrome() -> str | None:
    """Locate a Chromium-family binary, or None."""
    override = os.getenv(_CHROME_ENV, "").strip()
    if override:
        if Path(override).is_file() and os.access(override, os.X_OK):
            return override
        log.warning("%s=%s is not an executable file; ignoring", _CHROME_ENV, override)

    candidates: list[str] = []
    if platform.system() == "Darwin":
        candidates += _MAC_CANDIDATES
    else:
        candidates += _LINUX_CANDIDATES
    for name in _PATH_CANDIDATES:
        found = shutil.which(name)
        if found:
            candidates.append(found)

    for c in candidates:
        if Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


def _chrome_to_pdf(chrome: str, html_path: Path, pdf_path: Path) -> bool:
    base = [
        chrome,
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",  # /dev/shm is tiny on small cloud VMs
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--hide-scrollbars",
        "--no-pdf-header-footer",   # suppress the default URL/date furniture
    ]
    url = html_path.resolve().as_uri()

    # Chrome's own default profile works fine and is fastest. A fresh
    # --user-data-dir makes headless Chrome hang on macOS, so it is only used as a
    # fallback -- it is needed when Chrome runs as root on Linux.
    attempts: list[list[str]] = [[]]
    tmp = tempfile.mkdtemp(prefix="jobhunter-chrome-")
    attempts.append([f"--user-data-dir={tmp}"])

    try:
        for extra in attempts:
            if pdf_path.exists():
                pdf_path.unlink()
            cmd = base + extra + [f"--print-to-pdf={pdf_path}", url]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=_CHROME_TIMEOUT)
            except subprocess.TimeoutExpired:
                log.warning("Chrome PDF export timed out after %ss", _CHROME_TIMEOUT)
                continue
            except OSError as exc:
                log.warning("Chrome PDF export could not start: %s", exc)
                return False
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                return True
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            log.warning("Chrome PDF export produced no file (exit %s): %s",
                        proc.returncode, err)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return False


def _pdf_word_count(pdf_path: Path) -> int | None:
    """Words extractable from the PDF, or None if no PDF parser is available."""
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(str(pdf_path))
        text = " ".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        log.debug("Could not read back PDF for validation: %s", exc)
        return None
    return len(re.findall(r"\w+", text))


def _html_word_count(html_path: Path) -> int:
    raw = html_path.read_text(encoding="utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    return len(re.findall(r"\w+", re.sub(r"(?s)<[^>]+>", " ", raw)))


def _looks_truncated(pdf_path: Path, html_path: Path) -> bool:
    """True if the PDF is missing a large share of the resume's text.

    Some CSS constructs (a float container that establishes a block formatting
    context, e.g. `overflow:hidden` or `display:flow-root`) make WeasyPrint emit a
    single clipped page, silently dropping most of the content. Byte size does not
    reveal this -- embedded fonts dominate it -- so compare extractable words.

    Returns False when no PDF parser is installed: without evidence of a problem we
    keep the file rather than discarding a probably-fine resume.
    """
    got = _pdf_word_count(pdf_path)
    if got is None:
        return False
    expected = _html_word_count(html_path)
    if expected < 50:
        return False
    return got < expected * 0.6


def _strip_trailing_blank_pages(pdf_path: Path) -> int:
    """Drop empty pages at the end of the PDF. Returns how many were removed.

    WeasyPrint can emit a trailing empty page when the two floated columns end at
    different heights, which looks careless on a resume sent to a recruiter.
    """
    try:
        from pypdf import PdfReader, PdfWriter  # type: ignore
    except Exception:
        return 0
    try:
        reader = PdfReader(str(pdf_path))
        pages = list(reader.pages)
        keep = len(pages)
        while keep > 1 and not re.search(r"\w", pages[keep - 1].extract_text() or ""):
            keep -= 1
        if keep == len(pages):
            return 0
        writer = PdfWriter()
        for page in pages[:keep]:
            writer.add_page(page)
        tmp = pdf_path.with_suffix(".pdf.tmp")
        with open(tmp, "wb") as fh:
            writer.write(fh)
        tmp.replace(pdf_path)
        return len(pages) - keep
    except Exception as exc:
        log.debug("Could not strip blank pages: %s", exc)
        return 0


def _weasyprint_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:
        return False
    try:
        HTML(filename=str(html_path)).write_pdf(str(pdf_path))
    except Exception as exc:  # missing pango/cairo shows up here
        log.warning("WeasyPrint PDF export failed: %s", exc)
        return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def _finalise(pdf_path: Path) -> None:
    dropped = _strip_trailing_blank_pages(pdf_path)
    if dropped:
        log.info("Removed %d trailing blank page(s) from %s", dropped, pdf_path.name)


def html_to_pdf(html_path: Path, pdf_path: Path) -> Path | None:
    """Convert `html_path` to `pdf_path`. Returns None if no backend is available."""
    html_path = Path(html_path)
    pdf_path = Path(pdf_path)
    if not html_path.is_file():
        log.warning("PDF export skipped: %s does not exist", html_path)
        return None
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    chrome = find_chrome()
    if chrome and _chrome_to_pdf(chrome, html_path, pdf_path):
        if not _looks_truncated(pdf_path, html_path):
            _finalise(pdf_path)
            return pdf_path
        log.warning("Chrome produced a suspiciously small PDF; trying WeasyPrint")
    if _weasyprint_to_pdf(html_path, pdf_path):
        if not _looks_truncated(pdf_path, html_path):
            _finalise(pdf_path)
            return pdf_path
        # A truncated PDF is worse than none -- it would be sent to a recruiter.
        log.error("PDF export discarded: only %s of ~%s words made it into the PDF. "
                  "Print the .html to PDF from a browser instead.",
                  _pdf_word_count(pdf_path), _html_word_count(html_path))
        pdf_path.unlink(missing_ok=True)
        return None
    if not chrome:
        log.info(
            "No PDF backend found -- install Chrome/Chromium (or set %s), or "
            "`pip install weasyprint`. The .html resume can still be printed to "
            "PDF from a browser.", _CHROME_ENV,
        )
    return None


def backend_status() -> dict[str, object]:
    """Diagnostics for `doctor` / the dashboard."""
    chrome = find_chrome()
    try:
        import weasyprint  # type: ignore  # noqa: F401
        weasy = True
    except Exception:
        weasy = False
    return {
        "chrome": chrome,
        "weasyprint": weasy,
        "available": bool(chrome or weasy),
    }
