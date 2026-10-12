import json
import os
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

# Load configuration from the project's .env file before importing modules
# that may read environment variables. This works regardless of the terminal cwd.
BASE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    ENV_PATH = os.path.join(BASE, ".env")
    load_dotenv(ENV_PATH, override=False)
except ImportError:
    ENV_PATH = os.path.join(BASE, ".env")

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import ai_engine as ai
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024
STATUSES = ["Applied", "Shortlisted", "Interview", "Offered", "Rejected"]


# MongoDB Atlas persistence layer. SQL-shaped calls are translated to MongoDB
# collection operations so the existing Flask routes remain compatible.
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError
from bson import ObjectId

_MONGO_CLIENT = None
_MONGO_DB = None

def mongo_db():
    global _MONGO_CLIENT, _MONGO_DB
    if _MONGO_DB is None:
        uri = os.environ.get("MONGODB_URI", "").strip()
        if not uri:
            raise RuntimeError(
                "MONGODB_URI is missing. Create a file named .env in this project folder:\n"
                f"  {ENV_PATH}\n"
                "Copy the settings from .env.example, then replace MONGODB_URI with your real "
                "MongoDB Atlas connection string. Do not put the value in quotes."
            )
        _MONGO_CLIENT = MongoClient(uri, serverSelectionTimeoutMS=10000, connectTimeoutMS=10000)
        _MONGO_CLIENT.admin.command("ping")
        _MONGO_DB = _MONGO_CLIENT[os.environ.get("MONGODB_DATABASE", "placement_bbd")]
        for coll, field in [("users","email"),("applications","job_candidate"),("profiles","user_id")]:
            try:
                if coll == "applications":
                    _MONGO_DB[coll].create_index([("job_id",1),("candidate_id",1)], unique=True)
                else:
                    _MONGO_DB[coll].create_index(field, unique=True)
            except Exception:
                pass
        # Recover counter values if collections already contain records.
        for coll in ("users", "jobs", "applications"):
            highest = _MONGO_DB[coll].find_one(sort=[("_id", -1)])
            if highest and isinstance(highest.get("_id"), int):
                _MONGO_DB.counters.update_one(
                    {"_id": coll}, {"$max": {"seq": highest["_id"]}}, upsert=True)
    return _MONGO_DB

