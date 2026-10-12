"""
HireLens AI engine.
Works fully offline (TF-IDF + skill taxonomy). If ANTHROPIC_API_KEY is set,
text-generation features (JD writer, summary, bullet rewrite) use Claude.
"""
import io
import json
import math
import os
import re
import urllib.request
from collections import Counter

# --------------------------------------------------------------------------
# Skill taxonomy  (canonical name -> aliases)
# --------------------------------------------------------------------------
SKILLS = {
    "python": ["python", "py"], "java": ["java"], "javascript": ["javascript", "js", "ecmascript"],
    "typescript": ["typescript", "ts"], "c": [" c ", "c language"], "c++": ["c++", "cpp"],
    "c#": ["c#", "csharp"], "go": ["golang", "go lang"], "rust": ["rust"], "php": ["php"],
    "kotlin": ["kotlin"], "swift": ["swift"], "r": ["r programming", "rstudio", "r language"],
    "sql": ["sql", "t-sql", "pl/sql"], "mysql": ["mysql"], "postgresql": ["postgresql", "postgres"],
    "sqlite": ["sqlite", "sqlite3"], "mongodb": ["mongodb", "mongo"], "redis": ["redis"],
    "html": ["html", "html5"], "css": ["css", "css3"], "sass": ["sass", "scss"],
    "react": ["react", "reactjs", "react.js"], "angular": ["angular"], "vue": ["vue", "vuejs", "vue.js"],
    "next.js": ["next.js", "nextjs"], "node.js": ["node.js", "nodejs", "node js"],
    "express": ["express.js", "expressjs", "express"], "flask": ["flask"], "django": ["django"],
    "fastapi": ["fastapi"], "spring boot": ["spring boot", "springboot", "spring"],
    "tailwind": ["tailwind", "tailwindcss"], "bootstrap": ["bootstrap"],
    "rest api": ["rest api", "restful", "rest apis", "rest"], "graphql": ["graphql"],
    "docker": ["docker"], "kubernetes": ["kubernetes", "k8s"], "aws": ["aws", "amazon web services"],
    "azure": ["azure"], "gcp": ["gcp", "google cloud"], "linux": ["linux", "ubuntu", "unix"],
    "git": ["git", "github", "gitlab"], "ci/cd": ["ci/cd", "cicd", "jenkins", "github actions"],
    "terraform": ["terraform"], "nginx": ["nginx"],
    "machine learning": ["machine learning", "ml"], "deep learning": ["deep learning"],
    "nlp": ["nlp", "natural language processing"], "computer vision": ["computer vision", "opencv"],
    "tensorflow": ["tensorflow"], "pytorch": ["pytorch"], "scikit-learn": ["scikit-learn", "sklearn", "scikit learn"],
    "pandas": ["pandas"], "numpy": ["numpy"], "matplotlib": ["matplotlib"], "plotly": ["plotly"],
    "data analysis": ["data analysis", "data analytics"], "data visualization": ["data visualization", "data visualisation"],
    "power bi": ["power bi", "powerbi"], "tableau": ["tableau"], "excel": ["excel", "ms excel"],
    "statistics": ["statistics", "statistical"], "etl": ["etl"], "spark": ["spark", "pyspark"],
    "hadoop": ["hadoop"], "airflow": ["airflow"], "llm": ["llm", "llms", "large language model", "generative ai", "genai"],
    "iot": ["iot", "internet of things"], "arduino": ["arduino"], "raspberry pi": ["raspberry pi"],
    "node-red": ["node-red", "node red"], "embedded systems": ["embedded", "embedded systems"],
    "android": ["android"], "ios": ["ios"], "flutter": ["flutter"], "react native": ["react native"],
    "figma": ["figma"], "ui/ux": ["ui/ux", "ux design", "ui design", "user experience"],
    "testing": ["unit testing", "pytest", "jest", "selenium", "qa", "test automation"],
    "agile": ["agile", "scrum", "kanban"], "jira": ["jira"],
    "data structures": ["data structures", "dsa", "algorithms"], "oop": ["oop", "object oriented", "object-oriented"],
    "microservices": ["microservices"], "system design": ["system design"],
    "cybersecurity": ["cybersecurity", "cyber security", "infosec", "owasp"], "networking": ["networking", "tcp/ip"],
    "communication": ["communication"], "leadership": ["leadership", "team lead"],
    "problem solving": ["problem solving", "problem-solving"], "project management": ["project management"],
    "seo": ["seo"], "digital marketing": ["digital marketing"], "content writing": ["content writing", "copywriting"],
    "sales": ["sales", "business development"], "recruitment": ["recruitment", "talent acquisition", "hr"],
    "accounting": ["accounting", "tally", "gst"], "customer support": ["customer support", "customer service"],
}

