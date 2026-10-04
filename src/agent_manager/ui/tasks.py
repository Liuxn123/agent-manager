from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..domain import Cancelled, Operation, RestorePlan, UserError
from ..runtime import TaskContext
from ..security import safe_result
from ..storage import Store


class TaskSignals(QObject):
    finished = Signal(str, object, str)


class Worker(QRunnable):
    def __init__(self, identity: str, store: Store, operation: Operation) -> None:
        super().__init__()
        self.identity, self.store, self.operation = identity, store, operation
        self.signals = TaskSignals()
        self.context = TaskContext(lambda message: store.append_log(identity, message))

    @Slot()
    def run(self) -> None:
        outcome = None
        state = "failed"
        try:
            self.context.log("任务已开始。")
            outcome = self.operation(self.context)
            report = outcome.summary if isinstance(outcome, RestorePlan) else outcome
            state = "success"
            self.store.finish_task(self.identity, state, safe_result(report))
            self.context.log("任务完成。")
        except Cancelled as exc:
            state = "cancelled"
            report = {"error": str(exc)}
            self.store.finish_task(self.identity, state, report)
            self.context.log(str(exc))
            outcome = report
        except UserError as exc:
            report = {"error": str(exc)}
            self.store.finish_task(self.identity, state, safe_result(report))
            self.context.log(str(exc))
            outcome = report
        except Exception as exc:
            # Never persist arbitrary exception values from external libraries.
            report = {"error": "操作未完成，请检查配置、文件权限和依赖。", "error_type": type(exc).__name__}
            self.store.finish_task(self.identity, state, report)
            self.context.log(report["error"] + "（" + report["error_type"] + "）")
            outcome = report
        finally:
            self.signals.finished.emit(self.identity, outcome, state)