def _next_id(name):
    row = mongo_db().counters.find_one_and_update(
        {"_id": name}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    return row["seq"]

class Result:
    def __init__(self, rows=None, lastrowid=None):
        self.rows = [dict(r) for r in (rows or [])]
        self.lastrowid = lastrowid
    def fetchone(self):
        return self.rows[0] if self.rows else None
    def fetchall(self):
        return self.rows

class MongoCompat:
    def commit(self): pass
    def execute(self, sql, params=()):
        db = mongo_db()
        q = " ".join(str(sql).lower().split())
        p = list(params or ())
        def rows(coll, filt=None, sort=None, limit=None):
            cur = db[coll].find(filt or {})
            if sort: cur = cur.sort(sort)
            if limit: cur = cur.limit(limit)
            return list(cur)
        def one(coll, filt):
            return db[coll].find_one(filt)
        def withid(d):
            if not d: return None
            d = dict(d)
            d["id"] = d.get("_id")
            return d
        # Basic single collection SELECTs used by the app.
        if q.startswith("select * from users where id="):
            return Result([withid(one("users", {"_id": p[0]}))] if one("users", {"_id": p[0]}) else [])
        if q.startswith("select * from users where email="):
            d=one("users", {"email":p[0]}); return Result([withid(d)] if d else [])
        if q.startswith("select 1 from users where email="):
            d=one("users", {"email":p[0]}); return Result([{"1":1}] if d else [])
        if q.startswith("select resume_text from profiles where user_id="):
            d=one("profiles", {"user_id":p[0]}); return Result([{"resume_text":d.get("resume_text","")}] if d else [])
        if q.startswith("select resume_json from profiles where user_id="):
            d=one("profiles", {"user_id":p[0]}); return Result([{"resume_json":d.get("resume_json","")}] if d else [])
        if q.startswith("select * from profiles where user_id="):
            d=one("profiles", {"user_id":p[0]}); return Result([withid(d)] if d else [])
        if q.startswith("select * from jobs where id=") and "recruiter_id=?" in q:
            d=one("jobs", {"_id":p[0],"recruiter_id":p[1]}); return Result([withid(d)] if d else [])
        if q.startswith("select * from jobs where id="):
            filt={"_id":p[0]}
            if "status='open'" in q: filt["status"]="open"
            d=one("jobs",filt); return Result([withid(d)] if d else [])
        if q.startswith("select * from jobs where status='open'") and " and " not in q:
            return Result([withid(d) for d in rows("jobs",{"status":"open"},[("_id",-1)],6 if "limit 6" in q else None)])
        if q.startswith("select 1 from applications where job_id="):
            d=one("applications",{"job_id":p[0],"candidate_id":p[1]}); return Result([{"1":1}] if d else [])
        if q.startswith("select * from applications where job_id=? and candidate_id="):
            d=one("applications",{"job_id":p[0],"candidate_id":p[1]}); return Result([withid(d)] if d else [])
        if q.startswith("select * from applications where job_id="):
            return Result([withid(d) for d in rows("applications",{"job_id":p[0]})])
        if q.startswith("select job_id from applications where candidate_id="):
            return Result([{"job_id":d["job_id"]} for d in rows("applications",{"candidate_id":p[0]})])
        if q.startswith("select id, score from applications where job_id="):
            return Result([{"id":d["_id"],"score":d.get("score",0)} for d in rows("applications",{"job_id":p[0],"status":"Applied"},[("score",-1)])])
        if q.startswith("select status, count(*) c from applications"):
            counts={}
            for d in rows("applications",{"candidate_id":p[0]}): counts[d.get("status","Applied")]=counts.get(d.get("status","Applied"),0)+1
            return Result([{"status":k,"c":v} for k,v in counts.items()])
        if q.startswith("select count(*) c from jobs"):
            filt={"status":"open"} if "status='open'" in q else {}
            return Result([{"c":db.jobs.count_documents(filt)}])
        if q.startswith("select count(*) c from users"):
            filt={"role":"candidate"} if "role='candidate'" in q else {}
            return Result([{"c":db.users.count_documents(filt)}])
        if q.startswith("select count(*) c from applications"):
            return Result([{"c":db.applications.count_documents({})}])
        # Search jobs query assembled by /jobs.
        if q.startswith("select * from jobs where status='open' and"):
            filt={"status":"open"}; i=0
            if "title like ?" in q:
                vals=p[i:i+4]; i+=4
                term=vals[0].strip("%")
                filt["$or"]=[{"title":{"$regex":re.escape(term),"$options":"i"}},{"skills":{"$regex":re.escape(term),"$options":"i"}},{"company":{"$regex":re.escape(term),"$options":"i"}},{"description":{"$regex":re.escape(term),"$options":"i"}}]
            if "location like ?" in q:
                filt["location"]={"$regex":re.escape(p[i].strip("%")),"$options":"i"}; i+=1
            if "job_type=?" in q: filt["job_type"]=p[i]
            return Result([withid(d) for d in rows("jobs",filt,[("_id",-1)])])
        # JOINs are composed in Python to preserve the current templates' row shape.
        if "join jobs j on j.id=a.job_id" in q and "join users u on u.id=a.candidate_id" in q:
            appid=p[0]; a=one("applications",{"_id":appid})
            if not a: return Result()
            j=one("jobs",{"_id":a["job_id"]}); u=one("users",{"_id":a["candidate_id"]})
            if not j or not u: return Result()
            merged={**withid(a),"name":u.get("name"),"email":u.get("email"),"title":j.get("title"),"company":j.get("company"),"recruiter_id":j.get("recruiter_id")}
            return Result([merged])
        if "join jobs j on j.id=a.job_id" in q and "where candidate_id=?" in q:
            cand=p[0]; limit=5 if "limit 5" in q else None; out=[]
            for a in rows("applications",{"candidate_id":cand},[("_id",-1)],limit):
                j=one("jobs",{"_id":a["job_id"]})
                if j: out.append({**withid(a),"title":j.get("title"),"company":j.get("company"),"location":j.get("location")})
            return Result(out)
        if q.startswith("select j.*, count(a.id)"):
            out=[]
            for j in rows("jobs",{"recruiter_id":p[0]},[("_id",-1)]):
                apps=rows("applications",{"job_id":j["_id"]})
                out.append({**withid(j),"apps":len(apps),"short":sum(1 for a in apps if a.get("status")=="Shortlisted"),"best":max([a.get("score",0) for a in apps] or [0])})
            return Result(out)
        if q.startswith("select a.id, a.job_id from applications"):
            a=one("applications",{"_id":p[0]})
            j=one("jobs",{"_id":a["job_id"]}) if a else None
            if a and j and j.get("recruiter_id")==p[1]: return Result([{"id":a["_id"],"job_id":a["job_id"]}])
            return Result()
        if q.startswith("select a.*, u.name, u.email from applications"):
            jobid,minscore=p[0],p[1]; filt={"job_id":jobid,"score":{"$gte":minscore}}
            if "and a.status=?" in q: filt["status"]=p[2]
            out=[]
            for a in rows("applications",filt,[("score",-1),("_id",1)]):
                u=one("users",{"_id":a["candidate_id"]})
                if u: out.append({**withid(a),"name":u.get("name"),"email":u.get("email")})
            return Result(out)
        # INSERT operations
        if q.startswith("insert into users"):
            keys=re.search(r"\((.*?)\) values",q).group(1).replace(" ","").split(",")
            d=dict(zip(keys,p)); d["_id"]=_next_id("users"); d.setdefault("company",""); d.setdefault("created",datetime.utcnow().isoformat())
            db.users.insert_one(d); return Result(lastrowid=d["_id"])
        if q.startswith("insert into profiles"):
            uid=p[0] if p else None
            db.profiles.update_one({"user_id":uid},{"$setOnInsert":{"user_id":uid,"resume_json":"","resume_text":"","updated":datetime.utcnow().isoformat()}},upsert=True)
            return Result()
        if q.startswith("insert into jobs"):
            keys=re.search(r"\((.*?)\) values",q,re.S).group(1).replace("\n","").replace(" ","").split(",")
            d=dict(zip(keys,p)); d["_id"]=_next_id("jobs"); d.setdefault("status","open"); d.setdefault("created",datetime.utcnow().isoformat())
            db.jobs.insert_one(d); return Result(lastrowid=d["_id"])
        if q.startswith("insert into applications"):
            keys=re.search(r"\((.*?)\) values",q,re.S).group(1).replace("\n","").replace(" ","").split(",")
            d=dict(zip(keys,p)); d["_id"]=_next_id("applications"); d.setdefault("created",datetime.utcnow().isoformat())
            db.applications.insert_one(d); return Result(lastrowid=d["_id"])
        # UPDATE / DELETE
        if q.startswith("update profiles set"):
            db.profiles.update_one({"user_id":p[3]},{"$set":{"resume_json":p[0],"resume_text":p[1],"updated":p[2]}})
            return Result()
        if q.startswith("update jobs set title="):
            keys=["title","company","location","job_type","level","salary_min","salary_max","skills","description","auto_shortlist","threshold"]
            db.jobs.update_one({"_id":p[-1]},{"$set":dict(zip(keys,p[:-1]))}); return Result()
        if q.startswith("update jobs set status="):
            db.jobs.update_one({"_id":p[1]},{"$set":{"status":p[0]}}); return Result()
        if q.startswith("delete from jobs where id="):
            jid=p[0]; db.jobs.delete_one({"_id":jid}); db.applications.delete_many({"job_id":jid}); return Result()
        if q.startswith("update applications set score="):
            keys=["score","skill_score","sim_score","matched","missing","reason"]
            db.applications.update_one({"_id":p[-1]},{"$set":dict(zip(keys,p[:-1]))}); return Result()
        if q.startswith("update applications set status='shortlisted'"):
            db.applications.update_one({"_id":p[0]},{"$set":{"status":"Shortlisted"}}); return Result()
        if q.startswith("update applications set status="):
            db.applications.update_one({"_id":p[1]},{"$set":{"status":p[0]}}); return Result()
        raise NotImplementedError("MongoDB adapter does not recognize query: "+q[:160])
    def executemany(self, sql, seq):
        for p in seq: self.execute(sql,p)


def db():
    # MongoDB is stateless per request; a tiny adapter preserves route-level calls.
    if "db" not in g:
        g.db = MongoCompat()
    return g.db

@app.teardown_appcontext
def close_db(_):
    g.pop("db", None)

def seed_mongo():
    database = mongo_db()
    if database.users.count_documents({}) == 0:
        database.users.insert_one({"_id":1,"name":"Demo Recruiter","email":"recruiter@demo.com",
            "password":generate_password_hash("demo1234"),"role":"recruiter","company":"Nimbus Labs",
            "created":datetime.utcnow().isoformat()})
        database.users.insert_one({"_id":2,"name":"Demo Candidate","email":"candidate@demo.com",
            "password":generate_password_hash("demo1234"),"role":"candidate","company":"",
            "created":datetime.utcnow().isoformat()})
        database.counters.update_one({"_id":"users"},{"$max":{"seq":2}},upsert=True)
        demo = [
          ("Full Stack Developer","Nimbus Labs","Lucknow, UP","Full-time","Mid",600000,1200000,"python, flask, javascript, react, sql, rest api, git","We need a full stack developer with 2+ years of experience building Flask and React applications, designing REST APIs and working with SQL databases. Git workflow and clean code are a must."),
          ("Data Analyst","Nimbus Labs","Remote","Full-time","Junior",400000,800000,"sql, excel, python, power bi, data visualization, statistics","Analyse business data, build dashboards in Power BI, write SQL queries and present insights. Python and statistics knowledge preferred."),
          ("IoT Engineer","Nimbus Labs","Noida, UP","Full-time","Mid",500000,1000000,"c, c++, arduino, raspberry pi, iot, python, embedded systems","Design IoT systems using Arduino and Raspberry Pi, integrate sensors over UART/SPI/I2C and push data to the cloud."),
          ("Machine Learning Intern","Nimbus Labs","Remote","Internship","Fresher",120000,240000,"python, machine learning, pandas, numpy, scikit-learn","Work with our data team on model training, feature engineering and evaluation using the Python ML stack.")
        ]
        for i,d in enumerate(demo,1):
            database.jobs.insert_one({"_id":i,"recruiter_id":1,"title":d[0],"company":d[1],"location":d[2],
              "job_type":d[3],"level":d[4],"salary_min":d[5],"salary_max":d[6],"skills":d[7],
              "description":d[8],"status":"open","auto_shortlist":1,"threshold":70,"created":datetime.utcnow().isoformat()})
        database.counters.update_one({"_id":"jobs"},{"$max":{"seq":len(demo)}},upsert=True)

# ----------------------------------------------------------------- helpers
@app.before_request
def ensure_mongo_seed():
    # Atlas ping and seed are performed lazily on first request.
    if not getattr(app, "_mongo_initialized", False):
        seed_mongo()
        app._mongo_initialized = True

@app.before_request
def load_user():
    g.user = None
    if "uid" in session:
        g.user = db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    if request.method == "POST":
        token = request.form.get("_csrf") or request.headers.get("X-CSRF-Token")
        if not token or token != session.get("csrf"):
            abort(400, "Invalid or missing CSRF token. Refresh the page and try again.")


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"user": g.get("user"), "csrf_token": session["csrf"], "STATUSES": STATUSES,
            "llm": ai.llm_available()}


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrap(*a, **kw):
            if not g.user:
                flash("Please sign in to continue.", "warn")
                return redirect(url_for("login", next=request.path))
            if role and g.user["role"] != role:
                flash(f"That page is for {role}s.", "warn")
                return redirect(url_for("home"))
            return fn(*a, **kw)
        return wrap
    return deco


