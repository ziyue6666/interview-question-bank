"""Local question normalization and classification. No external document connections."""
import hashlib
import re


def normalized(value):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", value.lower())


def fingerprint(value):
    return hashlib.sha256(normalized(value).encode()).hexdigest()


def topic_for(text):
    value = text.lower()
    if re.search(r"agent|skill|大模型|rag|检索|prompt|tool|mcp|评测|向量|embedding|llm", value):
        return "agent"
    if re.search(r"java|spring|mysql|redis|kafka|线程|并发|锁|事务|索引|jvm|gc|数据库|消息|集合|缓存|http", value):
        return "java"
    return "other"
