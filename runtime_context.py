"""单次运行的模型策略与绝对截止时间。"""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

import config


@dataclass(frozen=True)
class RunSettings:
    model: str
    reasoning: str
    deadline_epoch: Optional[float] = None

    def __post_init__(self):
        model, reasoning = config.validate_model_reasoning(
            self.model,
            self.reasoning,
        )
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "reasoning", reasoning)
        if self.deadline_epoch is not None:
            object.__setattr__(self, "deadline_epoch", float(self.deadline_epoch))


_RUN_SETTINGS = ContextVar("run_settings", default=None)


def current_settings():
    """返回当前运行设置；未绑定时使用环境配置。"""
    settings = _RUN_SETTINGS.get()
    if settings is not None:
        return settings
    return RunSettings(config.MODEL_NAME, config.REASONING_EFFORT)


def remaining_seconds(limit=110):
    """返回当前运行在 limit 内的剩余墙钟秒数。"""
    remaining = max(0.0, float(limit))
    deadline_epoch = current_settings().deadline_epoch
    if deadline_epoch is not None:
        remaining = min(remaining, max(0.0, deadline_epoch - time.time()))
    return remaining


def monotonic_deadline(limit=110):
    """将当前运行的 epoch 截止时间换算为单调时钟截止点。"""
    return time.monotonic() + remaining_seconds(limit=limit)


@contextmanager
def use_run_settings(settings):
    """在当前上下文临时绑定运行设置，支持安全嵌套。"""
    if not isinstance(settings, RunSettings):
        raise TypeError("settings 必须是 RunSettings")
    token = _RUN_SETTINGS.set(settings)
    try:
        yield settings
    finally:
        _RUN_SETTINGS.reset(token)