def get_resume_text(uid):
    p = db().execute("SELECT resume_text FROM profiles WHERE user_id=?", (uid,)).fetchone()
    return (p["resume_text"] if p else "") or ""


def get_resume_json(uid):
    p = db().execute("SELECT resume_json FROM profiles WHERE user_id=?", (uid,)).fetchone()
    try:
        return json.loads(p["resume_json"]) if p and p["resume_json"] else {}
    except ValueError:
        return {}


def save_profile(uid, rjson=None, rtext=None):
    cur = db().execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()
    if not cur:
        db().execute("INSERT INTO profiles(user_id) VALUES(?)", (uid,))
        cur = db().execute("SELECT * FROM profiles WHERE user_id=?", (uid,)).fetchone()
    j = json.dumps(rjson) if rjson is not None else cur["resume_json"]
    t = rtext if rtext is not None else cur["resume_text"]
    db().execute("UPDATE profiles SET resume_json=?, resume_text=?, updated=? WHERE user_id=?",
                 (j, t, datetime.utcnow().isoformat(), uid))
    db().commit()


def clean_skills(s):
    seen, out = set(), []
    for x in (s or "").replace("\n", ",").split(","):
        x = x.strip().lower()
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return ", ".join(out)


def own_job(job_id):
    j = db().execute("SELECT * FROM jobs WHERE id=? AND recruiter_id=?", (job_id, g.user["id"])).fetchone()
    if not j:
        abort(404)
    return j


