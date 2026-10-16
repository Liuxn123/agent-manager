"""Execute one AI-sourced project task with the locally registered Codex CLI."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .domain import UserError


COMPLETION_MARKER = "AGENT_TASK_RESULT: DONE"


def build_task_prompt(project_id: str, task: dict) -> str:
    notes = str(task.get("notes", "")).strip() or "（没有额外备注）"
    return f"""你正在执行项目 {project_id} 中的一条 AI 规划任务。当前工作目录就是该项目目录。

任务：{task['title']}
任务说明：
{notes}

要求：
1. 先阅读项目内适用的 AGENTS.md、README 及相关说明，遵守其中的范围和数据保护约束。
2. 只处理上面这条任务；先检查现状，再实施所需修改，并运行与本任务相关的验证。
3. 保留无关改动和用户资料。未经任务明确要求，不删除文件、不发布、不提交或推送。
4. 不编辑 agent/TASKS-AI.md 来自行标记完成；由任务管理器在验证通过后同步状态和完成记录。
5. 若任务实际完成且已验证，最终答复须作为完成记录，按“完成内容”和“验证结果”简要写明实际修改及验证命令/结果；若无需运行命令，说明如何核验结果。随后以单独一行精确结束：{COMPLETION_MARKER}
6. 若遇到阻塞、验证失败或未能完成，不得输出完成标记；最终答复以单独一行 `AGENT_TASK_RESULT: BLOCKED` 结束并说明原因。
"""


def execute_codex_task(executable: str, project_directory: str | Path, project_id: str,
                       task: dict, context) -> dict[str, str | int]:
    """Run Codex in a workspace-write sandbox; success requires its explicit final marker."""
    requested = str(executable or "").strip()
    resolved = shutil.which(requested) if requested and not Path(requested).expanduser().is_file() else requested
    if not resolved:
        raise UserError("找不到 Codex CLI。请编辑 Agent 资源，填写可用的 Codex 可执行文件路径。")
    binary = Path(resolved).expanduser()
    if not binary.is_file():
        raise UserError("Codex 可执行文件不存在，请检查 Agent 资源中的程序路径。")
    if binary.suffix.casefold() in {".cmd", ".bat"}:
        raise UserError("AI 任务执行需要原生 Codex 可执行文件（例如 codex.exe），不支持 .cmd/.bat 启动脚本。")
    directory = Path(project_directory).resolve(strict=True)
    if not directory.is_dir():
        raise UserError("项目目录不可用，未启动 Codex。")

    command = [str(binary), "exec", "--cd", str(directory), "--sandbox", "workspace-write",
               "--ask-for-approval", "never", "-"]
    context.checkpoint()
    context.log("Codex 已在项目目录启动；文件写入范围受 workspace-write 沙箱限制。")
    try:
        result = subprocess.run(
            command,
            input=build_task_prompt(project_id, task),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(directory),
            timeout=2 * 60 * 60,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise UserError("Codex 执行超过 2 小时限制；任务保持进行中，可检查后重试。") from exc
    except OSError as exc:
        raise UserError("无法启动 Codex CLI，请检查可执行文件和登录状态。") from exc
    context.checkpoint()
    if result.returncode != 0:
        raise UserError(f"Codex 退出码为 {result.returncode}；任务保持进行中，不会标记完成。")

    final_text = (result.stdout or "").strip()
    if not final_text.splitlines() or final_text.splitlines()[-1].strip() != COMPLETION_MARKER:
        raise UserError("Codex 没有返回明确的完成标记；任务保持进行中，请检查 Agent 输出后重试。")
    summary = "\n".join(final_text.splitlines()[:-1]).strip()
    if not summary:
        raise UserError("Codex 返回完成标记但没有改动与验证摘要；任务保持进行中。")
    return {"summary": summary[-6000:], "return_code": result.returncode}
