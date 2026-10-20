"""Human-readable resume facts, including conversion of older JSON records."""
import json
import re


def from_legacy(value):
    """Convert the old structured facts without changing their meaning."""
    if not isinstance(value, dict):
        return str(value or "").strip()
    lines = []
    if value.get("target_role"):
        lines.append(f"目标岗位：{value['target_role']}")
    skills = value.get("skills") or []
    if skills:
        lines.append("技能：" + "、".join(str(skill) for skill in skills))
    for project in value.get("projects") or []:
        if not isinstance(project, dict):
            continue
        if lines:
            lines.append("")
        labels = (("name", "项目"), ("role", "角色"), ("stack", "技术栈"),
                  ("contributions", "本人贡献"), ("results", "结果"))
        lines.extend(f"{label}：{project[key]}" for key, label in labels if project.get(key))
        for key, item in project.items():
            if key not in dict(labels) and item:
                lines.append(f"{key}：{item}")
    for key, item in value.items():
        if key not in ("target_role", "skills", "projects") and item:
            lines.append(f"{key}：{item}")
    return "\n".join(lines).strip()


def normalize(value):
    """Keep ordinary text; convert only legacy JSON objects."""
    if not isinstance(value, str):
        raise ValueError("简历事实必须是文字")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return value.strip()
    return from_legacy(parsed) if isinstance(parsed, dict) else value.strip()


def projects(value):
    text = normalize(value)
    matches = list(re.finditer(r"(?m)^\s*项目(?:名称)?[：:]\s*(.+?)\s*$", text))
    return [{"name": match.group(1).strip(),
             "details": text[match.start():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip()}
            for index, match in enumerate(matches) if match.group(1).strip()]
