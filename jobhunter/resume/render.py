"""Document rendering: ATS-safe .docx, Markdown and plain-text resumes.

ATS-safety rules applied here: single column, no tables, no text boxes, no headers/
footers, no images, standard fonts (Calibri/Arial), literal section headings that
parsers recognise, and plain hyphen bullets in the .txt variant.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import OUTPUT_DIR, Profile
from ..models import Job
from .pdf import html_to_pdf
from .tailor import TailoredResume

SECTION_ORDER = ["PROFESSIONAL SUMMARY", "CORE COMPETENCIES", "PROFESSIONAL EXPERIENCE",
                 "TECHNICAL SKILLS", "CERTIFICATIONS", "EDUCATION", "SELECTED ACHIEVEMENTS"]

# Degrees worth surfacing in the header banner, highest signal first.
_FLAGSHIP_DEGREE = re.compile(r"\b(MBA|PGDM|Master|MS\b|M\.S\.|MSc|PhD|Doctor)", re.I)


def credential_banner(resume: TailoredResume, profile: Profile) -> str:
    """'Degree | Institution' line shown under the name — ground truth only.

    Prefers the highest qualification (MBA/Master's) so the credential is visible
    to a recruiter within the first three lines rather than buried in EDUCATION.
    """
    entries = list(resume.education or []) or list(profile.education or [])
    chosen = next((e for e in entries if _FLAGSHIP_DEGREE.search(str(e.get("degree", "")))), None)
    if chosen is None:
        return ""
    degree = str(chosen.get("degree", "")).strip()
    institution = str(chosen.get("institution", "")).strip()
    if not degree:
        return ""
    return f"{degree}  |  {institution}" if institution else degree


def slug(*parts: str) -> str:
    s = "_".join(re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-") for p in parts if p)
    return re.sub(r"-{2,}", "-", s)[:110] or "resume"


def output_paths(job: Job, base_dir: Path | None = None) -> dict[str, Path]:
    d = (base_dir or OUTPUT_DIR) / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    stem = slug(job.company or "Company", job.title or "Role", job.job_id[:6])
    return {
        "docx": d / f"{stem}_Resume.docx",
        "md": d / f"{stem}_Resume.md",
        "txt": d / f"{stem}_Resume_ATS.txt",
        "json": d / f"{stem}_Resume.json",
        "html": d / f"{stem}_Resume.html",
        "pdf": d / f"{stem}_Resume.pdf",
        "cover_docx": d / f"{stem}_CoverLetter.docx",
        "cover_md": d / f"{stem}_CoverLetter.md",
    }


# ------------------------------------------------------------------- DOCX
def render_docx(resume: TailoredResume, profile: Profile, job: Job, path: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor, Inches

    doc = Document()

    # Page + base style: tight margins, ATS-standard font
    for section in doc.sections:
        section.top_margin = section.bottom_margin = Inches(0.5)
        section.left_margin = section.right_margin = Inches(0.6)

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)
    normal.paragraph_format.space_after = Pt(2)
    normal.paragraph_format.space_before = Pt(0)

    def para(text: str = "", *, bold: bool = False, size: int = 10, align: str = "left",
             italic: bool = False, space_after: int = 2, color: tuple[int, int, int] | None = None):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(space_after)
        p.paragraph_format.space_before = Pt(0)
        if align == "center":
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text)
        run.bold, run.italic = bold, italic
        run.font.size = Pt(size)
        run.font.name = "Calibri"
        if color:
            run.font.color.rgb = RGBColor(*color)
        return p

    def heading(text: str):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(9)
        p.paragraph_format.space_after = Pt(3)
        run = p.add_run(text.upper())
        run.bold = True
        run.font.size = Pt(10)
        run.font.name = "Calibri"
        run.font.color.rgb = RGBColor(0x1E, 0x4F, 0xD8)
        # Material-style accent rule; still single-column and ATS-parseable.
        _letter_space(run, 60)
        _bottom_border(p)

    def bullet(text: str):
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(1)
        p.paragraph_format.left_indent = Inches(0.18)
        p.paragraph_format.first_line_indent = Inches(-0.14)
        run = p.add_run(text)
        run.font.size = Pt(9.5)
        run.font.name = "Calibri"

    # --- header ---------------------------------------------------------
    p_name = para(profile.name, bold=True, size=19, align="center", space_after=2)
    _letter_space(p_name.runs[0], 10)
    para(resume.target_title.upper(), size=10, align="center", space_after=2,
         color=(0x1E, 0x4F, 0xD8))
    banner = credential_banner(resume, profile)
    if banner:
        para(banner, size=9, align="center", space_after=2, color=(0x55, 0x5F, 0x6B))
    c = profile.contact
    contact_bits = [c.get("location"), c.get("phone"), c.get("email"), c.get("linkedin")]
    para(" | ".join(b for b in contact_bits if b), size=9, align="center", space_after=6,
         color=(0x4A, 0x55, 0x63))

    # --- summary --------------------------------------------------------
    heading("Professional Summary")
    para(resume.professional_summary, size=9.5, space_after=3)

    # --- core competencies (comma list, NOT a table -> ATS parseable) ---
    if resume.core_competencies:
        heading("Core Competencies")
        para(" | ".join(resume.core_competencies), size=9.5, space_after=3)

    # --- experience -----------------------------------------------------
    heading("Professional Experience")
    for role in resume.experience:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6)
        p.paragraph_format.space_after = Pt(0)
        r1 = p.add_run(f"{role['title']}")
        r1.bold = True
        r1.font.size = Pt(10.5)
        r2 = p.add_run(f"  |  {role['company']}")
        r2.font.size = Pt(10.5)
        r2.font.color.rgb = RGBColor(0x1E, 0x4F, 0xD8)
        meta = " | ".join(x for x in [role.get("location", ""), role.get("dates", "")] if x)
        para(meta, italic=True, size=9, space_after=2, color=(0x78, 0x83, 0x8F))
        for b in role.get("bullets", []):
            bullet(b)

    # --- skills ---------------------------------------------------------
    if resume.technical_skills:
        heading("Technical Skills")
        for group, items in resume.technical_skills.items():
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(1)
            rb = p.add_run(f"{group}: ")
            rb.bold = True
            rb.font.size = Pt(9.5)
            rv = p.add_run(", ".join(items))
            rv.font.size = Pt(9.5)

    if resume.certifications:
        heading("Certifications")
        para(" | ".join(resume.certifications), size=9.5)

    heading("Education")
    for e in resume.education:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(1)
        rb = p.add_run(f"{e.get('degree','')}")
        rb.bold = True
        rb.font.size = Pt(9.5)
        tail = " | ".join(x for x in [e.get("institution", ""), e.get("dates", "")] if x)
        rv = p.add_run(f"  |  {tail}")
        rv.font.size = Pt(9.5)
        if e.get("details"):
            para(str(e["details"]), italic=True, size=9, space_after=1)

    if resume.selected_achievements:
        heading("Selected Achievements")
        for a in resume.selected_achievements:
            bullet(a)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _letter_space(run, twentieths: int) -> None:
    """Apply character spacing (w:spacing) for a cleaner Material-style header."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    rPr = run._element.get_or_add_rPr()
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:val"), str(twentieths))
    rPr.append(spacing)


def _bottom_border(paragraph) -> None:
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    pPr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), "C9D8F5")
    borders.append(bottom)
    pPr.append(borders)