_ALIAS_INDEX = []
for canon, aliases in SKILLS.items():
    for a in set(aliases + [canon]):
        _ALIAS_INDEX.append((canon, a.strip().lower(), a.startswith(" ")))

ACTION_VERBS = set("""built developed designed implemented created led managed launched optimized improved
reduced increased delivered automated deployed engineered architected analyzed integrated migrated mentored
streamlined collaborated established achieved drove owned scaled secured tested refactored shipped
trained coordinated researched resolved generated visualized configured""".split())

SECTION_HEADS = {
    "summary": ["summary", "profile", "objective", "about me"],
    "experience": ["experience", "work history", "employment", "internship"],
    "education": ["education", "academic", "qualification"],
    "skills": ["skills", "technical skills", "core competencies", "technologies"],
    "projects": ["projects", "project work"],
    "certifications": ["certification", "certifications", "courses", "licenses"],
}

STOP = set("""a an the and or of to in for on with at by from as is are was were be been this that these those it its
we you your our their will can should may must have has had not but if than then so such into over under about
across per etc using use used work working experience strong good knowledge ability able looking role team
candidate responsibilities requirements required preferred plus years year""".split())


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------
def tokenize(text):
    return [w for w in re.findall(r"[a-z][a-z0-9+#.\-]{1,}", (text or "").lower()) if w not in STOP and len(w) > 2]


def extract_skills(text):
    t = " " + re.sub(r"[\n\r\t,;|/()•·]", " ", (text or "").lower()) + " "
    t = re.sub(r"\s+", " ", t)
    found = []
    for canon, alias, pad in _ALIAS_INDEX:
        if canon in found:
            continue
        if alias in ("c", "r", "go", "ts", "js", "py", "ml", "hr", "rest", "spring", "express", "swift", "qa"):
            # short / ambiguous aliases need word boundaries
            if re.search(rf"(?<![a-z0-9+#.]){re.escape(alias)}(?![a-z0-9+#])", t) and alias not in ("c", "r", "go", "hr", "qa", "rest", "swift", "spring", "express"):
                found.append(canon)
            continue
        if alias.startswith("c ") or alias == "c":
            continue
        if re.search(rf"(?<![a-z0-9+#.]){re.escape(alias)}(?![a-z0-9+#])", t):
            found.append(canon)
    # ambiguous single-letter / common-word skills only when explicit
    if re.search(r"(?<![a-z])c\s*(,|/|programming|language)", t) or re.search(r"\bc\b(?=.*(java|python|c\+\+))", t):
        if "c" not in found:
            found.append("c")
    if re.search(r"\br (programming|language|studio)\b|\bpython,? r\b|\br,? python\b|\br\b(?=\s*(,|/)\s*(sql|python))", t):
        if "r" not in found:
            found.append("r")
    if re.search(r"\bgolang\b|\bgo (developer|programming|language)\b", t) and "go" not in found:
        found.append("go")
    if re.search(r"\b(express\.js|expressjs)\b|\bnode\b.*\bexpress\b", t) and "express" not in found:
        found.append("express")
    if re.search(r"\bspring boot\b|\bspringboot\b|\bspring framework\b", t) and "spring boot" not in found:
        found.append("spring boot")
    if re.search(r"\b(restful|rest api|rest apis)\b", t) and "rest api" not in found:
        found.append("rest api")
    return found


