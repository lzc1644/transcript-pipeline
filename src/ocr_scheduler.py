from __future__ import annotations

import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from threading import Event
from typing import Callable, Sequence


@dataclass(frozen=True)
class OCRPageTask:
    page_number: int


@dataclass(frozen=True)
class OCRPageTaskRunResult:
    texts_by_page: dict[int, str]
    errors_by_page: dict[int, Exception]
    interrupted: bool = False

    def ordered_texts(self) -> list[str]:
        return [self.texts_by_page[page_number] for page_number in sorted(self.texts_by_page)]


def run_staggered_page_ocr_tasks(
    tasks: Sequence[OCRPageTask],
    worker: Callable[[OCRPageTask], str],
    *,
    max_concurrency: int,
    submit_interval_seconds: float,
    logger: logging.Logger | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_dispatched: Callable[[OCRPageTask, float], None] | None = None,
    on_succeeded: Callable[[OCRPageTask, str], None] | None = None,
    on_failed: Callable[[OCRPageTask, Exception], None] | None = None,
    stop_event: Event | None = None,
) -> OCRPageTaskRunResult:
    """连续错峰执行全部单页 OCR，分别保留成功结果与失败页。"""

    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须是正整数")
    if submit_interval_seconds < 0:
        raise ValueError("submit_interval_seconds 不能小于 0")
    if not tasks:
        return OCRPageTaskRunResult(texts_by_page={}, errors_by_page={})

    ordered_tasks = sorted(tasks, key=lambda task: task.page_number)
    if len({task.page_number for task in ordered_tasks}) != len(ordered_tasks):
        raise ValueError("OCR 页任务的页码不能重复")
    task_positions = {task.page_number: index for index, task in enumerate(ordered_tasks, start=1)}
    results: dict[int, str] = {}
    errors: dict[int, Exception] = {}

    next_task_index = 0
    next_submit_at = monotonic()
    active: dict[Future[str], OCRPageTask] = {}

    with ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="pdf-ocr") as executor:
        while next_task_index < len(ordered_tasks) or active:
            completed = [future for future in active if future.done()]
            for future in completed:
                task = active.pop(future)
                try:
                    page_text = future.result()
                    if on_succeeded:
                        on_succeeded(task, page_text)
                except Exception as exc:
                    errors[task.page_number] = exc
                    if on_failed:
                        on_failed(task, exc)
                    if logger:
                        logger.error(
                            "OCR 页任务失败 | page=%s | batch=%s/%s | %s",
                            task.page_number,
                            task_positions[task.page_number],
                            len(ordered_tasks),
                            exc,
                        )
                else:
                    results[task.page_number] = page_text
                    if logger:
                        logger.info(
                            "OCR 页任务完成 | page=%s | batch=%s/%s | active=%s",
                            task.page_number,
                            task_positions[task.page_number],
                            len(ordered_tasks),
                            len(active),
                        )

            has_capacity = len(active) < max_concurrency
            stopped = stop_event is not None and stop_event.is_set()
            if next_task_index < len(ordered_tasks) and has_capacity and not stopped:
                delay = next_submit_at - monotonic()
                if delay > 0:
                    if stop_event is None:
                        sleep(delay)
                    else:
                        stop_event.wait(delay)
                    continue

                if stop_event is not None and stop_event.is_set():
                    continue
                task = ordered_tasks[next_task_index]
                submitted_at = monotonic()
                active[executor.submit(worker, task)] = task
                next_task_index += 1
                next_submit_at = submitted_at + submit_interval_seconds
                if on_dispatched:
                    on_dispatched(task, submitted_at)
                if logger:
                    logger.info(
                        "OCR 页任务已投递 | page=%s | batch=%s/%s | active=%s",
                        task.page_number,
                        task_positions[task.page_number],
                        len(ordered_tasks),
                        len(active),
                    )
                continue

            if active:
                wait(active, return_when=FIRST_COMPLETED)
            elif stopped:
                break

    if len(results) + len(errors) != next_task_index:
        raise RuntimeError("OCR 页任务结束后仍存在缺失结果")
    return OCRPageTaskRunResult(texts_by_page=results, errors_by_page=errors,
                                interrupted=next_task_index < len(ordered_tasks))