# --------------------------------------------------------------- MARKDOWN
def render_markdown(resume: TailoredResume, profile: Profile, job: Job, path: Path) -> Path:
    c = profile.contact
    banner = credential_banner(resume, profile)
    L: list[str] = [
        f"# {profile.name}",
        f"**{resume.target_title}**",
    ]
    if banner:
        L.append(banner)
    L += [
        " | ".join(x for x in [c.get("location"), c.get("phone"), c.get("email"), c.get("linkedin")] if x),
        "",
        f"> Tailored for **{job.title}** at **{job.company}** "
        f"({job.work_mode}{', ' + job.location if job.location else ''}) — {job.url}",
        f"> Generated by `{resume.generated_by}` | Estimated ATS alignment: **{resume.ats_alignment_score}%**",
        "",
        "## Professional Summary", resume.professional_summary, "",
    ]
    if resume.core_competencies:
        L += ["## Core Competencies", " · ".join(resume.core_competencies), ""]
    L += ["## Professional Experience"]
    for role in resume.experience:
        L += [f"### {role['title']} — {role['company']}",
              f"*{' | '.join(x for x in [role.get('location',''), role.get('dates','')] if x)}*", ""]
        L += [f"- {b}" for b in role.get("bullets", [])] + [""]
    if resume.technical_skills:
        L += ["## Technical Skills"]
        L += [f"- **{g}:** {', '.join(v)}" for g, v in resume.technical_skills.items()] + [""]
    if resume.certifications:
        L += ["## Certifications", " · ".join(resume.certifications), ""]
    L += ["## Education"]
    for e in resume.education:
        L.append(f"- **{e.get('degree','')}** — {e.get('institution','')} ({e.get('dates','')})"
                 + (f"  \n  _{e['details']}_" if e.get("details") else ""))
    if resume.selected_achievements:
        L += ["", "## Selected Achievements"] + [f"- {a}" for a in resume.selected_achievements]
    L += ["", "---", "### Tailoring Report",
          f"- **ATS keywords mirrored ({len(resume.ats_keywords_included)}):** "
          f"{', '.join(resume.ats_keywords_included) or 'n/a'}",
          f"- **Keywords intentionally omitted (not truthful):** "
          f"{', '.join(resume.keywords_intentionally_omitted) or 'none'}"]
    L += [f"- {n}" for n in resume.tailoring_notes]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ------------------------------------------------------------------- TEXT