def tfidf_cosine(a, b):
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    docs = [Counter(ta), Counter(tb)]
    vocab = set(docs[0]) | set(docs[1])
    idf = {w: math.log((1 + 2) / (1 + sum(1 for d in docs if w in d))) + 1 for w in vocab}

    def vec(c):
        n = sum(c.values())
        return {w: (c[w] / n) * idf[w] for w in c}
    va, vb = vec(docs[0]), vec(docs[1])
    dot = sum(va[w] * vb.get(w, 0) for w in va)
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return dot / (na * nb) if na and nb else 0.0


def estimate_years(text):
    yrs = [int(x) for x in re.findall(r"(\d{1,2})\+?\s*(?:years|yrs)", (text or "").lower())]
    return max(yrs) if yrs else 0


def jd_min_years(text):
    m = re.search(r"(\d{1,2})\s*(?:\+|-\s*\d+)?\s*(?:years|yrs)", (text or "").lower())
    return int(m.group(1)) if m else 0


def parse_resume_file(filename, data):
    name = (filename or "").lower()
    try:
        if name.endswith(".pdf"):
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        if name.endswith(".docx"):
            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs).strip()
        return data.decode("utf-8", errors="ignore").strip()
    except Exception as e:  # noqa
        return ""


def resume_json_to_text(r):
    """Flatten builder JSON to plain text for scoring."""
    if not r:
        return ""
    out = [r.get("name", ""), r.get("headline", ""), r.get("email", ""), r.get("phone", ""),
           r.get("location", ""), r.get("links", "")]
    if r.get("summary"):
        out += ["SUMMARY", r["summary"]]
    if r.get("skills"):
        out += ["SKILLS", r["skills"]]
    if r.get("experience"):
        out.append("EXPERIENCE")
        for e in r["experience"]:
            out += [f'{e.get("role","")} - {e.get("company","")} ({e.get("period","")})', e.get("bullets", "")]
    if r.get("projects"):
        out.append("PROJECTS")
        for p in r["projects"]:
            out += [f'{p.get("name","")} ({p.get("tech","")})', p.get("bullets", "")]
    if r.get("education"):
        out.append("EDUCATION")
        for e in r["education"]:
            out += [f'{e.get("degree","")} - {e.get("school","")} ({e.get("period","")}) {e.get("score","")}']
    if r.get("certifications"):
        out += ["CERTIFICATIONS", r["certifications"]]
    return "\n".join(x for x in out if x)


# --------------------------------------------------------------------------
# ATS scoring
# --------------------------------------------------------------------------
def detect_sections(text):
    low = text.lower()
    found = {}
    for key, heads in SECTION_HEADS.items():
        found[key] = any(re.search(rf"(^|\n)\s*{re.escape(h)}s?\s*[:\n]", low) or re.search(rf"(^|\n)\s*{re.escape(h)}\b", low) for h in heads)
    return found