# --------------------------------------------------------------------- auth
@app.route("/")
def home():
    jobs = db().execute("SELECT * FROM jobs WHERE status='open' ORDER BY id DESC LIMIT 6").fetchall()
    stats = {
        "jobs": db().execute("SELECT COUNT(*) c FROM jobs WHERE status='open'").fetchone()["c"],
        "cands": db().execute("SELECT COUNT(*) c FROM users WHERE role='candidate'").fetchone()["c"],
        "apps": db().execute("SELECT COUNT(*) c FROM applications").fetchone()["c"],
    }
    return render_template("index.html", jobs=jobs, stats=stats)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        name, email, pw, role = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("password", ""), f.get("role", "candidate")
        if not (name and email and len(pw) >= 6) or role not in ("candidate", "recruiter"):
            flash("Fill every field. Password needs at least 6 characters.", "err")
        elif db().execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            flash("That email is already registered. Try signing in.", "err")
        else:
            cur = db().execute("INSERT INTO users(name,email,password,role,company) VALUES(?,?,?,?,?)",
                               (name, email, generate_password_hash(pw), role, f.get("company", "").strip()))
            db().commit()
            session.clear()
            session["uid"] = cur.lastrowid
            flash("Welcome aboard!", "ok")
            return redirect(url_for("recruiter_dashboard" if role == "recruiter" else "candidate_dashboard"))
    return render_template("register.html", role=request.args.get("role", "candidate"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),)).fetchone()
        if u and check_password_hash(u["password"], request.form.get("password", "")):
            session.clear()
            session["uid"] = u["id"]
            nxt = request.args.get("next")
            if nxt and nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("recruiter_dashboard" if u["role"] == "recruiter" else "candidate_dashboard"))
        flash("Email or password is incorrect.", "err")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