def render_text(resume: TailoredResume, profile: Profile, job: Job, path: Path) -> Path:
    """Maximum-compatibility plain text for copy/paste into ATS web forms."""
    c = profile.contact
    L = [profile.name.upper(), resume.target_title]
    banner = credential_banner(resume, profile)
    if banner:
        L.append(banner)
    L += [" | ".join(x for x in [c.get("location"), c.get("phone"), c.get("email")] if x), ""]
    L += ["PROFESSIONAL SUMMARY", resume.professional_summary, ""]
    if resume.core_competencies:
        L += ["CORE COMPETENCIES"] + [f"- {k}" for k in resume.core_competencies] + [""]
    L += ["PROFESSIONAL EXPERIENCE", ""]
    for role in resume.experience:
        L += [f"{role['title']}", f"{role['company']} | {role.get('location','')} | {role.get('dates','')}"]
        L += [f"- {b}" for b in role.get("bullets", [])] + [""]
    if resume.technical_skills:
        L += ["TECHNICAL SKILLS"] + [f"{g}: {', '.join(v)}" for g, v in resume.technical_skills.items()] + [""]
    if resume.certifications:
        L += ["CERTIFICATIONS"] + [f"- {x}" for x in resume.certifications] + [""]
    L += ["EDUCATION"]
    for e in resume.education:
        L.append(f"{e.get('degree','')} | {e.get('institution','')} | {e.get('dates','')}")
    if resume.selected_achievements:
        L += ["", "SELECTED ACHIEVEMENTS"] + [f"- {a}" for a in resume.selected_achievements]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L).replace("\u2022", "-"), encoding="utf-8")
    return path


