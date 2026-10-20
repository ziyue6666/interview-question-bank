"""Portable, versioned data backup. API credentials never enter this format."""
import json
import sqlite3
import resume_facts

TABLES = (
    "groups", "questions", "answers", "deliveries", "events", "schedules",
    "draws", "settings", "import_jobs", "candidates", "answer_messages",
    "resume_profiles", "answer_versions",
)
PRIVATE_SETTING_PARTS = ("key", "password", "secret", "token", "credential")
MAX_ROWS = 500_000


def columns(connection, table):
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def export_connection(connection):
    data = {}
    for table in TABLES:
        names = columns(connection, table)
        if not names:
            data[table] = []
            continue
        records = [dict(zip(names, row)) for row in connection.execute(f"SELECT * FROM {table}")]
        if table == "settings":
            records = [r for r in records if not any(part in r["key"].lower() for part in PRIVATE_SETTING_PARTS)]
        data[table] = records
    return {"format": "interview-question-bank", "version": 1, "tables": data}


def restore_connection(target, payload):
    """Restore into a disposable copy of the current schema; caller swaps it after verification."""
    if not isinstance(payload, dict) or payload.get("format") != "interview-question-bank" or payload.get("version") != 1:
        raise ValueError("备份格式或版本不受支持")
    tables = payload.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError("备份缺少数据表或包含未知数据表")
    total = 0
    for table in TABLES:
        records = tables[table]
        if not isinstance(records, list):
            raise ValueError(f"{table} 数据格式不正确")
        total += len(records)
        if total > MAX_ROWS:
            raise ValueError("备份记录数量超出限制")
        allowed = set(columns(target, table))
        if not allowed:
            raise ValueError(f"目标数据库缺少 {table} 表")
        for record in records:
            if not isinstance(record, dict) or not set(record).issubset(allowed):
                raise ValueError(f"{table} 包含不支持的字段")
            if any(not isinstance(value, (str, int, float, type(None))) for value in record.values()):
                raise ValueError(f"{table} 包含无效字段值")
            if table == "settings" and any(part in str(record.get("key", "")).lower() for part in PRIVATE_SETTING_PARTS):
                raise ValueError("备份不得包含凭据设置")
    target.execute("PRAGMA foreign_keys=OFF")
    try:
        target.execute("BEGIN IMMEDIATE")
        for table in reversed(TABLES):
            target.execute(f"DELETE FROM {table}")
        for table in TABLES:
            for record in tables[table]:
                names = list(record)
                if not names:
                    raise ValueError(f"{table} 存在空记录")
                placeholders = ",".join("?" for _ in names)
                target.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({placeholders})", [record[n] for n in names])
        if target.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份数据的题目或答案关联不完整")
        for table, column, parent in (
            ("deliveries", "question_id", "questions"),
            ("events", "question_id", "questions"),
            ("events", "delivery_id", "deliveries"),
            ("schedules", "question_id", "questions"),
            ("draws", "group_id", "groups"),
            ("candidates", "job_id", "import_jobs"),
        ):
            orphan = target.execute(f"SELECT 1 FROM {table} x WHERE x.{column} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {parent} y WHERE y.id=x.{column}) LIMIT 1").fetchone()
            if orphan:
                raise ValueError(f"{table} 数据关联不完整")
        for name, keys, total_required in (
            ("source_weights", ("personal", "network", "ai"), True),
            ("ai_weights", ("fundamentals", "backend_scenario", "agent_fundamentals", "agent_scenario", "project"), False),
        ):
            row=target.execute("SELECT value FROM settings WHERE key=?",(name,)).fetchone()
            if row:
                try:
                    weights=json.loads(row[0])
                    valid=isinstance(weights,dict) and set(weights)==set(keys) and all(type(v)==int and 0<=v<=100 for v in weights.values()) and (sum(weights.values())==100 if total_required else sum(weights.values())>0)
                except (ValueError,TypeError):
                    valid=False
                if not valid:
                    raise ValueError(f"{name} 设置无效")
        for (facts,) in target.execute("SELECT facts FROM resume_profiles"):
            if not isinstance(facts,str) or not facts.strip() or len(facts)>30000:
                raise ValueError("简历事实格式不正确")
        for version, facts in target.execute("SELECT version,facts FROM resume_profiles").fetchall():
            plain=resume_facts.normalize(facts)
            if plain != facts:
                target.execute("UPDATE resume_profiles SET facts=? WHERE version=?",(plain,version))
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据未通过完整性检查")
        target.commit()
    except Exception:
        target.rollback()
        raise
    finally:
        target.execute("PRAGMA foreign_keys=ON")


def dumps(connection):
    return json.dumps(export_connection(connection), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