def ats_score(resume_text, jd_text=""):
    """Returns dict(score, grade, breakdown[list], tips[list], skills[list], jd{...})"""
    text = resume_text or ""
    low = text.lower()
    words = len(text.split())
    breakdown, tips = [], []

    # 1 contact (10)
    has_email = bool(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text))
    has_phone = bool(re.search(r"(\+?\d[\d\s-]{8,}\d)", text))
    has_link = bool(re.search(r"(linkedin\.com|github\.com|portfolio|https?://)", low))
    c = (4 if has_email else 0) + (3 if has_phone else 0) + (3 if has_link else 0)
    breakdown.append(("Contact details", c, 10))
    if not has_email: tips.append("Add a professional email address at the top.")
    if not has_phone: tips.append("Add a phone number so recruiters can reach you.")
    if not has_link: tips.append("Link your LinkedIn or GitHub profile.")

    # 2 sections (20)
    secs = detect_sections(text)
    core = ["experience", "education", "skills", "projects", "summary"]
    s = sum(4 for k in core if secs.get(k))
    if not secs.get("experience") and secs.get("projects"):
        s = min(20, s + 2)
    breakdown.append(("Standard sections", min(s, 20), 20))
    for k in core:
        if not secs.get(k):
            tips.append(f"Add a clear “{k.title()}” section heading — ATS parsers look for it.")

    # 3 skills (20)
    skills = extract_skills(text)
    sk = min(20, len(skills) * 2)
    breakdown.append(("Skills coverage", sk, 20))
    if len(skills) < 8: tips.append("List more relevant technical skills (aim for 8–15).")

    # 4 impact (15)
    nums = len(re.findall(r"\b\d+(?:\.\d+)?\s?(?:%|x|k|\+|lpa|users|clients|projects|hours|ms|rs|₹)?", text))
    imp = min(15, nums * 2)
    breakdown.append(("Quantified impact", imp, 15))
    if nums < 5: tips.append("Quantify achievements — e.g. “cut load time by 40%”, “served 2,000+ users”.")

    # 5 action verbs (10)
    verbs = {w for w in re.findall(r"[a-z]+", low) if w in ACTION_VERBS}
    av = min(10, len(verbs) * 2)
    breakdown.append(("Action verbs", av, 10))
    if len(verbs) < 5: tips.append("Start bullets with strong verbs: Built, Led, Optimized, Automated, Deployed.")

    # 6 length / format (10)
    if 250 <= words <= 900: ln = 10
    elif 150 <= words < 250 or 900 < words <= 1200: ln = 6
    else: ln = 2
    breakdown.append(("Length & readability", ln, 10))
    if words < 250: tips.append("Resume looks thin — expand projects and experience with detail.")
    if words > 900: tips.append("Resume is long — trim to 1–2 pages of the most relevant content.")

    # 7 JD keyword match (15) – only when JD supplied
    jd_info = None
    if jd_text and jd_text.strip():
        jd_sk = extract_skills(jd_text)
        matched = [k for k in jd_sk if k in skills]
        missing = [k for k in jd_sk if k not in skills]
        cov = (len(matched) / len(jd_sk)) if jd_sk else 0
        sim = tfidf_cosine(text, jd_text)
        kw = round(15 * (0.7 * cov + 0.3 * min(1, sim * 2.2)))
        breakdown.append(("Job description match", kw, 15))
        jd_info = {"matched": matched, "missing": missing, "similarity": round(sim * 100)}
        if missing:
            tips.insert(0, "Missing keywords from the JD: " + ", ".join(missing[:8]) + ". Add them where truthful.")
        total_max = 100
    else:
        total_max = 85

    got = sum(b[1] for b in breakdown)
    score = round(100 * got / total_max)
    grade = "Excellent" if score >= 80 else "Good" if score >= 65 else "Needs work" if score >= 45 else "Weak"
    return {
        "score": min(100, score), "grade": grade,
        "breakdown": [{"label": l, "got": g, "max": m} for l, g, m in breakdown],
        "tips": tips[:8], "skills": skills, "jd": jd_info, "words": words,
    }