# ------------------------------------------------------------------- HTML
# Material-inspired presentation copy for human reviewers. The .docx/.txt remain the
# ATS submission formats; this one is print-ready (A4) so it can be saved as PDF.
_MATERIAL_CSS = """
:root{
  --ink:#1a2027; --ink-soft:#4a5563; --muted:#78838f; --rule:#e3e8ee;
  --brand:#1e4fd8; --brand-dark:#12306b; --brand-wash:#eef3ff; --accent:#0ea5e9;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:#eef1f5;color:var(--ink);
  font:14px/1.55 "Roboto","Helvetica Neue",-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
  -webkit-print-color-adjust:exact;print-color-adjust:exact}
.sheet{max-width:820px;margin:24px auto;background:#fff;border-radius:14px;overflow:hidden;
  box-shadow:0 10px 34px rgba(20,35,60,.14)}
.hero{background:linear-gradient(135deg,var(--brand-dark),var(--brand) 58%,var(--accent));
  color:#fff;padding:30px 40px 26px}
.hero h1{margin:0;font-size:31px;font-weight:700;letter-spacing:-.4px;line-height:1.15}
.hero .role{margin-top:5px;font-size:14.5px;font-weight:500;letter-spacing:.28px;
  text-transform:uppercase;color:#dbe8ff}
.hero .cred{margin-top:9px;display:inline-block;padding:4px 12px;border-radius:20px;
  background:rgba(255,255,255,.16);font-size:12px;font-weight:500;color:#f2f7ff}
.hero .contact{margin-top:14px;font-size:12.5px;color:#dae6fb;
  display:flex;flex-wrap:wrap;gap:6px 16px}
.hero .contact span{white-space:nowrap}
/* Two columns via floats. NOTE: do not use `overflow:hidden` or `display:flow-root`
   here -- both establish a BFC that WeasyPrint cannot fragment, silently truncating
   the resume to a single page. The ::after clearfix is the portable option. */

.body::after{content:"";display:table;clear:both}
.main{float:left;width:calc(100% - 275px);padding:26px 30px 30px 40px;min-width:0;box-sizing:border-box}
.side{float:right;width:275px;padding:26px 34px 30px 24px;background:#f7f9fc;
  border-left:1px solid var(--rule);box-sizing:border-box}
h2{margin:0 0 11px;font-size:11.5px;font-weight:700;letter-spacing:1.25px;
  text-transform:uppercase;color:var(--brand);display:flex;align-items:center;gap:9px}
h2::after{content:"";flex:1;height:2px;border-radius:2px;background:var(--rule)}
section{margin-bottom:22px}
section:last-child{margin-bottom:0}
p{margin:0}
.summary{color:var(--ink-soft);font-size:13.2px;text-align:justify}
.role-block{position:relative;padding-left:19px;margin-bottom:17px}
.role-block:last-child{margin-bottom:0}
.role-block::before{content:"";position:absolute;left:0;top:5px;width:9px;height:9px;
  border-radius:50%;background:var(--brand);box-shadow:0 0 0 3px var(--brand-wash)}
.role-block::after{content:"";position:absolute;left:4px;top:18px;bottom:-13px;width:1.5px;
  background:var(--rule)}
.role-block:last-child::after{display:none}
.role-title{font-size:14.5px;font-weight:700;color:var(--ink)}
.role-org{font-size:13px;font-weight:500;color:var(--brand)}
.role-meta{font-size:11.5px;color:var(--muted);margin-top:2px}
ul{margin:8px 0 0;padding-left:16px}
li{margin-bottom:4px;font-size:12.8px;color:var(--ink-soft)}
li::marker{color:var(--brand)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--brand-wash);color:var(--brand-dark);border:1px solid #d6e2fb;
  border-radius:16px;padding:4px 11px;font-size:11.5px;font-weight:500}
.skill-group{margin-bottom:10px}
.skill-group .g{font-size:11.5px;font-weight:700;color:var(--ink);margin-bottom:3px}
.skill-group .v{font-size:12px;color:var(--ink-soft)}
.edu{margin-bottom:12px}
.edu .d{font-size:12.8px;font-weight:700;color:var(--ink)}
.edu .i{font-size:12px;color:var(--ink-soft)}
.edu .m{font-size:11.5px;color:var(--muted)}
.cert{font-size:12px;color:var(--ink-soft);margin-bottom:5px;padding-left:14px;position:relative}
.cert::before{content:"";position:absolute;left:0;top:6px;width:6px;height:6px;
  border-radius:50%;background:var(--accent)}
.tag-note{margin:0 auto 26px;max-width:820px;font-size:11px;color:#7b8695;
  text-align:center;line-height:1.6}
@media print{
  @page{size:A4;margin:11mm}
  html,body{background:#fff}
  .sheet{margin:0;box-shadow:none;border-radius:0;max-width:none}
  .tag-note{display:none}
  /* Keep small units intact, but let long sections flow across pages -- pinning
     whole sections or roles leaves large empty gaps when one does not fit. */
  .edu,.skill-group,.chip,li{break-inside:avoid}
  h2{break-after:avoid}
  /* Never leave a role heading stranded at the foot of a page. */
  .role-title,.role-org,.role-meta{break-after:avoid}
  .role-block{break-inside:auto}
  ul{orphans:2;widows:2}
}
@media(max-width:720px){.main,.side{float:none;width:100%}
  .side{border-left:0;border-top:1px solid var(--rule);padding:24px 30px}}
"""


