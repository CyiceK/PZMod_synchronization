from __future__ import annotations

from PyQt6.QtWidgets import QSizePolicy, QWidget


def clamp_button_width(button: QWidget, max_width: int) -> None:
    """Clamp button width to its size hint with an upper bound."""
    size_hint = button.sizeHint()
    target_width = min(size_hint.width(), max_width)
    button.setMinimumWidth(target_width)
    button.setMaximumWidth(max_width)
    policy = button.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
    button.setSizePolicy(policy)