# --------------------------------------------------------------------------
# Resume <-> Job matching (used for recommendations & shortlisting)
# --------------------------------------------------------------------------
def match_resume_to_job(resume_text, job_title, job_desc, job_skills):
    """Returns dict(score 0-100, skill_score, sim_score, exp_score, matched, missing, reason)."""
    req = [s.strip().lower() for s in (job_skills or "").split(",") if s.strip()]
    if not req:
        req = extract_skills(job_desc)
    r_sk = set(extract_skills(resume_text))
    # also let plain substring match catch custom skills the taxonomy doesn't know
    low = (resume_text or "").lower()
    matched = [s for s in req if s in r_sk or re.search(rf"(?<![a-z0-9]){re.escape(s)}(?![a-z0-9])", low)]
    missing = [s for s in req if s not in matched]
    skill_score = (len(matched) / len(req)) if req else 0.5
    sim = tfidf_cosine(resume_text, f"{job_title} {job_title} {job_desc} {' '.join(req)}")
    sim_score = min(1.0, sim * 2.4)
    need = jd_min_years(job_desc)
    have = estimate_years(resume_text)
    exp_score = 1.0 if need == 0 else min(1.0, (have + 0.5) / need) if have else 0.35
    title_hit = 1.0 if any(w in low for w in tokenize(job_title) if len(w) > 3) else 0.0
    score = round(100 * (0.55 * skill_score + 0.25 * sim_score + 0.12 * exp_score + 0.08 * title_hit))
    reason = _reason(score, matched, missing, have, need)
    return {
        "score": max(0, min(100, score)), "skill_score": round(skill_score * 100),
        "sim_score": round(sim_score * 100), "exp_score": round(exp_score * 100),
        "matched": matched, "missing": missing, "reason": reason,
    }


def _reason(score, matched, missing, have, need):
    parts = []
    if matched:
        parts.append(f"Matches {len(matched)} required skill{'s' if len(matched)!=1 else ''} ({', '.join(matched[:5])}{'…' if len(matched)>5 else ''}).")
    if missing:
        parts.append(f"Missing {', '.join(missing[:4])}{'…' if len(missing)>4 else ''}.")
    if need and have and have < need:
        parts.append(f"Stated experience (~{have}y) is below the {need}y asked.")
    elif need and have:
        parts.append(f"Experience (~{have}y) meets the {need}y requirement.")
    return " ".join(parts) or "Not enough information in the resume to judge."


def recommend_jobs(resume_text, jobs, limit=10):
    out = []
    for j in jobs:
        m = match_resume_to_job(resume_text, j["title"], j["description"], j["skills"])
        out.append({**dict(j), "match": m})
    out.sort(key=lambda x: x["match"]["score"], reverse=True)
    return out[:limit]


# --------------------------------------------------------------------------
# Text generation (LLM optional, rule-based fallback)
# --------------------------------------------------------------------------
def llm_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _llm(prompt, system="You are a concise expert recruiter and resume writer.", max_tokens=900):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    except Exception:
        return None


def generate_jd(title, company="", level="", skills="", location=""):
    sk = [s.strip() for s in skills.split(",") if s.strip()] or _default_skills(title)
    ai = _llm(
        f"Write a job description for a '{title}' role{' at ' + company if company else ''}"
        f"{', ' + level + ' level' if level else ''}{', based in ' + location if location else ''}. "
        f"Key skills: {', '.join(sk)}. Use plain text with these headings exactly: About the role, "
        "Responsibilities, Requirements, Nice to have, What we offer. Use '- ' bullets. Max 260 words.")
    if ai:
        return {"description": ai, "skills": ", ".join(sk), "source": "claude"}
    lvl = f"{level} " if level else ""
    desc = f"""About the role
{company or 'We'} {'is' if company else 'are'} hiring a {lvl}{title}{(' (remote)' if location.strip().lower()=='remote' else ' in ' + location) if location else ''}. You will own meaningful work end-to-end, collaborate with a small cross-functional team and ship features that real users depend on.

Responsibilities
- Design, build and maintain high-quality solutions as a {title}.
- Collaborate with product, design and engineering to turn requirements into working software.
- Write clean, tested, well-documented work and take part in code reviews.
- Monitor performance, fix issues quickly and keep improving what we ship.

Requirements
- Hands-on experience with {', '.join(sk[:5])}.
- Solid problem-solving and communication skills.
- {'2+ years of relevant experience.' if level.lower() in ('mid', 'senior', 'lead') else 'Degree in a relevant field or equivalent project experience.'}

Nice to have
- {', '.join(sk[5:9]) if len(sk) > 5 else 'Open-source or side-project contributions'}.
- Experience working in an agile team.

What we offer
- Competitive pay, learning budget and flexible working.
- A supportive team and room to grow."""
    return {"description": desc, "skills": ", ".join(sk), "source": "template"}