def render_html(resume: TailoredResume, profile: Profile, job: Job, path: Path) -> Path:
    """Modern Material-themed presentation resume (open in a browser, print to PDF)."""
    from html import escape as esc

    c = profile.contact
    banner = credential_banner(resume, profile)
    contact_bits = [c.get("location"), c.get("phone"), c.get("email"), c.get("linkedin")]

    H: list[str] = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{esc(profile.name)} — {esc(resume.target_title)}</title>",
        f"<style>{_MATERIAL_CSS}</style></head><body>",
        "<div class='sheet'>",
        "<header class='hero'>",
        f"<h1>{esc(profile.name)}</h1>",
        f"<div class='role'>{esc(resume.target_title)}</div>",
    ]
    if banner:
        H.append(f"<div class='cred'>{esc(banner)}</div>")
    H.append("<div class='contact'>"
             + "".join(f"<span>{esc(str(b))}</span>" for b in contact_bits if b)
             + "</div></header>")

    # ---- main column -------------------------------------------------
    H.append("<div class='body'><div class='main'>")
    if resume.professional_summary:
        H += ["<section><h2>Professional Summary</h2>",
              f"<p class='summary'>{esc(resume.professional_summary)}</p></section>"]

    H.append("<section><h2>Professional Experience</h2>")
    for role in resume.experience:
        meta = " · ".join(x for x in [role.get("location", ""), role.get("dates", "")] if x)
        H += ["<div class='role-block'>",
              f"<div class='role-title'>{esc(str(role.get('title', '')))}</div>",
              f"<div class='role-org'>{esc(str(role.get('company', '')))}</div>"]
        if meta:
            H.append(f"<div class='role-meta'>{esc(meta)}</div>")
        bullets = role.get("bullets", [])
        if bullets:
            H.append("<ul>" + "".join(f"<li>{esc(str(b))}</li>" for b in bullets) + "</ul>")
        H.append("</div>")
    H.append("</section>")

    if resume.selected_achievements:
        H += ["<section><h2>Selected Achievements</h2><ul>",
              "".join(f"<li>{esc(str(a))}</li>" for a in resume.selected_achievements),
              "</ul></section>"]
    H.append("</div>")

    # ---- sidebar -----------------------------------------------------
    H.append("<aside class='side'>")
    if resume.core_competencies:
        H += ["<section><h2>Core Competencies</h2><div class='chips'>",
              "".join(f"<span class='chip'>{esc(str(k))}</span>"
                      for k in resume.core_competencies),
              "</div></section>"]
    if resume.technical_skills:
        H.append("<section><h2>Technical Skills</h2>")
        for group, items in resume.technical_skills.items():
            H += ["<div class='skill-group'>",
                  f"<div class='g'>{esc(str(group))}</div>",
                  f"<div class='v'>{esc(', '.join(str(i) for i in items))}</div></div>"]
        H.append("</section>")
    if resume.education:
        H.append("<section><h2>Education</h2>")
        for e in resume.education:
            H += ["<div class='edu'>",
                  f"<div class='d'>{esc(str(e.get('degree', '')))}</div>",
                  f"<div class='i'>{esc(str(e.get('institution', '')))}</div>"]
            if e.get("dates"):
                H.append(f"<div class='m'>{esc(str(e['dates']))}</div>")
            H.append("</div>")
        H.append("</section>")
    if resume.certifications:
        H += ["<section><h2>Certifications</h2>",
              "".join(f"<div class='cert'>{esc(str(x))}</div>" for x in resume.certifications),
              "</section>"]
    H.append("</aside></div></div>")

    H.append(f"<div class='tag-note'>Tailored for <b>{esc(job.title)}</b> at "
             f"<b>{esc(job.company)}</b> · generated by {esc(resume.generated_by)} · "
             f"estimated ATS alignment {esc(str(resume.ats_alignment_score))}%<br>"
             "Print or save as PDF with Ctrl/Cmd+P. Submit the .docx or ATS .txt "
             "to applicant tracking systems.</div>")
    H.append("</body></html>")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(H), encoding="utf-8")
    return path


