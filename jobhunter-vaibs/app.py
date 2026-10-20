#!/usr/bin/env python3
"""jobhunter-vaibs: Dedicated Web Application for Vaibhav Bramhankar.

High-alignment Job Search, Tailored ATS Resume Engine & Candidate Portal.
Uses config/profile_vaibhav.yaml as single source of truth.
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jobhunter.config import Profile, load_model_config, load_profile
from jobhunter.models import Job
from jobhunter.resume.pdf import html_to_pdf
from jobhunter.resume.render import render_docx, render_text
from jobhunter.resume.tailor import TailoredResume, deterministic_tailor
from jobhunter.scoring import deterministic_score

log = logging.getLogger("jobhunter-vaibs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

app = FastAPI(
    title="JobHunter Vaibs - Executive Career & Tailored Resume Portal",
    description="Autonomous Job Search and ATS Resume Tailoring Engine for Vaibhav Bramhankar",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROFILE_PATH = ROOT / "config" / "profile_vaibhav.yaml"
OUTPUT_DIR = ROOT / "data" / "output" / "2026-09-27" / "Vaibhav_BI_Analytics_Resume"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def get_profile() -> Profile:
    return load_profile(str(PROFILE_PATH))


# -----------------------------------------------------------------------------
# REST API ENDPOINTS
# -----------------------------------------------------------------------------

@app.get("/api/health")
def api_health() -> dict[str, Any]:
    prof = get_profile()
    return {
        "status": "ok",
        "app": "jobhunter-vaibs",
        "candidate": prof.name,
        "headline": prof.headline,
        "location": prof.contact.get("location"),
        "yoe": prof.years_experience,
        "profile_file": str(PROFILE_PATH.name),
    }


@app.get("/api/profile")
def api_profile() -> dict[str, Any]:
    prof = get_profile()
    return prof.raw


@app.get("/api/resume/preview", response_class=HTMLResponse)
def api_resume_preview() -> HTMLResponse:
    html_file = OUTPUT_DIR / "Vaibhav_Bramhankar_BI_Analytics_Resume.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="Resume HTML not found")
    return HTMLResponse(content=html_file.read_text(encoding="utf-8"))


@app.get("/api/resume/download/{fmt}")
def api_resume_download(fmt: str) -> FileResponse:
    ext_map = {
        "pdf": ("Vaibhav_Bramhankar_BI_Analytics_Resume.pdf", "application/pdf"),
        "docx": ("Vaibhav_Bramhankar_BI_Analytics_Resume.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "html": ("Vaibhav_Bramhankar_BI_Analytics_Resume.html", "text/html"),
        "txt": ("Vaibhav_Bramhankar_BI_Analytics_Resume.txt", "text/plain"),
    }
    fmt_key = fmt.lower().strip(".")
    if fmt_key not in ext_map:
        raise HTTPException(status_code=400, detail=f"Invalid format {fmt}. Choose from: pdf, docx, html, txt")

    filename, media_type = ext_map[fmt_key]
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {filename} not found")

    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type=media_type,
    )


@app.post("/api/tailor")
async def api_tailor_jd(request: Request) -> dict[str, Any]:
    """Tailor Vaibhav's resume to an arbitrary Job Description."""
    data = await request.json()
    jd_title = data.get("title") or data.get("job_title", "Senior Data Analyst / BI Developer")
    jd_company = data.get("company", "Target Employer")
    jd_text = data.get("description") or data.get("job_description", "")
    if not jd_text.strip():
        raise HTTPException(status_code=400, detail="Job description text is required.")

    prof = get_profile()
    job = Job(
        source="custom",
        title=jd_title,
        company=jd_company,
        url="",
        description=jd_text,
        location="Mumbai, India",
        work_mode="hybrid",
    )

    score = deterministic_score(job, prof)
    tailored = deterministic_tailor(job, prof, score.ats_keywords)
    return {
        "match_score": score.match_score,
        "breakdown": score.breakdown.to_dict() if hasattr(score.breakdown, "to_dict") else score.breakdown,
        "matched_competencies": score.matched_competencies,
        "ats_keywords": score.ats_keywords,
        "tailored_summary": tailored.professional_summary,
        "tailored_title": tailored.target_title,
        "tailored_bullets_count": sum(len(r.get("bullets", [])) for r in tailored.experience),
    }


