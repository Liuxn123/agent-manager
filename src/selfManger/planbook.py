"""Longer-range planning records stored as Obsidian-compatible Markdown."""
from __future__ import annotations

import uuid
from datetime import date, timedelta

from .domain import UserError
from .workbench import MarkdownFiles, frontmatter, render_document

KINDS = {
    "plan": "长期任务",
    "goal": "长期任务",
    "recurring": "周期任务",
    "template": "计划模板",
    "review": "计划复盘",
}
FOLDERS = {
    "plan": "安排",
    "goal": "目标",
    "recurring": "周期任务",
    "template": "模板",
    "review": "复盘",
}
STATUSES = {"planned", "active", "done", "archived"}
FREQUENCIES = {"daily", "weekly", "monthly"}


class PlanBook:
    """Compare-before-write Markdown records; reads never create plan folders."""

    def __init__(self, root):
        self.files = MarkdownFiles(root)

    def scan(self, kind=None, context=None):
        if kind is not None and kind not in KINDS:
            raise UserError("计划类型无效。")
        kinds = [kind] if kind else list(KINDS)
        items, errors = [], []
        for entry_kind in kinds:
            directory = self.files.path("计划/" + FOLDERS[entry_kind])
            if not directory.exists():
                continue
            try:
                paths = sorted(directory.glob("*.md"), key=lambda path: path.name.casefold())
            except OSError as exc:
                errors.append(FOLDERS[entry_kind] + "：" + str(exc))
                continue
            for index, path in enumerate(paths):
                if context:
                    context.checkpoint()
                if index >= 1000:
                    errors.append(FOLDERS[entry_kind] + "超过 1000 个文件，请整理后再查看。")
                    break
                relative = "计划/" + FOLDERS[entry_kind] + "/" + path.name
                try:
                    safe = self.files.path(relative)
                    text = self.files.read(relative)
                    if text is None:
                        continue
                    metadata, body = frontmatter(text)
                    if metadata.get("type") != entry_kind:
                        errors.append(path.name + "：计划类型与目录不匹配。")
                        continue
                    title = metadata.get("title")
                    identity = metadata.get("id")
                    if not isinstance(title, str) or not title.strip() or not isinstance(identity, str):
                        errors.append(path.name + "：缺少有效标题或编号。")
                        continue
                    items.append({"id": identity, "type": entry_kind, "title": title,
                                  "metadata": metadata, "body": body, "text": text,
                                  "relative": relative, "path": str(safe)})
                except (UserError, OSError) as exc:
                    errors.append(path.name + "：" + str(exc))
        items.sort(key=lambda item: (item["metadata"].get("due") or "9999-99-99",
                                     item["title"].casefold()))
        return {"items": items, "errors": errors}

    def save(self, kind, values, body, previous=None):
        if kind not in KINDS:
            raise UserError("计划类型无效。")
        title = str(values.get("title", "")).strip()
        if not title or len(title) > 160 or "\n" in title or "\r" in title:
            raise UserError("请填写 1–160 字的一行标题。")
        status = values.get("status", "planned")
        if status not in STATUSES:
            raise UserError("计划状态无效。")
        start = self._date(values.get("start", ""), "开始日期")
        due = self._date(values.get("due", ""), "目标日期")
        if start and due and start > due:
            raise UserError("开始日期不能晚于目标日期。")
        if kind == "plan" and due is None:
            raise UserError("长期任务必须填写截止日期。")
        frequency = values.get("frequency", "")
        if kind == "recurring" and frequency not in FREQUENCIES:
            raise UserError("周期任务请选择每天、每周或每月。")
        if kind == "recurring" and not start:
            raise UserError("周期任务需要开始日期。")
        weekdays = values.get("weekdays")
        if weekdays is None and previous:
            weekdays = previous["metadata"].get("weekdays")
        if kind == "recurring" and frequency == "weekly":
            if not weekdays and previous and start:
                # Older weekly records repeated on the start date's weekday.
                weekdays = [start.isoweekday()]
            if not isinstance(weekdays, (list, tuple, set)):
                raise UserError("每周任务请选择至少一个重复星期。")
            try:
                weekdays = sorted({int(value) for value in weekdays})
            except (TypeError, ValueError) as exc:
                raise UserError("重复星期格式错误，请重新选择星期。") from exc
            if not weekdays or any(value not in range(1, 8) for value in weekdays):
                raise UserError("每周任务请选择至少一个重复星期。")
        else:
            weekdays = []
        if kind != "recurring":
            frequency = ""
        try:
            progress = int(values.get("progress", previous["metadata"].get("progress", 0) if previous else 0) or 0)
        except (TypeError, ValueError) as exc:
            raise UserError("目标进度需要是 0–100 的整数。") from exc
        if not 0 <= progress <= 100:
            raise UserError("目标进度需要在 0–100 之间。")

        previous_metadata, expected = {}, None
        if previous:
            if previous.get("type") != kind:
                raise UserError("不能在编辑时改变计划类型。")
            previous_metadata = previous["metadata"]
            expected = previous["text"]
            relative = previous["relative"]
            identity = previous["id"]
        else:
            title_key = title.casefold()
            if any(item["title"].casefold() == title_key
                   and item["metadata"].get("start", "") == (start.isoformat() if start else "")
                   and item["metadata"].get("due", "") == (due.isoformat() if due else "")
                   and item["metadata"].get("project_id", "") == str(values.get("project_id", "")).strip()
                   for item in self.scan(kind)["items"]):
                raise UserError("已有相同名称、日期和项目的计划，请编辑原记录或调整日期。")
            identity = uuid.uuid4().hex
            relative = "计划/" + FOLDERS[kind] + "/" + identity + ".md"
        metadata = {**previous_metadata, "id": identity, "type": kind, "title": title,
                    "status": status, "start": start.isoformat() if start else "",
                    "due": due.isoformat() if due else "",
                    "project_id": str(values.get("project_id", "")).strip(),
                    "parent_id": str(values.get("parent_id", "")).strip(),
                    "frequency": frequency,
                    "progress": progress if kind == "goal" else previous_metadata.get("progress", 0),
                    "created": previous_metadata.get("created") or date.today().isoformat(),
                    "updated": date.today().isoformat()}
        metadata.pop("weekdays", None)
        if kind == "recurring" and frequency == "weekly":
            metadata["weekdays"] = weekdays
        if kind == "review":
            metadata["summary_period"] = values.get("summary_period") or previous_metadata.get("summary_period", "weekly")
        return self.files.write(relative, render_document(metadata, body), expected)

    def delete(self, item):
        """Delete one unchanged planning Markdown record after validating its identity."""
        kind = item.get("type")
        identity = item.get("id")
        relative = item.get("relative")
        expected = item.get("text")
        if kind not in KINDS or not isinstance(identity, str) or not identity or expected is None:
            raise UserError("计划记录信息无效，请刷新后重试。")
        if relative != "计划/" + FOLDERS[kind] + "/" + identity + ".md":
            raise UserError("计划文件路径与记录编号不匹配，已取消删除。")
        with self.files.lock():
            path = self.files.path(relative)
            current = self.files.read(relative)
            if current != expected:
                raise UserError("计划文件已被 Obsidian 或其他程序修改，请刷新后再删除；原文件未删除。")
            metadata, _ = frontmatter(current)
            if metadata.get("id") != identity or metadata.get("type") != kind:
                raise UserError("计划文件身份已变化，已取消删除。")
            path.unlink()
        return {"path": str(path)}

    def set_occurrence_done(self, item, day, done):
        """Persist one recurring-task occurrence on its plan record, never in Daily Markdown."""
        if item.get("type") != "recurring":
            raise UserError("只有周期任务支持按日期记录完成状态。")
        day_text = day.isoformat() if isinstance(day, date) else date.fromisoformat(str(day)).isoformat()
        metadata = dict(item["metadata"])
        completed = metadata.get("completed_dates", [])
        if not isinstance(completed, list) or any(not isinstance(value, str) for value in completed):
            raise UserError("周期任务的完成日期记录格式错误，请先在计划 Markdown 中修正。")
        dates = set(completed)
        if done:
            dates.add(day_text)
        else:
            dates.discard(day_text)
        if len(dates) > 5000:
            raise UserError("周期任务完成记录超过 5000 天，请归档旧记录后再操作。")
        if dates:
            metadata["completed_dates"] = sorted(dates)
        else:
            metadata.pop("completed_dates", None)
        return self.files.write(item["relative"], render_document(metadata, item["body"]), item["text"])

    @staticmethod
    def _date(value, label):
        if not value:
            return None
        try:
            parsed = date.fromisoformat(str(value))
        except ValueError as exc:
            raise UserError(label + "请使用 YYYY-MM-DD 格式。") from exc
        return parsed


def occurs_on(item, day):
    """Project a recurring rule onto one date without writing an occurrence."""
    metadata = item["metadata"]
    if item["type"] != "recurring" or metadata.get("status", "planned") in {"done", "archived"}:
        return False
    try:
        start = date.fromisoformat(metadata.get("start", ""))
        end = date.fromisoformat(metadata["due"]) if metadata.get("due") else None
    except (TypeError, ValueError):
        return False
    if day < start or (end and day > end):
        return False
    frequency = metadata.get("frequency")
    if frequency == "daily":
        return True
    if frequency == "weekly":
        weekdays = metadata.get("weekdays")
        if not isinstance(weekdays, (list, tuple, set)) or not weekdays:
            # Keep legacy weekly records working until they are edited and saved.
            weekdays = [start.isoweekday()]
        try:
            selected = {int(value) for value in weekdays}
        except (TypeError, ValueError):
            return False
        return day.isoweekday() in selected
    if frequency == "monthly":
        # A month-end rule stays on month-end; other rules use their original day number.
        month_end = (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        start_month_end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        return day.day == (month_end.day if start.day == start_month_end.day else start.day)
    return False