def _default_skills(title):
    t = title.lower()
    table = [
        (("full stack", "fullstack"), ["javascript", "react", "node.js", "sql", "rest api", "git", "html", "css"]),
        (("frontend", "front end", "ui"), ["javascript", "react", "html", "css", "typescript", "git"]),
        (("backend", "back end"), ["python", "flask", "sql", "rest api", "docker", "git", "postgresql"]),
        (("data scien", "ml", "machine learning", "ai "), ["python", "machine learning", "pandas", "numpy", "scikit-learn", "sql", "statistics"]),
        (("data analyst", "analytics"), ["sql", "excel", "power bi", "python", "data visualization", "statistics"]),
        (("devops", "cloud", "sre"), ["docker", "kubernetes", "aws", "linux", "ci/cd", "terraform", "git"]),
        (("android", "mobile"), ["kotlin", "android", "rest api", "git", "flutter"]),
        (("iot", "embedded"), ["c", "c++", "arduino", "raspberry pi", "iot", "python", "embedded systems"]),
        (("python",), ["python", "flask", "django", "sql", "git", "rest api"]),
        (("java",), ["java", "spring boot", "sql", "rest api", "microservices", "git"]),
        (("design",), ["figma", "ui/ux", "html", "css"]),
        (("market", "seo"), ["seo", "digital marketing", "content writing", "excel"]),
        (("hr", "recruit"), ["recruitment", "communication", "excel"]),
    ]
    for keys, sk in table:
        if any(k in t for k in keys):
            return sk
    return ["communication", "problem solving", "git", "sql"]


def generate_summary(name, headline, skills, years=0, projects=""):
    sk = [s.strip() for s in (skills or "").split(",") if s.strip()][:6]
    ai = _llm(f"Write a 3-sentence ATS-friendly resume summary for {name or 'a candidate'}, {headline or 'job seeker'}, "
              f"{years} years experience, skills: {', '.join(sk)}. Projects: {projects[:300]}. First person implied, no 'I'. No fluff.")
    if ai:
        return ai
    exp = f"{years}+ years of experience" if years else "strong hands-on project experience"
    return (f"{headline or 'Motivated professional'} with {exp} building reliable, user-focused solutions. "
            f"Skilled in {', '.join(sk) if sk else 'modern tools and technologies'}, with a track record of delivering "
            f"measurable results and learning quickly. Looking for a role where I can contribute from day one and keep growing.")


def improve_bullet(text):
    text = (text or "").strip().lstrip("-•* ").strip()
    if not text:
        return ""
    ai = _llm(f"Rewrite this resume bullet to start with a strong action verb, be specific and ATS-friendly, "
              f"one line, add a plausible metric placeholder like [X%] only if none exists. Bullet: {text}", max_tokens=120)
    if ai:
        return ai.lstrip("-•* ")
    irregular = {"made": "Created", "wrote": "Authored", "ran": "Executed", "did": "Completed",
                 "helped": "Supported", "worked": "Contributed", "handled": "Managed", "fixed": "Resolved"}
    words = text.split()
    first = words[0].lower()
    weak = re.match(r"^(responsible for|worked on|helped with|in charge of|involved in|did)\s*", text, flags=re.I)
    if weak:
        rest = text[weak.end():]
        out = "Delivered " + rest
    elif first in irregular:
        out = irregular[first] + " " + " ".join(words[1:])
    elif first in ACTION_VERBS or first.endswith("ed"):
        out = words[0].capitalize() + (" " + " ".join(words[1:]) if len(words) > 1 else "")
    else:
        out = "Delivered " + text[0].lower() + text[1:]
    if not re.search(r"\d", out):
        out = out.rstrip(".") + " — improving [X%] efficiency / serving [N] users."
    return out
