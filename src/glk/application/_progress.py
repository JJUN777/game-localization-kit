"""Keep progress callback failures distinct from source-item failures."""

from __future__ import annotations

from collections.abc import Callable


ProgressCallback = Callable[[str], None]


class ProgressCallbackError(RuntimeError):
    """Raised when an observer cannot accept a progress notification."""


def guard_progress_callback(
    callback: ProgressCallback | None,
) -> ProgressCallback:
    if callback is None:
        return lambda _: None

    def notify(message: str) -> None:
        try:
            callback(message)
        except Exception as error:
            raise ProgressCallbackError(
                f"Progress callback failed: {error}"
            ) from error

    return notify