# --------------------------------------------------------------- public jobs
@app.route("/jobs")
def jobs():
    q = request.args.get("q", "").strip()
    loc = request.args.get("loc", "").strip()
    jt = request.args.get("type", "").strip()
    sql, args = "SELECT * FROM jobs WHERE status='open'", []
    if q:
        sql += " AND (title LIKE ? OR skills LIKE ? OR company LIKE ? OR description LIKE ?)"
        args += [f"%{q}%"] * 4
    if loc:
        sql += " AND location LIKE ?"
        args.append(f"%{loc}%")
    if jt:
        sql += " AND job_type=?"
        args.append(jt)
    rows = db().execute(sql + " ORDER BY id DESC", args).fetchall()
    return render_template("jobs.html", jobs=rows, q=q, loc=loc, jt=jt)


@app.route("/jobs/<int:job_id>")
def job_detail(job_id):
    j = db().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        abort(404)
    match, applied = None, None
    if g.user and g.user["role"] == "candidate":
        rt = get_resume_text(g.user["id"])
        if rt:
            match = ai.match_resume_to_job(rt, j["title"], j["description"], j["skills"])
        applied = db().execute("SELECT * FROM applications WHERE job_id=? AND candidate_id=?",
                               (job_id, g.user["id"])).fetchone()
    return render_template("job_detail.html", j=j, match=match, applied=applied)


