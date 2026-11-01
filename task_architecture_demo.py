"""Task architecture evolution demo: Task, TaskService, TaskStore and adapters.

Run: python task_architecture_demo.py
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol


@dataclass
class Task:
    id: int
    title: str
    done: bool = False

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("任务标题不能为空")

    def complete(self) -> None:
        if self.done:
            raise ValueError("任务已经完成")
        self.done = True


class TaskStore(Protocol):
    def list_all(self) -> list[Task]: ...

    def save(self, task: Task) -> None: ...


class MemoryTaskStore:
    def __init__(self) -> None:
        self._items: dict[int, Task] = {}

    def list_all(self) -> list[Task]:
        return [replace(task) for task in self._items.values()]

    def save(self, task: Task) -> None:
        self._items[task.id] = replace(task)


class JsonTaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def list_all(self) -> list[Task]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [Task(**record) for record in data]

    def save(self, task: Task) -> None:
        items = {item.id: item for item in self.list_all()}
        items[task.id] = replace(task)
        self.path.write_text(
            json.dumps([asdict(item) for item in items.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class TaskService:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def create(self, title: str) -> Task:
        next_id = max((t.id for t in self.store.list_all()), default=0) + 1
        task = Task(id=next_id, title=title)
        self.store.save(task)
        return task

    def complete(self, task_id: int) -> Task:
        task = next((t for t in self.store.list_all() if t.id == task_id), None)
        if task is None:
            raise KeyError("任务不存在")
        task.complete()
        self.store.save(task)
        return task


class LegacyTaskApi:
    """模拟旧系统的实际接口形状；并非第三方库源码。"""

    def __init__(self) -> None:
        self._rows: dict[int, dict[str, object]] = {}

    def fetch_records(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows.values()]

    def put_record(self, payload: dict[str, object]) -> None:
        self._rows[int(payload["task_id"])] = dict(payload)


class LegacyTaskApiAdapter:
    def __init__(self, api: LegacyTaskApi) -> None:
        self.api = api

    def list_all(self) -> list[Task]:
        return [
            Task(id=int(row["task_id"]), title=str(row["task_name"]), done=bool(row["finished"]))
            for row in self.api.fetch_records()
        ]

    def save(self, task: Task) -> None:
        self.api.put_record({
            "task_id": task.id,
            "task_name": task.title,
            "finished": task.done,
        })


def main() -> None:
    memory = TaskService(MemoryTaskStore())
    task = memory.create(" 写博客 ")
    assert task.title == "写博客"
    assert memory.complete(task.id).done is True
    print("内存:", memory.store.list_all()[0])

    try:
        memory.create("   ")
    except ValueError as exc:
        print("校验:", exc)
    else:
        raise AssertionError("空标题未被拒绝")

    try:
        memory.complete(task.id)
    except ValueError as exc:
        print("重复完成:", exc)
    else:
        raise AssertionError("重复完成未被拒绝")

    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "tasks.json"
        first_run = TaskService(JsonTaskStore(path))
        saved = first_run.create("写博客")
        first_run.complete(saved.id)
        second_run = TaskService(JsonTaskStore(path))
        assert second_run.store.list_all()[0].done is True
        print("重新加载后:", second_run.store.list_all()[0])

    remote = TaskService(LegacyTaskApiAdapter(LegacyTaskApi()))
    created = remote.create("写博客")
    completed = remote.complete(created.id)
    assert completed.done is True
    print("旧接口适配:", remote.store.list_all()[0])


if __name__ == "__main__":
    main()