# ----------------------------------------------------------- COVER LETTER
def render_cover_letter_docx(letter: dict[str, Any], profile: Profile, job: Job, path: Path) -> Path:
    from docx import Document
    from docx.shared import Pt, Inches

    doc = Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Inches(0.8)
        s.left_margin = s.right_margin = Inches(0.9)
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    c = profile.contact
    head = doc.add_paragraph()
    r = head.add_run(profile.name)
    r.bold = True
    r.font.size = Pt(14)
    doc.add_paragraph(" | ".join(x for x in [c.get("location"), c.get("phone"), c.get("email")] if x))
    doc.add_paragraph(datetime.now().strftime("%d %B %Y"))
    doc.add_paragraph(f"{job.company}")
    doc.add_paragraph(f"Re: {job.title}")
    doc.add_paragraph(letter.get("greeting", "Dear Hiring Manager,"))
    for p in letter.get("paragraphs", []):
        doc.add_paragraph(str(p))
    doc.add_paragraph(letter.get("sign_off", "Sincerely,"))
    doc.add_paragraph(profile.name)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def render_cover_letter_md(letter: dict[str, Any], profile: Profile, job: Job, path: Path) -> Path:
    L = [f"# Cover Letter — {job.title} @ {job.company}", "",
         datetime.now().strftime("%d %B %Y"), "", letter.get("greeting", "Dear Hiring Manager,"), ""]
    L += [str(p) + "\n" for p in letter.get("paragraphs", [])]
    L += [letter.get("sign_off", "Sincerely,"), profile.name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ------------------------------------------------------------------- ALL
def render_all(resume: TailoredResume, profile: Profile, job: Job,
               base_dir: Path | None = None) -> dict[str, Path]:
    import json as _json

    paths = output_paths(job, base_dir)
    written: dict[str, Path] = {
        "docx": render_docx(resume, profile, job, paths["docx"]),
        "html": render_html(resume, profile, job, paths["html"]),
        "md": render_markdown(resume, profile, job, paths["md"]),
        "txt": render_text(resume, profile, job, paths["txt"]),
    }
    # Best-effort: a missing browser/WeasyPrint must not fail resume generation.
    pdf = html_to_pdf(written["html"], paths["pdf"])
    if pdf is not None:
        written["pdf"] = pdf
    payload = {"job": job.to_dict(), "resume": resume.to_dict()}
    paths["json"].write_text(_json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    written["json"] = paths["json"]
    return written