@app.route("/jobs/<int:job_id>/apply", methods=["POST"])
@login_required("candidate")
def apply(job_id):
    j = db().execute("SELECT * FROM jobs WHERE id=? AND status='open'", (job_id,)).fetchone()
    if not j:
        abort(404)
    rt = get_resume_text(g.user["id"])
    if len(rt.split()) < 40:
        flash("Build or upload your resume first — it is what recruiters will score.", "warn")
        return redirect(url_for("resume_builder"))
    if db().execute("SELECT 1 FROM applications WHERE job_id=? AND candidate_id=?", (job_id, g.user["id"])).fetchone():
        flash("You have already applied to this job.", "warn")
        return redirect(url_for("job_detail", job_id=job_id))
    m = ai.match_resume_to_job(rt, j["title"], j["description"], j["skills"])
    status = "Shortlisted" if j["auto_shortlist"] and m["score"] >= j["threshold"] else "Applied"
    db().execute("""INSERT INTO applications(job_id,candidate_id,resume_text,cover,score,skill_score,sim_score,matched,missing,reason,status)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                 (job_id, g.user["id"], rt, request.form.get("cover", "")[:2000], m["score"], m["skill_score"],
                  m["sim_score"], ", ".join(m["matched"]), ", ".join(m["missing"]), m["reason"], status))
    db().commit()
    flash(f"Application sent. Your resume matched this role at {m['score']}%.", "ok")
    return redirect(url_for("my_applications"))


# --------------------------------------------------------------- candidate
@app.route("/candidate")
@login_required("candidate")
def candidate_dashboard():
    rt = get_resume_text(g.user["id"])
    apps = db().execute("""SELECT a.*, j.title, j.company FROM applications a JOIN jobs j ON j.id=a.job_id
                           WHERE candidate_id=? ORDER BY a.id DESC LIMIT 5""", (g.user["id"],)).fetchall()
    a = ai.ats_score(rt) if rt else None
    open_jobs = db().execute("SELECT * FROM jobs WHERE status='open'").fetchall()
    recs = ai.recommend_jobs(rt, open_jobs, 3) if rt else []
    counts = db().execute("SELECT status, COUNT(*) c FROM applications WHERE candidate_id=? GROUP BY status",
                          (g.user["id"],)).fetchall()
    return render_template("candidate_dashboard.html", ats=a, recs=recs, apps=apps, has_resume=bool(rt),
                           counts={r["status"]: r["c"] for r in counts})


@app.route("/candidate/applications")
@login_required("candidate")
def my_applications():
    rows = db().execute("""SELECT a.*, j.title, j.company, j.location FROM applications a JOIN jobs j ON j.id=a.job_id
                           WHERE candidate_id=? ORDER BY a.id DESC""", (g.user["id"],)).fetchall()
    return render_template("my_applications.html", apps=rows)


@app.route("/candidate/resume-builder")
@login_required("candidate")
def resume_builder():
    r = get_resume_json(g.user["id"])
    if not r:
        r = {"name": g.user["name"], "email": g.user["email"], "experience": [{}], "education": [{}], "projects": [{}]}
    return render_template("resume_builder.html", resume=r)


@app.route("/candidate/resume/save", methods=["POST"])
@login_required("candidate")
def resume_save():
    data = request.get_json(silent=True) or {}
    save_profile(g.user["id"], rjson=data, rtext=ai.resume_json_to_text(data))
    return jsonify(ok=True, ats=ai.ats_score(ai.resume_json_to_text(data)))


@app.route("/candidate/resume/print")
@login_required("candidate")
def resume_print():
    return render_template("resume_print.html", r=get_resume_json(g.user["id"]),
                           tpl=request.args.get("t", "modern"))


@app.route("/candidate/resume/upload", methods=["POST"])
@login_required("candidate")
def resume_upload():
    f = request.files.get("resume")
    if not f or not f.filename:
        flash("Choose a PDF, DOCX or TXT file.", "err")
        return redirect(request.referrer or url_for("candidate_ats"))
    if not f.filename.lower().endswith((".pdf", ".docx", ".txt")):
        flash("Only PDF, DOCX or TXT files are supported.", "err")
        return redirect(request.referrer or url_for("candidate_ats"))
    text = ai.parse_resume_file(f.filename, f.read())
    if len(text.split()) < 30:
        flash("Couldn't read enough text from that file. Scanned images aren't supported — try a text PDF or DOCX.", "err")
    else:
        save_profile(g.user["id"], rtext=text)
        flash("Resume uploaded and ready for scoring, recommendations and applications.", "ok")
    return redirect(url_for("candidate_ats"))


@app.route("/candidate/ats", methods=["GET", "POST"])
@login_required("candidate")
def candidate_ats():
    rt = get_resume_text(g.user["id"])
    jd = request.form.get("jd", "") if request.method == "POST" else ""
    res = ai.ats_score(rt, jd) if rt else None
    return render_template("ats.html", res=res, jd=jd, has_resume=bool(rt), words=len(rt.split()))


@app.route("/candidate/recommendations")
@login_required("candidate")
def recommendations():
    rt = get_resume_text(g.user["id"])
    open_jobs = db().execute("SELECT * FROM jobs WHERE status='open'").fetchall()
    recs = ai.recommend_jobs(rt, open_jobs, 20) if rt else []
    skills = ai.extract_skills(rt) if rt else []
    applied = {r["job_id"] for r in db().execute("SELECT job_id FROM applications WHERE candidate_id=?", (g.user["id"],))}
    return render_template("recommendations.html", recs=recs, skills=skills, has_resume=bool(rt), applied=applied)


# --------------------------------------------------------------- recruiter
@app.route("/recruiter")
@login_required("recruiter")
def recruiter_dashboard():
    rows = db().execute("""SELECT j.*, COUNT(a.id) apps,
                           SUM(CASE WHEN a.status='Shortlisted' THEN 1 ELSE 0 END) short,
                           COALESCE(MAX(a.score),0) best
                           FROM jobs j LEFT JOIN applications a ON a.job_id=j.id
                           WHERE j.recruiter_id=? GROUP BY j.id ORDER BY j.id DESC""", (g.user["id"],)).fetchall()
    tot = {"jobs": len(rows), "apps": sum(r["apps"] for r in rows), "short": sum(r["short"] or 0 for r in rows)}
    return render_template("recruiter_dashboard.html", jobs=rows, tot=tot)


def _job_form_values():
    f = request.form
    return dict(title=f.get("title", "").strip(), company=f.get("company", "").strip(),
                location=f.get("location", "").strip(), job_type=f.get("job_type", "Full-time"),
                level=f.get("level", ""), salary_min=int(f.get("salary_min") or 0),
                salary_max=int(f.get("salary_max") or 0), skills=clean_skills(f.get("skills", "")),
                description=f.get("description", "").strip(), auto_shortlist=1 if f.get("auto_shortlist") else 0,
                threshold=max(10, min(95, int(f.get("threshold") or 70))))


@app.route("/recruiter/jobs/create", methods=["GET", "POST"])
@login_required("recruiter")
def job_create():
    if request.method == "POST":
        v = _job_form_values()
        if not (v["title"] and v["company"] and len(v["description"]) >= 30):
            flash("Title, company and a description (30+ characters) are required.", "err")
            return render_template("job_form.html", j=v, edit=False)
        if not v["skills"]:
            v["skills"] = ", ".join(ai.extract_skills(v["description"]))
        cur = db().execute("""INSERT INTO jobs(recruiter_id,title,company,location,job_type,level,salary_min,salary_max,
                              skills,description,auto_shortlist,threshold) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (g.user["id"], v["title"], v["company"], v["location"], v["job_type"], v["level"],
                            v["salary_min"], v["salary_max"], v["skills"], v["description"], v["auto_shortlist"], v["threshold"]))
        db().commit()
        flash("Job posted. Candidates will now be scored against it automatically.", "ok")
        return redirect(url_for("job_applicants", job_id=cur.lastrowid))
    return render_template("job_form.html", j={"company": g.user["company"], "auto_shortlist": 1, "threshold": 70,
                                               "job_type": "Full-time"}, edit=False)


