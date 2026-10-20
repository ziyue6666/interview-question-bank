"""Local interview practice server. No practice draft is ever persisted or logged."""
from __future__ import annotations
import json
import os
import random
import re
import secrets
import sqlite3
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from io import BytesIO
import keyring
from platformdirs import user_data_dir
from fastapi import UploadFile, File
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import parser as source_parser

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("INTERVIEW_DATA_DIR") or user_data_dir("InterviewQuestionBankOpen", "InterviewQuestionBankOpen"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "interview.sqlite3"
TZ = ZoneInfo("Asia/Shanghai")
MODEL = "deepseek-flash"
LOCK = threading.RLock()
app = FastAPI(title="面试题库助手开源版", docs_url=None, redoc_url=None)



def now():
    return datetime.now(TZ)


def today():
    return now().date().isoformat()


def uid():
    return secrets.token_hex(12)


def db():
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def rows(c, query, params=()):
    return [dict(x) for x in c.execute(query, params).fetchall()]


def one(c, query, params=()):
    r = c.execute(query, params).fetchone()
    return dict(r) if r else None


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY,bank TEXT NOT NULL,source_key TEXT UNIQUE,company TEXT,round TEXT,section TEXT,topic TEXT,interview_type TEXT,status TEXT DEFAULT 'active',created_day TEXT,generated_day TEXT,reference_ids TEXT DEFAULT '[]',difficulty_reason TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS questions(id TEXT PRIMARY KEY,group_id TEXT NOT NULL,position INTEGER NOT NULL,text TEXT NOT NULL,provenance TEXT,source_key TEXT UNIQUE,source_ref TEXT,status TEXT DEFAULT 'active',fingerprint TEXT,FOREIGN KEY(group_id) REFERENCES groups(id));
        CREATE TABLE IF NOT EXISTS answers(question_id TEXT PRIMARY KEY,primary_text TEXT DEFAULT '',primary_origin TEXT DEFAULT '',source_ref TEXT DEFAULT '',ai_supplement TEXT DEFAULT '',generated_at TEXT,FOREIGN KEY(question_id) REFERENCES questions(id));
        CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,group_id TEXT,question_id TEXT,state TEXT DEFAULT 'active',revealed_at TEXT,created_at TEXT,skip_streak INTEGER DEFAULT 0,next_id TEXT,draw_kind TEXT);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,delivery_id TEXT UNIQUE,question_id TEXT,group_id TEXT,bank TEXT,action TEXT,grade TEXT,occurred_at TEXT,local_day TEXT,question_text TEXT);
        CREATE TABLE IF NOT EXISTS schedules(question_id TEXT PRIMARY KEY,grade TEXT,due_on TEXT,reviewed_at TEXT);
        CREATE TABLE IF NOT EXISTS draws(id TEXT PRIMARY KEY,group_id TEXT,bank TEXT,topic TEXT,draw_kind TEXT,day TEXT,created_at TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS import_jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,created_at TEXT,committed_at TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,job_id TEXT,source_key TEXT,action TEXT,confidence REAL,selected INTEGER,payload TEXT,note TEXT);
        CREATE TABLE IF NOT EXISTS answer_messages(id TEXT PRIMARY KEY,question_id TEXT NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,proposed_answer TEXT DEFAULT '',created_at TEXT NOT NULL,FOREIGN KEY(question_id) REFERENCES questions(id));
        CREATE TABLE IF NOT EXISTS resume_profiles(version INTEGER PRIMARY KEY, facts TEXT NOT NULL, confirmed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS answer_versions(id TEXT PRIMARY KEY,question_id TEXT NOT NULL,previous_text TEXT NOT NULL,saved_at TEXT NOT NULL,FOREIGN KEY(question_id) REFERENCES questions(id));
        CREATE INDEX IF NOT EXISTS idx_questions_group ON questions(group_id,position);
        CREATE INDEX IF NOT EXISTS idx_events_day ON events(local_day);
        CREATE INDEX IF NOT EXISTS idx_answer_messages_question ON answer_messages(question_id,created_at);
        """)
        group_columns={r["name"] for r in c.execute("PRAGMA table_info(groups)")}
        if "resume_version" not in group_columns:
            c.execute("ALTER TABLE groups ADD COLUMN resume_version INTEGER NOT NULL DEFAULT 0")
        answer_columns={r["name"] for r in c.execute("PRAGMA table_info(answers)")}
        if "resume_version" not in answer_columns:
            c.execute("ALTER TABLE answers ADD COLUMN resume_version INTEGER NOT NULL DEFAULT 0")
        delivery_columns={r["name"] for r in c.execute("PRAGMA table_info(deliveries)")}
        if "practice_mode" not in delivery_columns:
            c.execute("ALTER TABLE deliveries ADD COLUMN practice_mode TEXT NOT NULL DEFAULT 'technical'")
        if "focus" not in delivery_columns:
            c.execute("ALTER TABLE deliveries ADD COLUMN focus TEXT NOT NULL DEFAULT 'all'")
        job_columns={r["name"] for r in c.execute("PRAGMA table_info(import_jobs)")}
        if "source_text" not in job_columns:
            c.execute("ALTER TABLE import_jobs ADD COLUMN source_text TEXT NOT NULL DEFAULT ''")
        c.execute("INSERT OR IGNORE INTO settings VALUES('weights','{\"agent\":55,\"java\":35,\"other\":10}')")

init_db()


def fail(code, message, status=400, retryable=False):
    raise HTTPException(status, detail={"code": code, "message": message, "retryable": retryable})


@app.exception_handler(HTTPException)
async def errors(_, exc):
    return JSONResponse(status_code=exc.status_code, content=exc.detail if isinstance(exc.detail, dict) else {"code":"ERROR","message":str(exc.detail),"retryable":False})


@app.middleware("http")
async def local_only(request: Request, call_next):
    host=request.headers.get("host", "")
    if host.split(":")[0] not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse(status_code=403,content={"code":"HOST","message":"仅允许本机访问"})
    origin=request.headers.get("origin")
    if origin and origin.rstrip("/") not in ("http://"+host, "http://127.0.0.1:"+host.split(":")[-1], "http://localhost:"+host.split(":")[-1]):
        return JSONResponse(status_code=403,content={"code":"ORIGIN","message":"仅允许当前题库页面操作"})
    path=request.url.path
    public=(path in ("/api/health","/api/setup/status","/api/setup/key") or path.startswith("/static/") or path=="/")
    if path.startswith("/api/") and not public and not key_get():
        return JSONResponse(status_code=403,content={"code":"SETUP_REQUIRED","message":"请先验证并配置本人的 DeepSeek API Key"})
    return await call_next(request)


def key_get():
    try:
        return keyring.get_password("InterviewQuestionBankOpen.DeepSeek", "current-user") or ""
    except Exception:
        return ""


def key_set(value):
    try:
        keyring.set_password("InterviewQuestionBankOpen.DeepSeek", "current-user", value)
    except Exception:
        fail("KEYCHAIN_ERROR", "无法写入系统凭据存储")


def key_delete():
    try:
        keyring.delete_password("InterviewQuestionBankOpen.DeepSeek", "current-user")
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception:
        fail("KEYCHAIN_ERROR", "无法移除系统凭据")


def resume_current(c):
    return one(c,"SELECT version,facts,confirmed_at FROM resume_profiles ORDER BY version DESC LIMIT 1")


def facts_text(c):
    profile=resume_current(c)
    return profile["facts"] if profile else "没有已确认的简历事实。不要推断个人经历、指标或项目。"


def resume_projects(c):
    profile=resume_current(c)
    if not profile: return []
    try:
        return [x for x in json.loads(profile["facts"]).get("projects",[]) if isinstance(x,dict) and x.get("name")]
    except (ValueError,TypeError):
        return []


def verify_key(value):
    body=json.dumps({"model":MODEL,"messages":[{"role":"user","content":"回复 OK"}],"max_tokens":8,"thinking":{"type":"disabled"}}).encode()
    req=urllib.request.Request("https://api.deepseek.com/chat/completions",data=body,headers={"Authorization":"Bearer "+value,"Content-Type":"application/json"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=20) as res: return res.status==200
    except urllib.error.HTTPError as exc:
        if exc.code in (401,403): fail("KEY_INVALID","密钥验证失败，请检查 DeepSeek API Key",400)
        fail("KEY_VERIFY_FAILED","DeepSeek 暂时无法验证密钥，请稍后重试",503,True)
    except urllib.error.URLError:
        fail("KEY_VERIFY_FAILED","无法连接 DeepSeek，请联网后重试",503,True)


@app.get("/api/setup/status")
def setup_status():
    with db() as c:
        profile=resume_current(c)
        return {"configured":bool(key_get()),"resume_confirmed":bool(profile),"resume_version":profile["version"] if profile else 0,"resume_skipped":bool(one(c,"SELECT value FROM settings WHERE key='resume_skipped'"))}


class KeyInput(BaseModel):
    key: str=Field(min_length=8,max_length=256)


@app.put("/api/setup/key")
def setup_key(data: KeyInput):
    value=data.key.strip()
    verify_key(value)
    key_set(value)
    return {"configured":True}

def ai_json(system, prompt, max_tokens=1100):
    key = key_get()
    if not key:
        fail("KEY_MISSING", "请先在设置中填写 DeepSeek API Key", 503, True)
    body = json.dumps({"model": MODEL, "messages": [{"role":"system","content":system + "\n只返回一个合法 JSON 对象。"},{"role":"user","content":prompt}], "response_format":{"type":"json_object"}, "max_tokens":max_tokens,"thinking":{"type":"disabled"}}, ensure_ascii=False).encode()
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body, headers={"Authorization":"Bearer " + key,"Content-Type":"application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=55) as res:
            payload = json.loads(res.read(2_000_000))
        choice = payload["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("truncated")
        return json.loads(choice["message"]["content"])
    except urllib.error.HTTPError as e:
        if e.code in (401,403): fail("KEY_INVALID","DeepSeek 密钥已失效，请在设置中更新",401)
        fail("MODEL_UNAVAILABLE", "DeepSeek 暂不可用，请稍后重试", 503, True)
    except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError):
        fail("MODEL_UNAVAILABLE", "DeepSeek 暂不可用，请稍后重试", 503, True)


def insert_group(c, payload, bank="personal"):
    gid = uid()
    c.execute("INSERT INTO groups(id,bank,source_key,company,round,section,topic,interview_type,status,created_day,generated_day,reference_ids,difficulty_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (gid, bank, payload.get("source_key") or uid(), payload.get("company", ""), payload.get("round", ""), payload.get("section", ""), payload.get("topic", "other"), payload.get("interview_type", "technical"), "active", today(), payload.get("generated_day", ""), json.dumps(payload.get("reference_ids", [])), payload.get("difficulty_reason", "")))
    items = [{"text":payload["text"],"source_key":payload.get("source_key") or uid(),"source_ref":payload.get("source_ref", ""),"provenance":payload.get("provenance",bank),"answer":payload.get("answer", ""),"answer_origin":payload.get("answer_origin", ""),"answer_ref":payload.get("answer_ref", "")}] + payload.get("followups", [])
    for pos,item in enumerate(items):
        qid=uid()
        c.execute("INSERT INTO questions(id,group_id,position,text,provenance,source_key,source_ref,status,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)", (qid,gid,pos,item["text"],item.get("provenance", bank),item.get("source_key") or uid(),item.get("source_ref", ""),"active",source_parser.fingerprint(item["text"])))
        c.execute("INSERT INTO answers(question_id,primary_text,primary_origin,source_ref) VALUES(?,?,?,?)",(qid,item.get("answer", ""),item.get("answer_origin", ""),item.get("answer_ref", "")))
    return gid


def update_group(c, gid, payload):
    c.execute("UPDATE groups SET company=?,round=?,section=?,topic=?,interview_type=? WHERE id=?",(payload.get("company", ""),payload.get("round", ""),payload.get("section", ""),payload.get("topic","other"),payload.get("interview_type","technical"),gid))
    q = one(c,"SELECT * FROM questions WHERE group_id=? AND position=0",(gid,))
    c.execute("UPDATE questions SET text=?,source_ref=?,fingerprint=? WHERE id=?",(payload["text"],payload.get("source_ref",""),source_parser.fingerprint(payload["text"]),q["id"]))
    c.execute("UPDATE answers SET primary_text=?,primary_origin=?,source_ref=? WHERE question_id=?",(payload.get("answer",""),payload.get("answer_origin",""),payload.get("answer_ref",""),q["id"]))
    existing = {x["source_key"]:x for x in rows(c,"SELECT * FROM questions WHERE group_id=? AND position>0",(gid,))}
    for pos,item in enumerate(payload.get("followups",[]),1):
        old=existing.get(item.get("source_key"))
        if old:
            c.execute("UPDATE questions SET text=?,position=?,source_ref=?,fingerprint=? WHERE id=?",(item["text"],pos,item.get("source_ref",""),source_parser.fingerprint(item["text"]),old["id"]))
        else:
            qid=uid()
            c.execute("INSERT INTO questions(id,group_id,position,text,provenance,source_key,source_ref,fingerprint) VALUES(?,?,?,?,?,?,?,?)",(qid,gid,pos,item["text"],item.get("provenance","experience"),item.get("source_key") or uid(),item.get("source_ref",""),source_parser.fingerprint(item["text"])))
            c.execute("INSERT INTO answers(question_id) VALUES(?)",(qid,))


HR_SEEDS = {"国企专项": ["为什么希望应聘这一岗位？", "你怎样理解规范流程与工作效率的关系？", "如果需要从基础工作做起，你怎么看？"], "HR 通用": ["请用两分钟介绍自己。", "为什么选择这个岗位？", "你的优势和需要改进的地方是什么？", "你未来三年的职业规划是什么？", "请讲一次你与同事意见不一致的经历。", "请讲一次任务没有达到预期的经历。", "多项任务同时紧急时怎样排优先级？", "你承担过什么具体职责？", "为什么认为自己适合这个团队？", "你想向面试官了解什么？"]}


def seed_bank():
    items=json.loads((ROOT/"seeds"/"questions-v1.json").read_text(encoding="utf-8"))
    with db() as c:
        for i,item in enumerate(items):
            source_key=f"seed:v1:{i:02d}"
            if one(c,"SELECT id FROM groups WHERE source_key=?",(source_key,)): continue
            insert_group(c,{"source_key":source_key,"text":item["question"],"answer":item["answer"],"answer_origin":"curated","topic":item["topic"],"section":item["section"],"interview_type":item["interview_type"],"company":"通用精选题库","provenance":"curated"},"network")

def card(c, delivery):
    if not delivery:
        return None
    q = one(c,"SELECT q.*,g.bank,g.topic,g.interview_type,g.company,g.round,g.section,g.generated_day,g.reference_ids,g.difficulty_reason,g.resume_version AS group_resume_version FROM questions q JOIN groups g ON g.id=q.group_id WHERE q.id=?",(delivery["question_id"],))
    if not q:
        return None
    count = one(c,"SELECT COUNT(*) n FROM questions WHERE group_id=? AND status='active'",(q["group_id"],))["n"]
    out = {"delivery_id":delivery["id"],"group_id":q["group_id"],"question_id":q["id"],"text":q["text"],"bank":q["bank"],"topic":q["topic"],"interview_type":q["interview_type"],"practice_mode":delivery["practice_mode"],"focus":delivery["focus"],"company":q["company"],"round":q["round"],"section":q["section"],"position":q["position"],"group_size":count,"source_ref":q["source_ref"],"provenance":q["provenance"],"reference_ids":json.loads(q["reference_ids"] or "[]"),"revealed":bool(delivery["revealed_at"]),"show_followup_toggle":q["bank"]=="ai" and q["position"]==0 and q["generated_day"]==today() and count==1,"stale_project":bool(q["group_resume_version"] and (not resume_current(c) or q["group_resume_version"]<resume_current(c)["version"]))}
    if out["revealed"]:
        out["answer"] = answer(c,q["id"])
    return out


def answer(c,qid):
    a=one(c,"SELECT primary_text,primary_origin,source_ref,ai_supplement,resume_version FROM answers WHERE question_id=?",(qid,))
    if a:
        group=one(c,"SELECT g.section,g.resume_version FROM groups g JOIN questions q ON q.group_id=g.id WHERE q.id=?",(qid,))
        current=resume_current(c)
        a["stale_question"]=bool(group["resume_version"] and (not current or group["resume_version"]<current["version"]))
        a["stale_answer"]=bool((a["primary_origin"]=="ai" or a["ai_supplement"]) and a["resume_version"] and (not current or a["resume_version"]<current["version"]))
        a["stale_resume"]=a["stale_question"] or a["stale_answer"]
    return a


def previous_answer_context(c,q):
    if q["position"]<=0:
        return ""
    profile=resume_current(c)
    group=one(c,"SELECT resume_version FROM groups WHERE id=?",(q["group_id"],))
    if group["resume_version"] and (not profile or group["resume_version"]<profile["version"]):
        return ""
    previous=rows(c,"""SELECT q.id,q.position,q.text,a.primary_text,a.primary_origin,a.ai_supplement,a.resume_version
        FROM questions q JOIN answers a ON a.question_id=q.id
        WHERE q.group_id=? AND q.position<? ORDER BY q.position DESC LIMIT 2""",(q["group_id"],q["position"]))
    context=[]
    latest_id=previous[0]["id"] if previous else None
    for item in reversed(previous):
        stale=bool(item["resume_version"] and (not profile or item["resume_version"]<profile["version"]))
        dialogue=[] if stale else list(reversed(rows(c,"SELECT role,content FROM answer_messages WHERE question_id=? ORDER BY rowid DESC LIMIT 6",(item["id"],))))
        context.append({
            "question":item["text"][:500],
            "current_saved_answer":"" if stale else (item["primary_text"] or "")[:4500 if item["id"]==latest_id else 2200],
            "answer_origin":item["primary_origin"],
            "saved_ai_supplement":"" if stale else (item["ai_supplement"] or "")[:1500],
            "recent_user_adjustments_and_ai_explanations":[{"role":m["role"],"content":m["content"][:1000]} for m in dialogue],
        })
    return json.dumps(context,ensure_ascii=False)


def generate_reference_answer(c,qid):
    a=answer(c,qid)
    if not a: fail("NOT_FOUND","题目不存在",404)
    if a["primary_text"]: return a
    q=one(c,"SELECT q.text,q.position,q.group_id,g.interview_type FROM questions q JOIN groups g ON g.id=q.group_id WHERE q.id=?",(qid,))
    if not q: fail("NOT_FOUND","题目不存在",404)
    previous=previous_answer_context(c,q)
    continuity=(f"同一题组的前文（其中 current_saved_answer 是用户调整后当前保存的版本，优先据此承接）：{previous}。"
                "回答当前追问时，明确承接前一问的具体问题和已保存答案，结合用户最近提出的侧重点；"
                "不要重复整段前文，也不要把未确认的提议写成既成事实。" if previous else "")
    if q["interview_type"]=="hr":
        system="你是应届生 HR 面试辅导员。给出自然、具体、可口述的回答框架，先回应问题，再说明如何用真实经历支撑，最后连接岗位；不编造个人经历、单位制度或承诺。"
        prompt=f"HR 面试题：{q['text']}。{continuity}已知简历事实：{facts_text(c)}。仅使用相关且确知的事实，缺少的个人动机或例子用【需本人补充】提示。输出 JSON: {{\"answer\":\"回答框架\"}}"
    else:
        system="你是技术面试辅导员，答案准确、可口述、按要点组织，明确区分事实和假设，不编造项目经历。"
        prompt=f"技术面试题：{q['text']}。{continuity}简历事实：{facts_text(c)}。先给直接结论，再解释关键原理与适用条件；场景题给排查或设计顺序。输出 JSON: {{\"answer\":\"分点答案\"}}"
    result=ai_json(system,prompt,1800 if previous else 1500)
    value=result.get("answer","")
    if not isinstance(value,str) or not value.strip(): fail("ANSWER_GENERATION_FAILED","答案生成失败，请重试",503,True)
    version=resume_current(c)["version"] if resume_current(c) else 0
    c.execute("UPDATE answers SET primary_text=?,primary_origin='ai',generated_at=?,resume_version=? WHERE question_id=?",(value.strip()[:6000],now().isoformat(),version,qid))
    return answer(c,qid)


def validate_practice(mode, focus):
    if mode not in ("technical", "hr") or focus not in (("all",) if mode=="technical" else ("all", "common", "soe")):
        fail("INVALID_PRACTICE_MODE", "练习类型无效")


def current(c, mode="technical", focus="all"):
    return one(c,"SELECT * FROM deliveries WHERE state='active' AND practice_mode=? AND focus=? ORDER BY created_at DESC LIMIT 1",(mode,focus))


def deliver(c,gid,qid,skip_streak=0,draw_kind="followup",mode="technical",focus="all"):
    did=uid()
    c.execute("INSERT INTO deliveries(id,group_id,question_id,state,created_at,skip_streak,draw_kind,practice_mode,focus) VALUES(?,?,?,'active',?,?,?,?,?)",(did,gid,qid,now().isoformat(),skip_streak,draw_kind,mode,focus))
    return one(c,"SELECT * FROM deliveries WHERE id=?",(did,))


def choose_weighted(items, weights):
    return random.choices(items,[weights.get(x,1) for x in items],k=1)[0]


AI_CATEGORY_LABELS = {
    "fundamentals": "高频八股",
    "backend_scenario": "后端场景设计",
    "agent_scenario": "Agent 场景设计",
    "project": "项目深挖",
}
AI_BLUEPRINTS = {
    "fundamentals": [
        ("java", "HashMap 的扩容和线程安全"), ("java", "JVM 垃圾回收和常见排查指标"),
        ("java", "线程池核心参数与任务堆积"), ("java", "Spring 事务失效的常见原因"),
        ("java", "MySQL 联合索引和最左前缀"), ("java", "MVCC 与事务隔离级别"),
        ("java", "Redis 缓存穿透、击穿和雪崩的区别"), ("java", "Kafka 消费者组与重复消费"),
        ("agent", "RAG 检索、重排与引用依据"), ("agent", "Agent 工具调用与普通文本生成的区别"),
    ],
    "backend_scenario": [
        ("java", "接口超时：区分应用、数据库和外部依赖"),
        ("java", "消息重复投递：消费端幂等与失败重试"),
        ("java", "缓存和数据库不一致：更新顺序与失效策略"),
        ("java", "热点接口突增：限流、降级和观测"),
        ("java", "慢 SQL：执行计划与索引选择"),
        ("java", "订单状态重复更新：并发控制"),
    ],
    "agent_scenario": [
        ("agent", "工具调用超时或部分失败：重试与结果校验"),
        ("agent", "RAG 召回错误文档：检索质量和回答引用"),
        ("agent", "模型输出 JSON 不稳定：结构化校验与重试"),
        ("agent", "多步任务中断：状态保存和幂等恢复"),
        ("agent", "Agent 错误调用工具：权限边界和人工确认"),
        ("agent", "回答看似正确但缺少依据：评测指标与人工抽检"),
    ],
    "project": [

    ],
}


def choose_ai_category(c, allowed=None):
    if allowed:
        return random.choice(allowed)
    counts={r["section"]:r["n"] for r in rows(c,"SELECT section,COUNT(*) n FROM groups WHERE bank='ai' AND interview_type='technical' GROUP BY section")}
    total=sum(counts.get(label,0) for label in AI_CATEGORY_LABELS.values())+1
    targets={"fundamentals":.45,"backend_scenario":.275,"agent_scenario":.275}
    if resume_projects(c): targets["project"]=.10
    return choose_weighted(list(targets),{key:max(.1,share*total-counts.get(AI_CATEGORY_LABELS[key],0)+.5) for key,share in targets.items()})


def generate_ai_question(c, mode="technical", category=None, focus="all"):
    recent=[r["text"] for r in rows(c,"SELECT q.text FROM questions q JOIN groups g ON q.group_id=g.id WHERE q.position=0 AND g.interview_type=? ORDER BY g.rowid DESC LIMIT 35",(mode,))]
    if mode=="hr":
        if focus=="all":
            counts={r["section"]:r["n"] for r in rows(c,"SELECT section,COUNT(*) n FROM groups WHERE bank='ai' AND interview_type='hr' GROUP BY section")}
            section=min(HR_SEEDS,key=lambda x:counts.get(x,0))
        else:
            section="国企专项" if focus=="soe" else "HR 通用"
        seed=random.choice(HR_SEEDS[section])
        prompt=(f"方向：{section}。示例考点：{seed}。应聘者是应届 Java 后端/Agent 岗位。"
                f"最近已有题目，避免重复：{recent}。请生成一道常见且自然的 HR 主问题，"
                "只考察一个点，适合两分钟口述；国企方向可考察岗位动机、基层适应、流程合规、团队协作和长期投入，"
                "不要假定具体单位制度、个人经历或政治立场。输出 JSON：{\"text\":\"题目\"}。")
        system="你是审慎的应届生 HR 面试官。提问真实、常见、清晰，不设计刁钻或带诱导性的假设。"
        topic="other"
    else:
        category=category or choose_ai_category(c)
        if category not in AI_CATEGORY_LABELS: fail("INVALID_CATEGORY","出题方向无效")
        section=AI_CATEGORY_LABELS[category]
        if category=="project":
            projects=resume_projects(c)
            if not projects: fail("RESUME_REQUIRED","请先在设置中确认简历，再生成个人项目深挖题",409)
            project=random.choice(projects)
            topic=source_parser.topic_for(json.dumps(project,ensure_ascii=False))
            seed=json.dumps(project,ensure_ascii=False)
        else: topic,seed=random.choice(AI_BLUEPRINTS[category])
        special=("只问一个经典知识点，考察原理及适用边界，不写成项目题。" if category=="fundamentals" else
                 "给出一个常见、规模适中的工作场景，只问排查或设计中的一个核心决策。" if category in ("backend_scenario","agent_scenario") else
                 "必须明确点名种子中的那个真实项目或实现，只围绕已给事实问一个具体设计决策、原因或权衡；不得添加没有提供的业务事实。")
        prompt=(f"出题类型：{section}；指定考点：{seed}；方向：{topic}。{special}"
                f"{'已确认简历事实：'+facts_text(c) if category=='project' else '不要假设任何个人经历或项目。'}最近已有题目，避免重复：{recent}。"
                "题目适合初中级岗位，最多一句背景和一个问题；不用多租户、跨地域、亿级流量等人为加难条件。"
                "输出 JSON：{\"text\":\"题目\"}。")
        system="你是严谨的 Java 后端与 Agent 开发面试官。优先考察高频基础和真实工程决策，语言像真实面试官，不堆砌概念。"
    for _ in range(3):
        result=ai_json(system,prompt,550)
        text=result.get("text","")
        if not isinstance(text,str): continue
        text=text.strip()
        valid=(8<=len(text)<=160 and text.count("？")+text.count("?")<=1
               and not re.search(r"亿级|跨地域|多租户|全球部署|千万级",text)
               and not one(c,"SELECT id FROM questions WHERE fingerprint=?",(source_parser.fingerprint(text),)))
        if mode=="technical" and category=="project":
            anchor=project["name"]
            valid=valid and anchor in text
        if valid:
            payload={"source_key":"ai:"+uid(),"text":text,"topic":topic,"company":"AI HR 面试官" if mode=="hr" else "AI 模拟","section":section,"interview_type":mode,"provenance":"ai","generated_day":today(),"difficulty_reason":seed}
            gid=insert_group(c,payload,"ai")
            if category=="project": c.execute("UPDATE groups SET resume_version=? WHERE id=?",(resume_current(c)["version"],gid))
            return gid
        prompt += " 上一题未通过单一考点或去重检查，请换一种清晰问法。"
    fail("QUESTION_QUALITY","AI 题目未通过质量检查，请重试",503,True)


def hr_focus_sql(focus):
    return " AND g.section='国企专项'" if focus=="soe" else " AND COALESCE(g.section,'')!='国企专项'" if focus=="common" else ""


def draw(c, mode="technical", focus="all"):
    validate_practice(mode,focus)
    active=current(c,mode,focus)
    if active: return card(c,active)
    day=today()
    has_key=bool(key_get())
    answer_filter="" if has_key else " AND NOT EXISTS(SELECT 1 FROM questions x JOIN answers a ON a.question_id=x.id WHERE x.group_id=g.id AND x.status='active' AND a.primary_text='')"
    focus_filter=hr_focus_sql(focus) if mode=="hr" else ""
    due=rows(c,"""SELECT g.id,MIN(s.due_on) due_on FROM groups g JOIN questions q ON q.group_id=g.id JOIN schedules s ON s.question_id=q.id WHERE g.status='active' AND g.interview_type=? AND q.status='active' AND s.due_on<=? AND NOT EXISTS(SELECT 1 FROM events e WHERE e.group_id=g.id AND e.local_day=? AND e.action='feedback')"""+focus_filter+answer_filter+" GROUP BY g.id ORDER BY due_on LIMIT 10",(mode,day,day))
    kind="review" if mode=="technical" else "hr_review"
    if not resume_projects(c):
        due=[item for item in due if one(c,"SELECT section FROM groups WHERE id=?",(item["id"],))["section"]!="项目深挖"]
    if due:
        gid=random.choice(due[:min(3,len(due))])["id"]
    else:
        kind="new" if mode=="technical" else "hr_"+focus+"_new"
        candidates=rows(c,"""SELECT g.id,g.bank,g.topic FROM groups g JOIN questions q ON q.group_id=g.id AND q.position=0 WHERE g.status='active' AND g.interview_type=? AND g.bank IN ('personal','network') AND NOT EXISTS(SELECT 1 FROM schedules s WHERE s.question_id=q.id) AND NOT EXISTS(SELECT 1 FROM events e WHERE e.group_id=g.id AND e.local_day=?)"""+focus_filter+answer_filter,(mode,day))
        bybank={b:[r for r in candidates if r["bank"]==b] for b in ("personal","network")}
        available=[b for b in bybank if bybank[b]]
        if has_key: available.append("ai")
        if not available: return None
        actual={r["bank"]:r["n"] for r in rows(c,"SELECT bank,COUNT(*) n FROM draws WHERE draw_kind=? GROUP BY bank",(kind,))}
        total=sum(actual.values())+1
        target={"personal":.4,"network":.3,"ai":.3} if mode=="technical" else {"personal":.35,"network":.35,"ai":.3}
        normalizer=sum(target[b] for b in available)
        priority={b:max(.05,target[b]/normalizer*total-actual.get(b,0)+.4) for b in available}
        bank=choose_weighted(available,priority)
        if bank=="ai":
            try: gid=generate_ai_question(c,mode,focus=focus)
            except HTTPException:
                available=[b for b in available if b!="ai"]
                if not available: raise
                bank=choose_weighted(available,{b:target[b] for b in available})
                gid=None
        else: gid=None
        if gid is None:
            if mode=="technical":
                weights=json.loads(one(c,"SELECT value FROM settings WHERE key='weights'")["value"])
                gid=random.choices(bybank[bank],[max(1,weights.get(r["topic"],10)) for r in bybank[bank]],k=1)[0]["id"]
            else:
                gid=random.choice(bybank[bank])["id"]
    main=one(c,"SELECT id FROM questions WHERE group_id=? AND position=0",(gid,))
    g=one(c,"SELECT bank,topic FROM groups WHERE id=?",(gid,))
    c.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?)",(uid(),gid,g["bank"],g["topic"],kind,day,now().isoformat()))
    return card(c,deliver(c,gid,main["id"],draw_kind=kind,mode=mode,focus=focus))


def next_in_group(c,d, want_followup=False,draft=""):
    q=one(c,"SELECT * FROM questions WHERE id=?",(d["question_id"],))
    g=one(c,"SELECT * FROM groups WHERE id=?",(d["group_id"],))
    nxt=one(c,"SELECT * FROM questions WHERE group_id=? AND position>? AND status='active' ORDER BY position LIMIT 1",(g["id"],q["position"]))
    if nxt: return card(c,deliver(c,g["id"],nxt["id"],mode=d["practice_mode"],focus=d["focus"]))
    if g["bank"]=="ai" and q["position"]==0 and g["generated_day"]==today() and want_followup:
        return generated_followup(c,g,d,draft)
    return draw(c,d["practice_mode"],d["focus"])


def generated_followup(c,g,d,draft=""):
    existing=rows(c,"SELECT text FROM questions WHERE group_id=? AND status='active' ORDER BY position",(g["id"],))
    if len(existing)>2: return draw(c,d["practice_mode"],d["focus"])
    guide="HR 追问围绕动机、真实行为和岗位匹配，不预设经历。" if d["practice_mode"]=="hr" else facts_text(c)
    result=ai_json("你是面试官，追问应紧贴原题，只问一个更具体的点，难度逐步增加。",f"题组：{[r['text'] for r in existing]}。临时口述草稿（如有）：{draft[:3000]}。{guide}。输出 JSON: {{\"text\":\"一道追问\"}}",450)
    text=result.get("text","").strip()
    if not (5<=len(text)<=180) or source_parser.fingerprint(text) in [source_parser.fingerprint(x["text"]) for x in existing]:
        fail("FOLLOWUP_FAILED","追问生成未通过检查，请重试",503,True)
    pos=one(c,"SELECT COALESCE(MAX(position),0)+1 n FROM questions WHERE group_id=?",(g["id"],))["n"]
    qid=uid()
    c.execute("INSERT INTO questions(id,group_id,position,text,provenance,source_key,fingerprint) VALUES(?,?,?,?,?,?,?)",(qid,g["id"],pos,text,"ai","ai:"+uid(),source_parser.fingerprint(text)))
    c.execute("INSERT INTO answers(question_id) VALUES(?)",(qid,))
    return card(c,deliver(c,g["id"],qid,skip_streak=d["skip_streak"],mode=d["practice_mode"],focus=d["focus"]))


@app.get("/")
def homepage():
    return FileResponse(ROOT/"static"/"index.html")


@app.get("/static/{name}")
def assets(name: str):
    if name not in ("app.js","style.css"):
        fail("NOT_FOUND","文件不存在",404)
    return FileResponse(ROOT/"static"/name)


@app.get("/api/health")
def health():
    return {"ok":True,"app":"interview-question-bank-open"}


@app.get("/api/practice/current")
def practice_current(mode: str="technical", focus: str="all"):
    validate_practice(mode,focus)
    with db() as c: return {"current":card(c,current(c,mode,focus))}


@app.post("/api/practice/draw")
def practice_draw(mode: str="technical", focus: str="all"):
    validate_practice(mode,focus)
    with LOCK,db() as c:
        return {"current":draw(c,mode,focus)}


class AIQuestionRequest(BaseModel):
    mode: str="technical"
    focus: str="all"
    category: str=""
    categories: list[str]=Field(default_factory=list,max_length=4)


@app.post("/api/practice/ai-question")
def practice_ai_question(data: AIQuestionRequest):
    validate_practice(data.mode,data.focus)
    selected=data.categories or ([data.category] if data.category else [])
    if data.mode=="technical" and (data.category not in ("",*AI_CATEGORY_LABELS) or any(x not in AI_CATEGORY_LABELS for x in selected)):
        fail("INVALID_CATEGORY","出题方向无效")
    with LOCK,db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            category=choose_ai_category(c,selected) if data.mode=="technical" and selected else None
            gid=generate_ai_question(c,data.mode,category,data.focus)
            active=current(c,data.mode,data.focus)
            if active: c.execute("UPDATE deliveries SET state='replaced' WHERE id=?",(active["id"],))
            main=one(c,"SELECT id FROM questions WHERE group_id=? AND position=0",(gid,))
            g=one(c,"SELECT topic FROM groups WHERE id=?",(gid,))
            kind="new" if data.mode=="technical" else "hr_"+data.focus+"_new"
            c.execute("INSERT INTO draws VALUES(?,?,?,?,?,?,?)",(uid(),gid,"ai",g["topic"],kind,today(),now().isoformat()))
            result=card(c,deliver(c,gid,main["id"],draw_kind=kind,mode=data.mode,focus=data.focus))
            c.commit()
            return {"current":result}
        except Exception:
            c.rollback()
            raise


@app.post("/api/practice/{delivery_id}/reveal")
def reveal(delivery_id: str):
    with LOCK,db() as c:
        d=one(c,"SELECT * FROM deliveries WHERE id=?",(delivery_id,))
        if not d or d["state"]!="active": fail("INVALID_TRANSITION","当前题已结束，请刷新",409)
        a=answer(c,d["question_id"])
        q=one(c,"SELECT q.*,g.interview_type FROM questions q JOIN groups g ON g.id=q.group_id WHERE q.id=?",(d["question_id"],))
        if not a["primary_text"]:
            generate_reference_answer(c,q["id"])
        elif q["interview_type"]=="technical" and a["primary_origin"]=="personal" and not a["ai_supplement"] and key_get():
            try:
                result=ai_json("在不改动个人原文的前提下，仅补充必要的技术遗漏或纠错；没有则返回空字符串。",f"题目：{q['text']}。个人原文：{a['primary_text'][:6000]}。{facts_text(c)}。输出 JSON: {{\"supplement\":\"简短补充\"}}",450)
                version=resume_current(c)["version"] if resume_current(c) else 0
                c.execute("UPDATE answers SET ai_supplement=?,resume_version=? WHERE question_id=?",(result.get("supplement","")[:2500],version,q["id"]))
            except HTTPException: pass
        c.execute("UPDATE deliveries SET revealed_at=COALESCE(revealed_at,?) WHERE id=?",(now().isoformat(),delivery_id))
        return {"current":card(c,one(c,"SELECT * FROM deliveries WHERE id=?",(delivery_id,)))}


class Feedback(BaseModel):
    grade: str
    want_followup: bool=False
    draft_answer: str=""
    idempotency_key: str=""


@app.post("/api/practice/{delivery_id}/feedback")
def feedback(delivery_id: str, data: Feedback):
    if data.grade not in ("can_answer","partial","cannot_answer"): fail("INVALID_GRADE","无效反馈")
    with LOCK,db() as c:
        d=one(c,"SELECT * FROM deliveries WHERE id=?",(delivery_id,))
        if not d: fail("NOT_FOUND","题卡不存在",404)
        if d["state"]!="active": return {"current":card(c,current(c,d["practice_mode"],d["focus"])),"duplicate":True}
        if not d["revealed_at"]: fail("INVALID_TRANSITION","请先查看答案",409)
        days={"cannot_answer":1,"partial":3,"can_answer":5}[data.grade]
        due=(now().date()+timedelta(days=days)).isoformat()
        q=one(c,"SELECT q.text,g.bank FROM questions q JOIN groups g ON g.id=q.group_id WHERE q.id=?",(d["question_id"],))
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("UPDATE deliveries SET state='done' WHERE id=? AND state='active'",(delivery_id,))
            c.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",(uid(),delivery_id,d["question_id"],d["group_id"],q["bank"],"feedback",data.grade,now().isoformat(),today(),q["text"]))
            c.execute("INSERT INTO schedules VALUES(?,?,?,?) ON CONFLICT(question_id) DO UPDATE SET grade=excluded.grade,due_on=excluded.due_on,reviewed_at=excluded.reviewed_at",(d["question_id"],data.grade,due,now().isoformat()))
            c.commit()
        except Exception:
            c.rollback(); raise
        try:
            nxt=next_in_group(c,{**d,"skip_streak":0},data.want_followup,data.draft_answer)
            return {"current":nxt,"due_on":due}
        except HTTPException as exc:
            # Feedback is already durable. Clear transition and keep practice available.
            try: nxt=draw(c,d["practice_mode"],d["focus"])
            except HTTPException: nxt=None
            return {"current":nxt,"due_on":due,"notice":exc.detail.get("message","追问暂不可用")}


@app.post("/api/practice/{delivery_id}/skip")
def skip(delivery_id: str):
    with LOCK,db() as c:
        d=one(c,"SELECT * FROM deliveries WHERE id=?",(delivery_id,))
        if not d: fail("NOT_FOUND","题卡不存在",404)
        if d["state"]!="active": return {"current":card(c,current(c,d["practice_mode"],d["focus"])),"duplicate":True}
        q=one(c,"SELECT * FROM questions WHERE id=?",(d["question_id"],))
        g=one(c,"SELECT * FROM groups WHERE id=?",(d["group_id"],))
        c.execute("UPDATE deliveries SET state='skipped' WHERE id=?",(delivery_id,))
        c.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?,?)",(uid(),delivery_id,q["id"],g["id"],g["bank"],"skip","",now().isoformat(),today(),q["text"]))
        if q["position"]==0:
            c.execute("UPDATE groups SET status='skipped' WHERE id=?",(g["id"],))
            try: nxt=draw(c,d["practice_mode"],d["focus"])
            except HTTPException: nxt=None
            return {"current":nxt}
        c.execute("UPDATE questions SET status='skipped' WHERE id=?",(q["id"],))
        if d["skip_streak"]>=1:
            try: nxt=draw(c,d["practice_mode"],d["focus"])
            except HTTPException: nxt=None
            return {"current":nxt,"notice":"已连续跳过两道追问，进入新题"}
        try:
            nxt=generated_followup(c,g,{**d,"skip_streak":1})
            return {"current":nxt}
        except HTTPException:
            try: nxt=draw(c,d["practice_mode"],d["focus"])
            except HTTPException: nxt=None
            return {"current":nxt,"notice":"替代追问暂不可用，已进入新题"}

@app.get("/api/banks/{bank}/groups")
def bank_groups(bank: str, query: str="", interview_type: str="", topic: str="", status: str="", page: int=1):
    if bank not in ("personal","network","ai"): fail("INVALID_BANK","题库不存在",404)
    page=max(1,page)
    with db() as c:
        where=["g.bank=?"]
        args=[bank]
        if query:
            where.append("(q.text LIKE ? OR g.company LIKE ? OR g.section LIKE ?)")
            args += ["%"+query+"%"]*3
        if interview_type in ("technical","hr"):
            where.append("g.interview_type=?");args.append(interview_type)
        if topic in ("agent","java","other"):
            where.append("g.topic=?");args.append(topic)
        if status in ("active","skipped","deleted"):
            where.append("g.status=?");args.append(status)
        elif not status:
            where.append("g.status!='deleted'")
        cond=" AND ".join(where)
        total=one(c,"SELECT COUNT(*) n FROM groups g JOIN questions q ON q.group_id=g.id AND q.position=0 WHERE "+cond,args)["n"]
        data=rows(c,"SELECT g.*,q.text,q.source_ref,q.id main_question_id,(SELECT COUNT(*) FROM questions x WHERE x.group_id=g.id AND x.position>0 AND x.status='active') followup_count,(SELECT COUNT(*) FROM answers a WHERE a.question_id=q.id AND a.primary_text!='') has_answer FROM groups g JOIN questions q ON q.group_id=g.id AND q.position=0 WHERE "+cond+" ORDER BY g.rowid DESC LIMIT 30 OFFSET ?",args+[(page-1)*30])
        return {"total":total,"page":page,"groups":data}


@app.get("/api/groups/{group_id}")
def group_detail(group_id: str):
    with db() as c:
        g=one(c,"SELECT * FROM groups WHERE id=?",(group_id,))
        if not g: fail("NOT_FOUND","题组不存在",404)
        g["questions"]=rows(c,"SELECT q.id,q.position,q.text,q.provenance,q.source_ref,q.status,CASE WHEN a.primary_text!='' THEN 1 ELSE 0 END has_answer FROM questions q JOIN answers a ON a.question_id=q.id WHERE q.group_id=? ORDER BY position",(group_id,))
        g["reference_ids"]=json.loads(g["reference_ids"] or "[]")
        return g


@app.post("/api/questions/{question_id}/answer/reveal")
def bank_answer(question_id: str):
    with LOCK,db() as c:
        q=one(c,"SELECT * FROM questions WHERE id=?",(question_id,))
        if not q: fail("NOT_FOUND","题目不存在",404)
        return {"answer":generate_reference_answer(c,question_id)}


@app.get("/api/questions/{question_id}/answer/chat")
def answer_chat_history(question_id: str):
    with db() as c:
        if not one(c,"SELECT id FROM questions WHERE id=?",(question_id,)):
            fail("NOT_FOUND","题目不存在",404)
        history=rows(c,"SELECT role,content,proposed_answer FROM answer_messages WHERE question_id=? ORDER BY rowid",(question_id,))
        return {"messages":history,"answer":answer(c,question_id)}


class AnswerChatInput(BaseModel):
    message: str=Field(min_length=2,max_length=1500)


@app.post("/api/questions/{question_id}/answer/chat")
def answer_chat(question_id: str,data: AnswerChatInput):
    message=data.message.strip()
    if len(message)<2: fail("EMPTY_MESSAGE","请输入想讨论的问题")
    with LOCK,db() as c:
        q=one(c,"SELECT q.text,q.position,q.group_id,g.interview_type FROM questions q JOIN groups g ON g.id=q.group_id WHERE q.id=?",(question_id,))
        if not q: fail("NOT_FOUND","题目不存在",404)
        a=answer(c,question_id)
        if not a or not a["primary_text"]: fail("ANSWER_REQUIRED","请先查看参考答案",409)
        history=list(reversed(rows(c,"SELECT role,content FROM answer_messages WHERE question_id=? ORDER BY rowid DESC LIMIT 12",(question_id,))))
        previous=previous_answer_context(c,q)
        guide=("HR 回答要真实、自然；没有提供的个人动机、单位情况或经历不能编造。" if q["interview_type"]=="hr" else
               "技术解释要准确，项目内容只能使用已知事实，不能虚构规模、实现和指标。")
        prompt=(f"题目：{q['text']}。当前参考答案：{a['primary_text'][:6000]}。"
                f"当前答案中已保存的 AI 补充：{(a['ai_supplement'] or '')[:1500]}。"
                f"同一题组此前的问题、已保存答案和用户调整：{previous or '无'}。"
                f"已知事实：{facts_text(c)}。之前的对话：{json.dumps(history,ensure_ascii=False)}。"
                f"用户这次说：{message}。{guide}。先直接回应用户，再判断是否需要提供可替换当前答案的完整优化版。"
                "解释性追问可以只讲解；要求调整侧重点、补充或润色时给完整新版答案。"
                "输出 JSON：{\"reply\":\"对用户的讲解\",\"revised_answer\":\"完整新版答案；无需修改则为空\"}。")
        result=ai_json("你是面试辅导员，逐轮讨论答案，承接前一问和用户已保存的调整；未保存的建议稿不能当作定稿。不要把建议自动写入参考答案。",prompt,2200)
        reply=result.get("reply","")
        revised=result.get("revised_answer","")
        if not isinstance(reply,str) or not reply.strip(): fail("CHAT_FAILED","暂时无法给出讲解，请重试",503,True)
        if not isinstance(revised,str): revised=""
        reply=reply.strip()[:4000];revised=revised.strip()[:6000]
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute("INSERT INTO answer_messages VALUES(?,?,?,?,?,?)",(uid(),question_id,"user",message,"",now().isoformat()))
            c.execute("INSERT INTO answer_messages VALUES(?,?,?,?,?,?)",(uid(),question_id,"assistant",reply,revised,now().isoformat()))
            c.commit()
        except Exception:
            c.rollback();raise
        return {"reply":reply,"revised_answer":revised}


class AnswerEdit(BaseModel):
    text: str=Field(min_length=10,max_length=6000)


@app.put("/api/questions/{question_id}/answer")
def save_ai_answer(question_id: str,data: AnswerEdit):
    value=data.text.strip()
    if len(value)<10: fail("ANSWER_TOO_SHORT","答案内容太短")
    with LOCK,db() as c:
        a=answer(c,question_id)
        if not a: fail("NOT_FOUND","题目不存在",404)
        if a["primary_origin"]!="ai": fail("PERSONAL_ANSWER","只能替换 AI 生成的参考答案；个人原文会保留",409)
        if value!=a["primary_text"]:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("INSERT INTO answer_versions VALUES(?,?,?,?)",(uid(),question_id,a["primary_text"],now().isoformat()))
                c.execute("UPDATE answers SET primary_text=?,generated_at=? WHERE question_id=?",(value,now().isoformat(),question_id))
                c.commit()
            except Exception:
                c.rollback();raise
        return {"answer":answer(c,question_id)}


class SingleQuestion(BaseModel):
    text: str=Field(min_length=4,max_length=500)
    answer: str=""
    followups: list[str]=[]
    topic: str="other"
    interview_type: str="technical"
    source_ref: str=""
    company: str=""
    round: str=""


@app.post("/api/banks/{bank}/questions")
def add_single(bank: str, data: SingleQuestion):
    if bank not in ("personal","network"):
        fail("INVALID_BANK","只能添加到个人或网络面经题库")
    if data.topic not in ("agent","java","other") or data.interview_type not in ("technical","hr"):
        fail("INVALID_INPUT","方向或类型无效")
    payload={"text":data.text.strip(),"answer":data.answer.strip(),"answer_origin":bank if data.answer.strip() else "","source_ref":data.source_ref.strip(),"company":data.company.strip() or ("个人面经" if bank=="personal" else "网络面经"),"round":data.round.strip(),"topic":data.topic,"interview_type":data.interview_type,"source_key":bank+":"+uid(),"followups":[{"text":t.strip(),"provenance":bank} for t in data.followups if t.strip()]}
    with LOCK,db() as c:
        gid=insert_group(c,payload,bank)
        return {"group_id":gid}


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: str):
    with LOCK,db() as c:
        g=one(c,"SELECT id FROM groups WHERE id=?",(group_id,))
        if not g: fail("NOT_FOUND","题组不存在",404)
        c.execute("UPDATE groups SET status='deleted' WHERE id=?",(group_id,))
        c.execute("UPDATE deliveries SET state='ended' WHERE group_id=? AND state='active'",(group_id,))
        return {"ok":True}


@app.post("/api/groups/{group_id}/restore")
def restore_group(group_id: str):
    with LOCK,db() as c:
        g=one(c,"SELECT id FROM groups WHERE id=?",(group_id,))
        if not g: fail("NOT_FOUND","题组不存在",404)
        c.execute("UPDATE groups SET status='active' WHERE id=?",(group_id,))
        c.execute("UPDATE questions SET status='active' WHERE group_id=?",(group_id,))
        return {"ok":True}


def candidate_action(c,item):
    existing=one(c,"SELECT id FROM groups WHERE source_key=?",(item["source_key"],))
    if existing:
        g=one(c,"SELECT q.text,a.primary_text,g.interview_type,g.topic FROM groups g JOIN questions q ON q.group_id=g.id AND q.position=0 JOIN answers a ON a.question_id=q.id WHERE g.id=?",(existing["id"],))
        if any((g["text"]!=item["text"],g["primary_text"]!=item.get("answer",""),g["interview_type"]!=item.get("interview_type"),g["topic"]!=item.get("topic"))):
            return "update"
        return "duplicate"
    exact=one(c,"SELECT id FROM questions WHERE fingerprint=? LIMIT 1",(source_parser.fingerprint(item["text"]),))
    return "possible_duplicate" if exact else "new"


def create_job(c,kind,items,source_text=""):
    job=uid()
    c.execute("INSERT INTO import_jobs(id,kind,status,created_at,source_text) VALUES(?,?,'preview',?,?)",(job,kind,now().isoformat(),source_text))
    for item in items:
        action=candidate_action(c,item)
        confidence=float(item.get("confidence",.9))
        selected=int(confidence>=.8 and action in ("new","update"))
        note=item.get("match_note","")
        if action=="possible_duplicate": note=(note+" 题干与已有题相同，请确认来源和题意。 ").strip()
        c.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?)",(uid(),job,item["source_key"],action,confidence,selected,json.dumps(item,ensure_ascii=False),note))
    return job



class BulkPreview(BaseModel):
    text: str=Field(min_length=4,max_length=30000)


def bulk_chunks(text, limit=8000):
    chunks=[]
    current=""
    for line in text.splitlines(keepends=True):
        while len(line)>limit:
            if current.strip(): chunks.append(current);current=""
            chunks.append(line[:limit]);line=line[limit:]
        if current and len(current)+len(line)>limit:
            chunks.append(current);current=""
        current+=line
    if current.strip(): chunks.append(current)
    return chunks


def parse_bulk_experience(text, bank):
    system=("你是面试记录整理助手。按语义整理原文中的主问题与追问，保持原始顺序。"
            "编号、换行、空行不代表一定是新主问题；若后一句依赖上一问题的背景、追问其细节或条件，归入上一题组。"
            "独立主题才建立新题组。技术面和 HR 面按内容分别判断。"
            "题干、追问和已有答案只从原文摘录，不编造问题、不补写答案。背景叙述可用来理解题目，不要凭背景自行出题。"
            "返回 groups 数组。每组含 text 主问题、answer 原文答案或空字符串、followups 数组（每项含 text 和 answer）、"
            "interview_type（technical 或 hr）、topic（agent、java 或 other）、company、round、section。"
            "如果本段开头是上一段主问题的追问，把首组 attach_to_previous 设为 true；否则 false。")
    groups=[]
    for index,chunk in enumerate(bulk_chunks(text)):
        previous=groups[-1]["text"] if groups else "无"
        prompt=f"来源：{'个人面经' if bank=='personal' else '网络面经'}。上一段最后的主问题（只用于判断跨段追问，不要重复输出）：{previous}\n当前原文第 {index+1} 段：\n{chunk}"
        result=ai_json(system,prompt,max_tokens=7000)
        parsed=result.get("groups") if isinstance(result,dict) else None
        if not isinstance(parsed,list) or not parsed:
            fail("PARSE_FAILED","AI 未能完整识别这段面经，请检查原文后重试",503,True)
        for pos,entry in enumerate(parsed):
            if not isinstance(entry,dict): fail("PARSE_FAILED","AI 拆题结果格式有误，请重试",503,True)
            question=str(entry.get("text") or "").strip()
            if len(question)<4 or len(question)>500:
                fail("PARSE_FAILED","AI 拆题结果有不完整的题目，请重试",503,True)
            raw_followups=entry.get("followups",[])
            if not isinstance(raw_followups,list): fail("PARSE_FAILED","AI 追问格式有误，请重试",503,True)
            followups=[]
            for follow in raw_followups:
                if not isinstance(follow,dict) or not str(follow.get("text") or "").strip():
                    fail("PARSE_FAILED","AI 追问格式有误，请重试",503,True)
                follow_text=str(follow["text"]).strip()
                follow_answer=str(follow.get("answer") or "").strip()
                followups.append({"text":follow_text,"answer":follow_answer,"answer_origin":bank if follow_answer else "","provenance":bank})
            if index>0 and pos==0 and entry.get("attach_to_previous") is True and groups:
                answer=str(entry.get("answer") or "").strip()
                groups[-1]["followups"].append({"text":question,"answer":answer,"answer_origin":bank if answer else "","provenance":bank})
                groups[-1]["followups"].extend(followups)
                continue
            answer=str(entry.get("answer") or "").strip()
            kind=entry.get("interview_type")
            if kind not in ("technical","hr"): kind="technical"
            topic=entry.get("topic")
            if topic not in ("agent","java","other"): topic=source_parser.topic_for(question)
            exact=source_parser.normalized(question) in source_parser.normalized(text)
            groups.append({"source_key":bank+":"+uid(),"text":question,"answer":answer,"answer_origin":bank if answer else "","source_ref":"","company":str(entry.get("company") or "").strip() or ("个人面经" if bank=="personal" else "网络面经"),"round":str(entry.get("round") or "").strip(),"section":str(entry.get("section") or "").strip(),"topic":topic,"interview_type":kind,"confidence":.9 if exact else .65,"match_note":"题干与原文不完全一致，请核对。" if not exact else "","followups":followups})
    if not groups: fail("PARSE_FAILED","没有识别到可导入的题组，请检查原文",503,True)
    return groups


@app.post("/api/imports/{bank}/preview")
def bulk_preview(bank: str, data: BulkPreview):
    if bank not in ("personal","network"):
        fail("INVALID_BANK","只能导入个人或网络面经")
    items=parse_bulk_experience(data.text.strip(),bank)
    with LOCK,db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            job=create_job(c,bank,items,data.text)
            c.commit()
        except Exception:
            c.rollback();raise
        return {"job_id":job,"count":len(items),"followup_count":sum(len(x["followups"]) for x in items)}


@app.get("/api/imports/pending")
def pending_bulk(bank: str):
    if bank not in ("personal","network"):
        fail("INVALID_BANK","只能查看个人或网络面经导入")
    with db() as c:
        row=one(c,"SELECT id FROM import_jobs WHERE kind=? AND status='preview' ORDER BY created_at DESC LIMIT 1",(bank,))
        return {"job_id":row["id"] if row else None}


@app.get("/api/imports/{job_id}")
def import_job(job_id: str):
    with db() as c:
        job=one(c,"SELECT * FROM import_jobs WHERE id=?",(job_id,))
        if not job: fail("NOT_FOUND","导入任务不存在",404)
        job["summary"]=rows(c,"SELECT action,COUNT(*) n,SUM(selected) selected FROM candidates WHERE job_id=? GROUP BY action",(job_id,))
        return job


@app.get("/api/imports/{job_id}/candidates")
def import_candidates(job_id: str, page: int=1, action: str="", interview_type: str=""):
    with db() as c:
        cond="job_id=?" + (" AND action=?" if action else "")
        args=[job_id]+([action] if action else [])
        if interview_type in ("technical","hr"):
            cond+=" AND json_extract(payload,'$.interview_type')=?"
            args.append(interview_type)
        total=one(c,"SELECT COUNT(*) n FROM candidates WHERE "+cond,args)["n"]
        result=rows(c,"SELECT * FROM candidates WHERE "+cond+" ORDER BY selected ASC,action,source_key LIMIT 40 OFFSET ?",args+[(max(1,page)-1)*40])
        for x in result: x["payload"]=json.loads(x["payload"])
        return {"total":total,"page":page,"candidates":result}


@app.get("/api/imports/{job_id}/targets")
def candidate_targets(job_id: str):
    with db() as c:
        result=[]
        for row in rows(c,"SELECT id,payload FROM candidates WHERE job_id=? ORDER BY rowid",(job_id,)):
            p=json.loads(row["payload"])
            result.append({"id":row["id"],"text":p["text"],"company":p.get("company",""),"round":p.get("round","")})
        return {"targets":result}


class CandidateEdit(BaseModel):
    selected: Optional[bool]=None
    text: Optional[str]=None
    answer: Optional[str]=None
    topic: Optional[str]=None
    interview_type: Optional[str]=None
    followups: Optional[list[dict]]=None


@app.patch("/api/imports/{job_id}/candidates/{candidate_id}")
def edit_candidate(job_id: str,candidate_id: str,data: CandidateEdit):
    with LOCK,db() as c:
        job=one(c,"SELECT status,kind FROM import_jobs WHERE id=?",(job_id,))
        if not job or job["status"]!="preview": fail("INVALID_TRANSITION","预览已结束",409)
        row=one(c,"SELECT * FROM candidates WHERE id=? AND job_id=?",(candidate_id,job_id))
        if not row: fail("NOT_FOUND","候选题不存在",404)
        payload=json.loads(row["payload"])
        for field in ("text","answer","topic","interview_type","followups"):
            val=getattr(data,field)
            if val is not None: payload[field]=val
        if data.answer is not None:
            payload["answer_origin"]=("network" if job["kind"]=="network" else "personal") if data.answer.strip() else ""
        if not payload["text"].strip() or payload["topic"] not in ("agent","java","other") or payload["interview_type"] not in ("technical","hr"):
            fail("INVALID_INPUT","题干、方向或类型无效")
        selected=row["selected"] if data.selected is None else int(data.selected)
        c.execute("UPDATE candidates SET payload=?,selected=? WHERE id=?",(json.dumps(payload,ensure_ascii=False),selected,candidate_id))
        return {"ok":True}


@app.delete("/api/imports/{job_id}/candidates/{candidate_id}")
def delete_candidate(job_id: str,candidate_id: str):
    with LOCK,db() as c:
        job=one(c,"SELECT * FROM import_jobs WHERE id=?",(job_id,))
        if not job or job["status"]!="preview": fail("INVALID_TRANSITION","预览已结束",409)
        c.execute("BEGIN IMMEDIATE")
        try:
            deleted=c.execute("DELETE FROM candidates WHERE id=? AND job_id=?",(candidate_id,job_id)).rowcount
            if not deleted: fail("NOT_FOUND","候选题不存在",404)
            finished=finish_job_if_empty(c,job)
            c.commit()
        except Exception:
            c.rollback();raise
        return {"ok":True,"finished":finished}


def finish_job_if_empty(c,job):
    if one(c,"SELECT id FROM candidates WHERE job_id=? LIMIT 1",(job["id"],)):
        return False
    c.execute("UPDATE import_jobs SET status='committed',committed_at=? WHERE id=?",(now().isoformat(),job["id"]))
    return True


@app.post("/api/imports/{job_id}/candidates/{candidate_id}/import")
def import_candidate(job_id: str,candidate_id: str):
    with LOCK,db() as c:
        job=one(c,"SELECT * FROM import_jobs WHERE id=?",(job_id,))
        if not job or job["status"]!="preview": fail("INVALID_TRANSITION","预览已结束",409)
        row=one(c,"SELECT * FROM candidates WHERE id=? AND job_id=?",(candidate_id,job_id))
        if not row: fail("NOT_FOUND","候选题不存在",404)
        payload=json.loads(row["payload"])
        c.execute("BEGIN IMMEDIATE")
        try:
            existing=one(c,"SELECT id,status FROM groups WHERE source_key=?",(payload["source_key"],))
            if existing:
                update_group(c,existing["id"],payload)
                if existing["status"]=="deleted":
                    c.execute("UPDATE groups SET status='active' WHERE id=?",(existing["id"],))
                gid=existing["id"]
                action="update"
            else:
                gid=insert_group(c,payload,"network" if job["kind"]=="network" else "personal")
                action="new"
            c.execute("DELETE FROM candidates WHERE id=?",(candidate_id,))
            finished=finish_job_if_empty(c,job)
            c.commit()
        except Exception:
            c.rollback();raise
        return {"ok":True,"group_id":gid,"action":action,"finished":finished}


class SplitInput(BaseModel):
    index: int


@app.post("/api/imports/{job_id}/candidates/{candidate_id}/split")
def split_candidate(job_id: str,candidate_id: str,data: SplitInput):
    with LOCK,db() as c:
        row=one(c,"SELECT * FROM candidates WHERE id=? AND job_id=?",(candidate_id,job_id))
        job=one(c,"SELECT status FROM import_jobs WHERE id=?",(job_id,))
        if not row or not job or job["status"]!="preview": fail("INVALID_TRANSITION","候选不可修改",409)
        p=json.loads(row["payload"])
        children=p.get("followups",[])
        if data.index<0 or data.index>=len(children): fail("INVALID_INPUT","请选择一道追问")
        chosen=children[data.index]
        new=dict(p)
        new.update({"source_key":chosen.get("source_key") or "split:"+uid(),"text":chosen["text"],"source_ref":chosen.get("source_ref",""),"answer":chosen.get("answer",""),"answer_origin":chosen.get("answer_origin",""),"answer_ref":chosen.get("answer_ref",""),"followups":children[data.index+1:]})
        p["followups"]=children[:data.index]
        c.execute("UPDATE candidates SET payload=? WHERE id=?",(json.dumps(p,ensure_ascii=False),candidate_id))
        c.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?)",(uid(),job_id,new["source_key"],candidate_action(c,new),row["confidence"],row["selected"],json.dumps(new,ensure_ascii=False),"从原题组拆分，请确认题意"))
        return {"ok":True}


class MergeInput(BaseModel):
    target_id: str


@app.post("/api/imports/{job_id}/candidates/{candidate_id}/merge")
def merge_candidate(job_id: str,candidate_id: str,data: MergeInput):
    if candidate_id==data.target_id: fail("INVALID_INPUT","不能并入自身")
    with LOCK,db() as c:
        job=one(c,"SELECT status,kind FROM import_jobs WHERE id=?",(job_id,))
        source=one(c,"SELECT * FROM candidates WHERE id=? AND job_id=?",(candidate_id,job_id))
        target=one(c,"SELECT * FROM candidates WHERE id=? AND job_id=?",(data.target_id,job_id))
        if not job or job["status"]!="preview" or not source or not target: fail("INVALID_TRANSITION","候选不可修改",409)
        s=json.loads(source["payload"]);t=json.loads(target["payload"])
        if (s.get("company"),s.get("round"))!=(t.get("company"),t.get("round")):
            fail("INVALID_INPUT","只能合并同一公司与轮次的题目")
        child={"text":s["text"],"source_key":s["source_key"],"source_ref":s.get("source_ref",""),"provenance":s.get("provenance",job["kind"]),"answer":s.get("answer",""),"answer_origin":s.get("answer_origin",""),"answer_ref":s.get("answer_ref","")}
        t["followups"]=t.get("followups",[])+[child]+s.get("followups",[])
        c.execute("UPDATE candidates SET payload=? WHERE id=?",(json.dumps(t,ensure_ascii=False),data.target_id))
        c.execute("DELETE FROM candidates WHERE id=?",(candidate_id,))
        return {"ok":True}


@app.post("/api/imports/{job_id}/commit")
def commit_import(job_id: str):
    with LOCK,db() as c:
        job=one(c,"SELECT * FROM import_jobs WHERE id=?",(job_id,))
        if not job: fail("NOT_FOUND","导入任务不存在",404)
        if job["status"]=="committed": return {"ok":True,"duplicate":True}
        if job["status"]!="preview": fail("INVALID_TRANSITION","任务不能提交",409)
        candidates=rows(c,"SELECT payload,action FROM candidates WHERE job_id=? AND selected=1",(job_id,))
        counts={"new":0,"update":0,"restored":0}
        c.execute("BEGIN IMMEDIATE")
        try:
            for row in candidates:
                payload=json.loads(row["payload"])
                existing=one(c,"SELECT id,status FROM groups WHERE source_key=?",(payload["source_key"],))
                if existing:
                    update_group(c,existing["id"],payload)
                    counts["update"]+=1
                    if existing["status"]=="deleted":
                        c.execute("UPDATE groups SET status='active' WHERE id=?",(existing["id"],))
                        counts["restored"]+=1
                elif row["action"]!="possible_duplicate":
                    insert_group(c,payload,job["kind"] if job["kind"]=="network" else "personal")
                    counts["new"]+=1
            c.execute("UPDATE import_jobs SET status='committed',committed_at=? WHERE id=?",(now().isoformat(),job_id))
            c.commit()
        except Exception:
            c.rollback();raise
        return {"ok":True,"counts":counts}


@app.get("/api/records/calendar")
def record_calendar():
    with db() as c:
        return {"days":rows(c,"SELECT local_day day,COUNT(*) count,SUM(grade='can_answer') can_answer,SUM(grade='partial') partial,SUM(grade='cannot_answer') cannot_answer FROM events WHERE action='feedback' GROUP BY local_day ORDER BY local_day"),"today":today()}


@app.get("/api/records/history")
def record_history(page: int=1,grade: str="",bank: str=""):
    with db() as c:
        clauses=["action='feedback'"];args=[]
        if grade:clauses.append("grade=?");args.append(grade)
        if bank:clauses.append("bank=?");args.append(bank)
        cond=" AND ".join(clauses)
        return {"total":one(c,"SELECT COUNT(*) n FROM events WHERE "+cond,args)["n"],"events":rows(c,"SELECT question_text,bank,grade,occurred_at,local_day FROM events WHERE "+cond+" ORDER BY occurred_at DESC LIMIT 50 OFFSET ?",args+[(max(1,page)-1)*50])}


@app.get("/api/records/due")
def record_due():
    with db() as c:
        return {"groups":rows(c,"SELECT g.id,g.bank,g.topic,MIN(s.due_on) due_on,MAX(q.text) text FROM groups g JOIN questions q ON q.group_id=g.id JOIN schedules s ON s.question_id=q.id WHERE g.status='active' AND q.status='active' GROUP BY g.id ORDER BY due_on LIMIT 50")}


@app.get("/api/records/source-mix")
def source_mix():
    with db() as c:
        return {"counts":rows(c,"SELECT bank,COUNT(*) count FROM draws WHERE draw_kind LIKE '%new' GROUP BY bank"),"target":{"personal":0,"network":70,"ai":30}}


def redact_resume(text):
    text=re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}","[邮箱已移除]",text)
    text=re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)","[电话已移除]",text)
    text=re.sub(r"(?:姓名|住址|家庭地址|现居地|身份证号|联系电话|手机号|电话|电子邮箱|邮箱|微信号|QQ号|籍贯|出生日期|性别)\s*[:：]\s*[^，,；;\n]*","[个人信息已移除]",text)
    return text.strip()


@app.post("/api/resume/extract")
async def extract_resume(file: UploadFile=File(...)):
    name=file.filename or ""
    suffix=Path(name).suffix.lower()
    if suffix not in (".pdf",".docx"): fail("INVALID_FILE","请选择可提取文字的 PDF 或 .docx 文件")
    blob=await file.read(5_000_001)
    if len(blob)>5_000_000: fail("FILE_TOO_LARGE","文件不能超过 5 MB")
    if len(blob)<100: fail("INVALID_FILE","文件内容为空或不完整")
    try:
        if suffix==".pdf":
            from pypdf import PdfReader
            reader=PdfReader(BytesIO(blob))
            if len(reader.pages)>20: fail("TOO_MANY_PAGES","简历请控制在 20 页以内")
            text="\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            from docx import Document
            doc=Document(BytesIO(blob))
            text="\n".join(p.text for p in doc.paragraphs)
            for table in doc.tables:
                text+="\n"+"\n".join(" | ".join(cell.text for cell in row.cells) for row in table.rows)
    except HTTPException: raise
    except Exception: fail("EXTRACT_FAILED","文件无法读取，请确认格式和文件未损坏")
    if len(text.strip())<40: fail("NO_TEXT","未提取到足够文字；扫描版 PDF 暂不支持，请改用可复制文字的文件或粘贴")
    if len(text)>30000: fail("TEXT_TOO_LONG","提取文字超过 3 万字，请精简后粘贴")
    return {"text":text}


class ResumeText(BaseModel):
    text: str=Field(min_length=40,max_length=30000)


@app.post("/api/resume/prepare")
def prepare_resume(data: ResumeText):
    value=redact_resume(data.text)
    if len(value)<40: fail("NO_TEXT","移除联系方式后，简历内容不足以整理")
    return {"text_to_send":value}


@app.post("/api/resume/organize")
def organize_resume(data: ResumeText):
    value=redact_resume(data.text)
    if len(value)<40: fail("NO_TEXT","简历内容不足以整理")
    result=ai_json("只从原文提取与 Java 后端/Agent 面试有关的可证事实。不得补写经历、成果、数字或联系人。只返回 JSON：target_role 字符串、skills 字符串数组、projects 数组，每项 name、role、stack、contributions、results 字符串。没有依据的字段用空值。",value,1800)
    if not isinstance(result,dict) or not isinstance(result.get("projects",[]),list): fail("ORGANIZE_FAILED","整理结果格式不正确，请重试",503,True)
    return {"facts":result,"text_sent":value}


class ResumeFacts(BaseModel):
    facts: dict


@app.put("/api/resume/confirm")
def confirm_resume(data: ResumeFacts):
    facts=data.facts
    if not isinstance(facts.get("target_role",""),str) or not isinstance(facts.get("skills",[]),list) or not isinstance(facts.get("projects",[]),list):
        fail("INVALID_FACTS","请检查整理后的字段格式")
    if any(not isinstance(p,dict) or not isinstance(p.get("name",""),str) for p in facts["projects"]): fail("INVALID_FACTS","项目格式不正确")
    value=json.dumps(facts,ensure_ascii=False)
    if len(value)>30000: fail("FACTS_TOO_LONG","确认内容过长")
    with LOCK,db() as c:
        version=one(c,"SELECT value FROM settings WHERE key='resume_version_counter'")
        version=int(version["value"])+1 if version else 1
        c.execute("INSERT OR REPLACE INTO settings VALUES('resume_version_counter',?)",(str(version),))
        c.execute("INSERT INTO resume_profiles VALUES(?,?,?)",(version,value,now().isoformat()))
        c.execute("DELETE FROM settings WHERE key='resume_skipped'")
    return {"version":version}


@app.get("/api/resume")
def get_resume():
    with db() as c:
        profile=resume_current(c)
        return {"version":profile["version"] if profile else 0,"facts":json.loads(profile["facts"]) if profile else None,"confirmed_at":profile["confirmed_at"] if profile else None,"skipped":bool(one(c,"SELECT value FROM settings WHERE key='resume_skipped'"))}


@app.post("/api/resume/skip")
def skip_resume():
    with db() as c: c.execute("INSERT OR REPLACE INTO settings VALUES('resume_skipped','1')")
    return {"skipped":True}


@app.delete("/api/resume")
def clear_resume():
    with LOCK,db() as c:
        c.execute("DELETE FROM resume_profiles")
        c.execute("INSERT OR REPLACE INTO settings VALUES('resume_skipped','1')")
    return {"cleared":True}


@app.post("/api/questions/{question_id}/answer/regenerate")
def regenerate_answer(question_id: str):
    with LOCK,db() as c:
        a=answer(c,question_id)
        if not a: fail("NOT_FOUND","题目不存在",404)
        if a["primary_origin"] not in ("ai",""): fail("CURATED_ANSWER","这道题的原始答案不会被覆盖",409)
        group=one(c,"SELECT g.section FROM groups g JOIN questions q ON q.group_id=g.id WHERE q.id=?",(question_id,))
        if group["section"]=="项目深挖" and not resume_projects(c): fail("RESUME_REQUIRED","请先确认简历，再重新生成项目题答案",409)
        c.execute("BEGIN IMMEDIATE")
        try:
            if a["primary_text"]:
                c.execute("INSERT INTO answer_versions VALUES(?,?,?,?)",(uid(),question_id,a["primary_text"],now().isoformat()))
            c.execute("UPDATE answers SET primary_text='',primary_origin='',resume_version=0 WHERE question_id=?",(question_id,))
            result=generate_reference_answer(c,question_id)
            c.commit()
            return {"answer":result}
        except Exception:
            c.rollback()
            raise


@app.get("/api/settings")
def get_settings():
    with db() as c:
        return {"weights":json.loads(one(c,"SELECT value FROM settings WHERE key='weights'")["value"]),"model":MODEL,"deepseek_configured":bool(key_get())}


class Weights(BaseModel):
    agent: int
    java: int
    other: int


@app.put("/api/settings/topic-weights")
def set_weights(data: Weights):
    values=data.model_dump()
    if min(values.values())<0 or sum(values.values())!=100: fail("INVALID_WEIGHTS","三个方向权重合计必须为 100")
    with db() as c:c.execute("UPDATE settings SET value=? WHERE key='weights'",(json.dumps(values),))
    return {"weights":values}


@app.put("/api/settings/deepseek-key")
def set_key(data: KeyInput):
    verify_key(data.key.strip())
    key_set(data.key.strip())
    return {"configured":True}


@app.delete("/api/settings/deepseek-key")
def delete_key():
    key_delete()
    return {"configured":False}


@app.post("/api/shutdown")
def shutdown():
    # The launcher supervises this process. Closing the browser alone does not stop it.
    threading.Timer(.35,lambda:os._exit(0)).start()
    return {"ok":True}


seed_bank()
