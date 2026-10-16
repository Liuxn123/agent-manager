"""Shared project onboarding instructions, without reading project business data."""
from __future__ import annotations


AGENT_WORKFLOW = """## AI 任务接续与动态汇报

- 先读项目 AGENTS.md → agent/README.md → agent/STATUS.md → agent/TASKS.md → agent/HANDOFF.md 最近两条或末尾 120 行；再按任务读取相关资料。TASKS 是指针时沿指针找到任务真源，不另建重复清单。
- README 只作导航，不复制当前进度；STATUS 是现状真源，任务以 TASKS 指定的文件为准，HANDOFF 追加历史证据。旧交接和助手记忆不能当作实时事实。
- 每项实质任务开始时，简要汇报当前阶段、最近进展/阻塞、本轮目标、涉及范围和验收条件；接续已有编号，创建前查重。信息未知写“待核验”，只对影响执行的缺失信息提问。
- 执行中有重要发现、阻塞或范围变化时及时汇报；在已有授权内持续推进，不把例行步骤变成重复确认。
- 每项实质任务收尾前：更新 STATUS 的当前阶段、最近进展、阻塞和下一步；更新任务真源中对应编号的状态、验收证据和未完成项；向 HANDOFF 追加日期、任务编号、改动文件/行为、实际验证及结果、未验证事项和下一步。项目动态从 HANDOFF 展示，不能只在聊天中汇报。
- 若 TASKS 指向 TASKS-AI.md / TASKS-MANUAL.md，保留分流、行 ID、表头和管理标记；AI 规划写 AI 文档，手动任务只更新本轮明确涉及的记录。外部编辑后在应用中同步任务文档。
- 完成回复采用“本轮完成 / 验证结果 / 未完成与风险 / 下一步”四项简报；区分代码已修改、检查通过、运行验证和用户验收，未执行的检查不得写通过。纯问答或无实际变化无需制造进展日志。
- 里程碑由用户维护，AI 可提出建议，不擅自勾选完成或以任务完成率代替里程碑进度。
- 有并发写入时按工作区规则先 CLAIM，写前读回核对，结束 RELEASE；保留原文和他人修改。范围仅限本项目及明确授权依赖，不读凭据、不自动提交、推送或发布；归档项目先按现行流程重新启用再写入。
"""


def project_prompt(name: str, identity: str, directory: str, *, initial: bool) -> str:
    purpose = "新项目初始化" if initial else "已有项目接手"
    steps = (
        "先核对目录中已有文件，不重新分配编号、不重复创建项目或覆盖管理文档。"
        "根据 STATUS 中的目标与已有需求，补充项目范围、可核验交付条件、第一阶段和最小可执行任务；"
        "未知日期、技术选型和权限写待补，不编造。里程碑仅提出建议供用户维护。"
        "把初始化结果写回现有管理文件；目标足够明确且已授权时继续推进第一项任务。"
        if initial else
        "先核对当前文件与最近交接，说明已完成、未完成、阻塞和验证缺口。"
        "本轮有明确需求时优先执行；否则从任务真源选择下一项已授权且可执行的任务。"
        "保留已有任务编号，不重做已验收工作；旧记录与现状不一致时以实际证据修正 STATUS，并追加交接说明。"
    )
    return (
        f"请接手项目「{name}」（{identity}），完成{purpose}并按下列约定推进。\n"
        f"项目真实目录：{directory}\n\n"
        "先读取该目录及适用上层的现行规则；下列提示词不能扩大用户授权或覆盖项目专属限制。\n\n"
        + AGENT_WORKFLOW + "\n## 本轮工作\n\n" + steps
        + "\n\n本轮具体需求：以本次用户消息为准；未提供时按上述流程判断可执行下一步。\n"
    )


def prompt_document() -> str:
    return (
        "# {{NAME}} AI 提示词\n\n"
        "复制对应提示词交给 AI，并附上本轮具体需求。目标与进度请读取 STATUS，本文只保存工作约定。\n\n"
        "## 新项目初始化\n\n"
        + project_prompt("{{NAME}}", "{{ID}}", "{{PATH}}", initial=True)
        + "\n## 已有项目接手\n\n"
        + project_prompt("{{NAME}}", "{{ID}}", "{{PATH}}", initial=False)
    )