@app.route("/recruiter/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@login_required("recruiter")
def job_edit(job_id):
    j = own_job(job_id)
    if request.method == "POST":
        v = _job_form_values()
        db().execute("""UPDATE jobs SET title=?,company=?,location=?,job_type=?,level=?,salary_min=?,salary_max=?,
                        skills=?,description=?,auto_shortlist=?,threshold=? WHERE id=?""",
                     (v["title"], v["company"], v["location"], v["job_type"], v["level"], v["salary_min"], v["salary_max"],
                      v["skills"], v["description"], v["auto_shortlist"], v["threshold"], job_id))
        db().commit()
        flash("Job updated.", "ok")
        return redirect(url_for("recruiter_dashboard"))
    return render_template("job_form.html", j=j, edit=True)


@app.route("/recruiter/jobs/<int:job_id>/toggle", methods=["POST"])
@login_required("recruiter")
def job_toggle(job_id):
    j = own_job(job_id)
    db().execute("UPDATE jobs SET status=? WHERE id=?", ("closed" if j["status"] == "open" else "open", job_id))
    db().commit()
    return redirect(url_for("recruiter_dashboard"))


@app.route("/recruiter/jobs/<int:job_id>/delete", methods=["POST"])
@login_required("recruiter")
def job_delete(job_id):
    own_job(job_id)
    db().execute("DELETE FROM jobs WHERE id=?", (job_id,))
    db().commit()
    flash("Job deleted.", "ok")
    return redirect(url_for("recruiter_dashboard"))


@app.route("/recruiter/jobs/<int:job_id>/applicants")
@login_required("recruiter")
def job_applicants(job_id):
    j = own_job(job_id)
    status = request.args.get("status", "")
    minscore = int(request.args.get("min") or 0)
    sql = """SELECT a.*, u.name, u.email FROM applications a JOIN users u ON u.id=a.candidate_id
             WHERE a.job_id=? AND a.score>=?"""
    args = [job_id, minscore]
    if status in STATUSES:
        sql += " AND a.status=?"
        args.append(status)
    rows = db().execute(sql + " ORDER BY a.score DESC, a.id", args).fetchall()
    return render_template("applicants.html", j=j, apps=rows, status=status, minscore=minscore)


@app.route("/recruiter/jobs/<int:job_id>/rescore", methods=["POST"])
@login_required("recruiter")
def job_rescore(job_id):
    """Re-run AI matching for all applicants (e.g. after editing the JD)."""
    j = own_job(job_id)
    for a in db().execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchall():
        m = ai.match_resume_to_job(a["resume_text"], j["title"], j["description"], j["skills"])
        db().execute("UPDATE applications SET score=?,skill_score=?,sim_score=?,matched=?,missing=?,reason=? WHERE id=?",
                     (m["score"], m["skill_score"], m["sim_score"], ", ".join(m["matched"]),
                      ", ".join(m["missing"]), m["reason"], a["id"]))
    db().commit()
    flash("All applicants re-scored against the current job description.", "ok")
    return redirect(url_for("job_applicants", job_id=job_id))


@app.route("/recruiter/jobs/<int:job_id>/shortlist", methods=["POST"])
@login_required("recruiter")
def job_shortlist(job_id):
    """AI shortlisting: by score threshold or top-N, based on JD vs resume match."""
    own_job(job_id)
    mode = request.form.get("mode", "threshold")
    pending = db().execute("SELECT id, score FROM applications WHERE job_id=? AND status='Applied' ORDER BY score DESC",
                           (job_id,)).fetchall()
    if mode == "top":
        n = max(1, int(request.form.get("n") or 5))
        ids = [r["id"] for r in pending[:n]]
    else:
        t = max(0, min(100, int(request.form.get("threshold") or 70)))
        ids = [r["id"] for r in pending if r["score"] >= t]
    for i in ids:
        db().execute("UPDATE applications SET status='Shortlisted' WHERE id=?", (i,))
    db().commit()
    flash(f"{len(ids)} candidate{'s' if len(ids) != 1 else ''} shortlisted by AI match score.", "ok")
    return redirect(url_for("job_applicants", job_id=job_id))


@app.route("/recruiter/applications/<int:app_id>/status", methods=["POST"])
@login_required("recruiter")
def app_status(app_id):
    a = db().execute("""SELECT a.id, a.job_id FROM applications a JOIN jobs j ON j.id=a.job_id
                        WHERE a.id=? AND j.recruiter_id=?""", (app_id, g.user["id"])).fetchone()
    if not a:
        abort(404)
    s = request.form.get("status")
    if s in STATUSES:
        db().execute("UPDATE applications SET status=? WHERE id=?", (s, app_id))
        db().commit()
    return redirect(url_for("job_applicants", job_id=a["job_id"]))


@app.route("/recruiter/applications/<int:app_id>")
@login_required("recruiter")
def application_view(app_id):
    a = db().execute("""SELECT a.*, u.name, u.email, j.title, j.company, j.recruiter_id FROM applications a
                        JOIN jobs j ON j.id=a.job_id JOIN users u ON u.id=a.candidate_id WHERE a.id=?""", (app_id,)).fetchone()
    if not a or a["recruiter_id"] != g.user["id"]:
        abort(404)
    return render_template("application_view.html", a=a)


# ---------------------------------------------------------------- AI JSON API
@app.route("/api/generate-jd", methods=["POST"])
@login_required("recruiter")
def api_generate_jd():
    d = request.get_json(silent=True) or {}
    if not d.get("title"):
        return jsonify(error="Enter a job title first."), 400
    return jsonify(ai.generate_jd(d.get("title", ""), d.get("company", ""), d.get("level", ""),
                                  d.get("skills", ""), d.get("location", "")))


@app.route("/api/analyze-jd", methods=["POST"])
@login_required("recruiter")
def api_analyze_jd():
    d = request.get_json(silent=True) or {}
    text = d.get("description", "")
    return jsonify(skills=ai.extract_skills(text), min_years=ai.jd_min_years(text))


@app.route("/api/preview-match", methods=["POST"])
@login_required("recruiter")
def api_preview_match():
    """Recruiter pastes a sample resume to test how the JD will score it."""
    d = request.get_json(silent=True) or {}
    if len((d.get("resume") or "").split()) < 10:
        return jsonify(error="Paste a few lines of a resume."), 400
    return jsonify(ai.match_resume_to_job(d["resume"], d.get("title", ""), d.get("description", ""), d.get("skills", "")))


@app.route("/api/ats", methods=["POST"])
@login_required("candidate")
def api_ats():
    d = request.get_json(silent=True) or {}
    return jsonify(ai.ats_score(ai.resume_json_to_text(d), d.get("jd", "")))


@app.route("/api/resume/summary", methods=["POST"])
@login_required("candidate")
def api_summary():
    d = request.get_json(silent=True) or {}
    return jsonify(summary=ai.generate_summary(d.get("name"), d.get("headline"), d.get("skills"),
                                               int(d.get("years") or 0), d.get("projects", "")))


@app.route("/api/resume/improve", methods=["POST"])
@login_required("candidate")
def api_improve():
    d = request.get_json(silent=True) or {}
    lines = [ai.improve_bullet(x) for x in (d.get("text") or "").split("\n") if x.strip()]
    return jsonify(text="\n".join(lines))


@app.errorhandler(404)
def nf(_):
    return render_template("error.html", code=404, msg="That page doesn't exist or isn't yours to open."), 404


@app.errorhandler(400)
def bad(e):
    return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400


@app.errorhandler(413)
def big(_):
    return render_template("error.html", code=413, msg="File is larger than 4 MB."), 413


if __name__ == "__main__":
    # MongoDB is initialized and demo data seeded lazily on the first request.
    # This avoids a duplicate connection/seed attempt during startup.
    app.run(debug=os.environ.get("FLASK_DEBUG", "0") == "1", port=int(os.environ.get("PORT", 5000)))