# -----------------------------------------------------------------------------
# WEB DASHBOARD (HTML UI)
# -----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    prof = get_profile()
    c = prof.contact
    esc = html.escape

    exp_blocks = ""
    for r in prof.experience:
        bullets = "".join(f"<li>{esc(b['text'])}</li>" for b in r.get("bullets", []))
        meta = " · ".join(x for x in [r.get("location", ""), r.get("dates", "")] if x)
        exp_blocks += f"""
        <div class="exp-card">
            <div class="exp-header">
                <div>
                    <h3 class="exp-title">{esc(r.get('title', ''))}</h3>
                    <div class="exp-company">{esc(r.get('company', ''))}</div>
                </div>
                <div class="exp-meta">{esc(meta)}</div>
            </div>
            <ul class="exp-bullets">{bullets}</ul>
        </div>
        """

    comp_chips = "".join(f'<span class="chip">{esc(comp.name)}</span>' for comp in prof.competencies)

    skills_html = ""
    for group, items in prof.skills.items():
        skills_html += f"""
        <div class="skill-group">
            <strong>{esc(group)}</strong>
            <div class="skill-items">{esc(', '.join(items))}</div>
        </div>
        """

    certs_html = "".join(f"<li>{esc(x)}</li>" for x in prof.certifications)
    achieve_html = "".join(f"<li>{esc(x)}</li>" for x in prof.achievements)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>JobHunter - Vaibhav Bramhankar</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --primary: #1e4fd8;
            --primary-dark: #12306b;
            --accent: #0ea5e9;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text-main: #0f172a;
            --text-muted: #64748b;
            --border: #e2e8f0;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, sans-serif;
            background: var(--bg);
            color: var(--text-main);
            line-height: 1.6;
        }}
        header.top-bar {{
            background: linear-gradient(135deg, var(--primary-dark), var(--primary) 60%, var(--accent));
            color: #fff;
            padding: 40px 24px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
        }}
        .container {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 0 16px;
        }}
        .hero-title {{ font-size: 32px; font-weight: 700; margin-bottom: 6px; }}
        .hero-sub {{ font-size: 16px; color: #dbeafe; font-weight: 500; margin-bottom: 16px; }}
        .hero-contact {{ display: flex; flex-wrap: wrap; gap: 12px 20px; font-size: 14px; color: #eff6ff; }}
        .hero-contact a {{ color: #fff; text-decoration: underline; }}
        
        .nav-tabs {{
            display: flex;
            gap: 10px;
            margin: 24px auto 0;
            max-width: 1100px;
            padding: 0 16px;
            border-bottom: 1px solid var(--border);
        }}
        .tab-btn {{
            background: none;
            border: none;
            font-size: 15px;
            font-weight: 600;
            padding: 12px 18px;
            color: var(--text-muted);
            cursor: pointer;
            border-bottom: 3px solid transparent;
            transition: all 0.2s;
        }}
        .tab-btn.active {{
            color: var(--primary);
            border-bottom-color: var(--primary);
        }}
        
        .main-body {{
            max-width: 1100px;
            margin: 28px auto 60px;
            padding: 0 16px;
        }}
        .tab-content {{ display: none; }}
        .tab-content.active {{ display: block; }}
        
        .grid-2 {{
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 24px;
        }}
        @media (max-width: 840px) {{
            .grid-2 {{ grid-template-columns: 1fr; }}
        }}
        
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        }}
        .card-title {{
            font-size: 17px;
            font-weight: 700;
            color: var(--primary);
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 2px solid #eef2ff;
        }}
        
        .summary-p {{ font-size: 14.5px; color: #334155; line-height: 1.7; }}
        
        .exp-card {{
            margin-bottom: 20px;
            padding-bottom: 18px;
            border-bottom: 1px dashed var(--border);
        }}
        .exp-card:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
        .exp-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 8px;
            flex-wrap: wrap;
            gap: 4px;
        }}
        .exp-title {{ font-size: 15px; font-weight: 700; color: #0f172a; }}
        .exp-company {{ font-size: 14px; font-weight: 600; color: var(--primary); }}
        .exp-meta {{ font-size: 13px; color: var(--text-muted); }}
        .exp-bullets {{ padding-left: 20px; font-size: 13.8px; color: #334155; margin-top: 6px; }}
        .exp-bullets li {{ margin-bottom: 6px; }}
        
        .chips-wrap {{ display: flex; flex-wrap: wrap; gap: 6px; }}
        .chip {{
            background: #eff6ff;
            color: #1e40af;
            border: 1px solid #bfdbfe;
            border-radius: 20px;
            padding: 4px 12px;
            font-size: 12.5px;
            font-weight: 500;
        }}
        
        .skill-group {{ margin-bottom: 12px; }}
        .skill-group strong {{ font-size: 13px; color: #1e293b; display: block; }}
        .skill-items {{ font-size: 13.5px; color: #475569; margin-top: 2px; }}
        
        .download-btn-group {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 14px;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 10px 18px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
            border: none;
            transition: all 0.2s;
        }}
        .btn-primary {{ background: var(--primary); color: #fff; }}
        .btn-primary:hover {{ background: var(--primary-dark); }}
        .btn-outline {{
            background: #fff;
            color: var(--primary);
            border: 1px solid var(--primary);
        }}
        .btn-outline:hover {{ background: #eff6ff; }}
        
        .iframe-container {{
            width: 100%;
            height: 750px;
            border: 1px solid var(--border);
            border-radius: 8px;
            overflow: hidden;
            margin-top: 16px;
        }}
        iframe {{ width: 100%; height: 100%; border: none; }}
        
        /* Tailor Form */
        .form-group {{ margin-bottom: 16px; }}
        label {{ font-size: 13.5px; font-weight: 600; color: #1e293b; display: block; margin-bottom: 6px; }}
        input[type="text"], textarea {{
            width: 100%;
            padding: 10px 14px;
            border: 1px solid var(--border);
            border-radius: 8px;
            font-family: inherit;
            font-size: 14px;
        }}
        textarea {{ height: 160px; resize: vertical; }}
        .score-badge {{
            display: inline-block;
            padding: 6px 14px;
            border-radius: 20px;
            font-weight: 700;
            font-size: 15px;
            background: #dcfce7;
            color: #15803d;
            margin-bottom: 12px;
        }}
    </style>
</head>
<body>

<header class="top-bar">
    <div class="container">
        <h1 class="hero-title">{esc(prof.name)}</h1>
        <div class="hero-sub">{esc(prof.headline)}</div>
        <div class="hero-contact">
            <span>📍 {esc(c.get('location', ''))}</span>
            <span>📞 {esc(c.get('phone', ''))}</span>
            <span>✉️ {esc(c.get('email', ''))}</span>
            <span>🔗 <a href="{esc(c.get('linkedin', ''))}" target="_blank">LinkedIn Profile</a></span>
            <span>💼 {prof.years_experience}+ Years Experience</span>
        </div>
    </div>
</header>

<div class="nav-tabs">
    <button class="tab-btn active" onclick="switchTab('profile')">Profile & Experience</button>
    <button class="tab-btn" onclick="switchTab('resume')">Live Resume & Downloads</button>
    <button class="tab-btn" onclick="switchTab('tailor')">JD Match & Instant Tailor</button>
</div>

<div class="main-body">
    <!-- TAB 1: PROFILE & EXPERIENCE -->
    <div id="tab-profile" class="tab-content active">
        <div class="grid-2">
            <div>
                <div class="card">
                    <h2 class="card-title">Professional Executive Summary</h2>
                    <p class="summary-p">{esc(prof.summary)}</p>
                </div>
                <div class="card">
                    <h2 class="card-title">Career History & Milestones</h2>
                    {exp_blocks}
                </div>
            </div>
            <div>
                <div class="card">
                    <h2 class="card-title">Core Competencies</h2>
                    <div class="chips-wrap">{comp_chips}</div>
                </div>
                <div class="card">
                    <h2 class="card-title">Technical Skill Stack</h2>
                    {skills_html}
                </div>
                <div class="card">
                    <h2 class="card-title">Education & Credentials</h2>
                    <ul style="padding-left:18px; font-size:13.5px; color:#334155;">
                        <li><strong>MBA</strong> — ITM University, Mumbai (2014–2016)</li>
                        <li><strong>B.E.</strong> — RTM Nagpur University (2009–2013)</li>
                    </ul>
                </div>
                <div class="card">
                    <h2 class="card-title">Certifications</h2>
                    <ul style="padding-left:18px; font-size:13px; color:#475569;">{certs_html}</ul>
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 2: RESUME & DOWNLOADS -->
    <div id="tab-resume" class="tab-content">
        <div class="card">
            <h2 class="card-title">ATS-Optimized Blue Modern Resume (2-Page A4)</h2>
            <p style="font-size:14px; color:var(--text-muted); margin-bottom:12px;">
                Strictly formatted for 2 pages with single-column ATS parsability, high visual polish, and verified metrics.
            </p>
            <div class="download-btn-group">
                <a href="/api/resume/download/pdf" class="btn btn-primary" download>📄 Download PDF (2-Page)</a>
                <a href="/api/resume/download/docx" class="btn btn-outline" download>📝 Download Word (.docx)</a>
                <a href="/api/resume/download/html" class="btn btn-outline" download>🌐 Download HTML</a>
                <a href="/api/resume/download/txt" class="btn btn-outline" download>📋 Download ATS Plain Text</a>
            </div>
            <div class="iframe-container">
                <iframe src="/api/resume/preview"></iframe>
            </div>
        </div>
    </div>

    <!-- TAB 3: TAILOR -->
    <div id="tab-tailor" class="tab-content">
        <div class="card">
            <h2 class="card-title">Targeted JD Fit Scoring & Tailor Engine</h2>
            <p style="font-size:14px; color:var(--text-muted); margin-bottom:18px;">
                Paste any job description to evaluate keyword alignment, calculate match percentage against Vaibhav's ground truth, and generate a tailored summary.
            </p>
            <div class="form-group">
                <label>Job Title</label>
                <input type="text" id="jd-title" value="Senior Data Analyst / BI Developer" placeholder="e.g. Lead BI Engineer">
            </div>
            <div class="form-group">
                <label>Company Name</label>
                <input type="text" id="jd-company" value="Global Financial Institution" placeholder="e.g. Morgan Stanley">
            </div>
            <div class="form-group">
                <label>Job Description Text</label>
                <textarea id="jd-text" placeholder="Paste the full job description text here..."></textarea>
            </div>
            <button class="btn btn-primary" onclick="runTailor()">⚡ Evaluate Match & Tailor</button>

            <div id="tailor-results" style="display:none; margin-top:24px; padding-top:20px; border-top:1px solid var(--border);">
                <div class="score-badge" id="res-score">Match: 0%</div>
                <h3 style="font-size:16px; margin-bottom:8px;">Transferable Summary Alignment:</h3>
                <p id="res-summary" class="summary-p" style="background:#f1f5f9; padding:16px; border-radius:8px;"></p>
                <div style="margin-top:16px;">
                    <strong>Matched Competencies:</strong>
                    <div class="chips-wrap" id="res-competencies" style="margin-top:6px;"></div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
function switchTab(tabId) {{
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('tab-' + tabId).classList.add('active');
}}

async function runTailor() {{
    const title = document.getElementById('jd-title').value;
    const company = document.getElementById('jd-company').value;
    const text = document.getElementById('jd-text').value;
    if (!text.trim()) {{
        alert('Please paste a job description first.');
        return;
    }}
    const resDiv = document.getElementById('tailor-results');
    resDiv.style.display = 'block';
    resDiv.scrollIntoView({{ behavior: 'smooth' }});
    document.getElementById('res-score').innerText = 'Analyzing match...';

    try {{
        const resp = await fetch('/api/tailor', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ title, company, description: text }})
        }});
        const data = await resp.json();
        document.getElementById('res-score').innerText = 'Alignment Fit: ' + Math.round(data.match_score) + '%';
        document.getElementById('res-summary').innerText = data.tailored_summary;
        const compDiv = document.getElementById('res-competencies');
        compDiv.innerHTML = '';
        (data.matched_competencies || []).forEach(c => {{
            const span = document.createElement('span');
            span.className = 'chip';
            span.innerText = c;
            compDiv.appendChild(span);
        }});
    }} catch (e) {{
        alert('Error scoring job: ' + e);
    }}
}}
</script>

</body>
</html>
"""
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8085"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"Starting jobhunter-vaibs web server on http://{host}:{port}")
    uvicorn.run("app:app", host=host, port=port, reload=False)
