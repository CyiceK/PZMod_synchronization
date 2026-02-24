"""
Server form panel - handles form field creation, validation and management.
"""
from typing import Dict, Any, List

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QGroupBox,
    QLineEdit,
)
from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QIntValidator, QDoubleValidator

from qfluentwidgets import (
    BodyLabel, CaptionLabel, TextEdit, CheckBox,
)
from services.i18n import tr


class ServerFormPanel:
    """Form panel for editing server config fields."""

    def __init__(self, parent: QWidget):
        self._parent = parent
        self._form_fields: Dict[str, Dict[str, Any]] = {}
        self._comment_map: Dict[str, str] = {}
        self._extra_fields: List[Dict[str, Any]] = []
        self._extra_field_keys: List[str] = []
        self._form_hint_default = ""
        self._form_layout: QVBoxLayout = None
        self._form_panel: QWidget = None
        self._form_header: BodyLabel = None
        self._form_hint: CaptionLabel = None
        self._base_form_keys: set = set()

    @property
    def form_panel(self) -> QWidget:
        return self._form_panel

    @property
    def form_layout(self) -> QVBoxLayout:
        return self._form_layout

    @property
    def form_hint(self) -> CaptionLabel:
        return self._form_hint

    @property
    def form_fields(self) -> Dict[str, Dict[str, Any]]:
        return self._form_fields

    @property
    def comment_map(self) -> Dict[str, str]:
        return self._comment_map

    @comment_map.setter
    def comment_map(self, value: Dict[str, str]):
        self._comment_map = value

    def get_base_form_keys(self) -> set:
        return self._base_form_keys.copy()

    def init_ui(self, layout: QVBoxLayout):
        """Initialize form UI."""
        self._form_panel = QWidget(self._parent)
        self._form_layout = QVBoxLayout(self._form_panel)
        self._form_layout.setContentsMargins(0, 0, 0, 0)
        self._form_layout.setSpacing(12)

        self._form_header = BodyLabel(tr("server.editor.form.title"))
        self._form_hint_default = tr("server.editor.form.hint")
        self._form_hint = CaptionLabel(self._form_hint_default)
        self._form_hint.setWordWrap(True)
        self._form_layout.addWidget(self._form_header)
        self._form_layout.addWidget(self._form_hint)

    def build_form_groups(self, schema: List[Dict[str, Any]], base_keys: set) -> None:
        """Build form groups from schema."""
        self._base_form_keys = base_keys
        self._form_fields.clear()
        for group in schema:
            group_box = QGroupBox(tr(group["title"]))
            group_layout = QFormLayout(group_box)
            group_layout.setContentsMargins(12, 12, 12, 12)
            group_layout.setHorizontalSpacing(12)
            group_layout.setVerticalSpacing(8)
            for field in group["fields"]:
                key = field["key"]
                if field.get("label_literal"):
                    label_key = f"server.editor.field.{key}"
                    translated = tr(label_key)
                    if translated == label_key or translated == key:
                        label_text = key
                    else:
                        label_text = f"{translated} ({key})"
                else:
                    translated = tr(field["label"])
                    if translated == field["label"] or translated == key:
                        label_text = key
                    else:
                        label_text = f"{translated} ({key})"
                label = BodyLabel(label_text)
                widget = self._create_form_widget(key, field)
                desc_label = CaptionLabel("")
                desc_label.setWordWrap(True)
                desc_label.setProperty("formDesc", True)
                desc_label.hide()

                field_container = QWidget(self._parent)
                field_layout = QVBoxLayout(field_container)
                field_layout.setContentsMargins(0, 0, 0, 0)
                field_layout.setSpacing(4)
                field_layout.addWidget(widget)
                field_layout.addWidget(desc_label)

                group_layout.addRow(label, field_container)
                self._form_fields[key] = {
                    "widget": widget,
                    "type": field["type"],
                    "label": label,
                    "desc_label": desc_label,
                }
                self._apply_field_comment(key, label, widget, desc_label)
            self._form_layout.addWidget(group_box)

    def rebuild_form_groups(self) -> None:
        """Clear and rebuild form groups - placeholder for external call."""
        pass

    def _create_form_widget(self, key: str, field: Dict[str, Any]):
        """Create form widget based on field type."""
        field_type = field.get("type", "text")
        placeholder_key = field.get("placeholder", "")
        if field_type == "bool":
            widget = CheckBox("")
            return widget
        if field_type == "multiline":
            widget = TextEdit(self._parent)
            widget.setFixedHeight(90)
            if placeholder_key:
                widget.setPlaceholderText(tr(placeholder_key))
            return widget
        widget = QLineEdit(self._parent)
        if placeholder_key:
            widget.setPlaceholderText(tr(placeholder_key))
        if field_type == "int":
            widget.setValidator(QIntValidator(0, 10**9, widget))
        elif field_type == "float":
            validator = QDoubleValidator(widget)
            validator.setDecimals(3)
            widget.setValidator(validator)
        return widget

    def _normalize_comment_text(self, text: str) -> str:
        """Normalize comment text."""
        text = text.replace("\\n", "\n")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)

    def _build_comment_map(self, content: str) -> Dict[str, str]:
        """Build comment map from INI content."""
        comment_map: Dict[str, str] = {}
        comment_lines: List[str] = []
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                comment_lines = []
                continue
            if stripped.startswith("#") or stripped.startswith(";"):
                text = stripped.lstrip("#;").strip()
                if text:
                    comment_lines.append(text)
                continue
            if "=" not in stripped:
                comment_lines = []
                continue
            key = stripped.split("=", 1)[0].strip()
            if comment_lines:
                comment_map[key] = self._normalize_comment_text("\n".join(comment_lines))
            comment_lines = []
        return comment_map

    def update_comment_map(self, content: str) -> bool:
        """Update comment map and return if changed."""
        new_map = self._build_comment_map(content)
        if new_map == self._comment_map:
            return False
        self._comment_map = new_map
        return True

    def _apply_field_comment(self, key: str, label, widget, desc_label=None) -> None:
        """Apply comment to field."""
        comment = self._comment_map.get(key, "")
        tooltip = comment.strip()
        if label is not None:
            label.setToolTip(tooltip)
        if widget is not None:
            widget.setToolTip(tooltip)
            if tooltip:
                widget.setProperty("formHintKey", key)
                if not widget.property("formHintBound"):
                    widget.installEventFilter(self._parent)
                    widget.setProperty("formHintBound", True)
            else:
                widget.setProperty("formHintKey", "")
        if desc_label is not None:
            if tooltip:
                desc_label.setText(tooltip)
                desc_label.show()
            else:
                desc_label.setText("")
                desc_label.hide()

    def apply_field_comments(self) -> None:
        """Apply comments to all fields."""
        for key, meta in self._form_fields.items():
            self._apply_field_comment(
                key,
                meta.get("label"),
                meta.get("widget"),
                meta.get("desc_label"),
            )

    def set_form_hint_text(self, text: str) -> None:
        """Set form hint text."""
        self._form_hint.setText(text if text else self._form_hint_default)

    def update_form_hint(self, key: str) -> None:
        """Update hint for specific field."""
        comment = self._comment_map.get(key, "")
        self.set_form_hint_text(comment or self._form_hint_default)

    def reset_form_hint(self) -> None:
        """Reset form hint based on focused widget."""
        from PyQt6.QtWidgets import QApplication
        focus_widget = QApplication.focusWidget()
        if focus_widget and focus_widget.property("formHintKey"):
            key = focus_widget.property("formHintKey")
            if key:
                self.update_form_hint(key)
                return
        self.set_form_hint_text(self._form_hint_default)

    def detect_field_type(self, value: str) -> str:
        """Detect field type from value."""
        lowered = value.lower()
        if lowered in {"true", "false", "yes", "no", "1", "0"}:
            return "bool"
        if ";" in value:
            return "multiline"
        try:
            int(value)
            return "int"
        except ValueError:
            pass
        try:
            float(value)
            return "float"
        except ValueError:
            return "text"

    def refresh_extra_fields(self, data: Dict[str, str]) -> bool:
        """Refresh extra fields based on data."""
        base_keys = self._base_form_keys
        extra_keys = sorted([k for k in data.keys() if k not in base_keys])
        if extra_keys == self._extra_field_keys:
            return False
        extra_fields: List[Dict[str, Any]] = []
        for key in extra_keys:
            value = data.get(key, "")
            extra_fields.append({
                "key": key,
                "label": key,
                "label_literal": True,
                "type": self.detect_field_type(value),
            })
        self._extra_fields = extra_fields
        self._extra_field_keys = extra_keys
        return True

    def normalize_form_value(self, field_type: str, value: str) -> str:
        """Normalize form value."""
        if field_type == "bool":
            return "true" if value.lower() in {"true", "1", "yes", "y"} else "false"
        return value

    def get_form_value(self, key: str) -> str:
        """Get value from form field."""
        field = self._form_fields.get(key)
        if not field:
            return ""
        widget = field["widget"]
        field_type = field["type"]
        if field_type == "bool":
            return "true" if widget.isChecked() else "false"
        if field_type == "multiline":
            lines = [line.strip() for line in widget.toPlainText().splitlines() if line.strip()]
            return ";".join(lines)
        return widget.text().strip()

    def set_form_value(self, key: str, value: str, field_type: str) -> None:
        """Set value to form field (without triggering signals)."""
        field = self._form_fields.get(key)
        if not field:
            return
        widget = field["widget"]
        if field_type == "bool":
            widget.blockSignals(True)
            widget.setChecked(value.lower() in {"true", "1", "yes", "y"})
            widget.blockSignals(False)
        elif field_type == "multiline":
            widget.blockSignals(True)
            if value:
                widget.setPlainText("\n".join([v.strip() for v in value.split(";") if v.strip()]))
            else:
                widget.setPlainText("")
            widget.blockSignals(False)
        else:
            widget.blockSignals(True)
            widget.setText(value)
            widget.blockSignals(False)

    def populate_from_content(self, content: str, parse_func, extra_fields_refresh_func) -> None:
        """Populate form from editor content using blockSignals."""
        self.update_comment_map(content)
        data = parse_func(content)
        if self.refresh_extra_fields(data):
            # Trigger form rebuild through external callback
            pass
        # Use blockSignals to prevent signal loops
        for key, field in self._form_fields.items():
            widget = field["widget"]
            field_type = field["type"]
            value = data.get(key, "")
            self.set_form_value(key, value, field_type)

    def get_extra_fields(self) -> List[Dict[str, Any]]:
        """Get extra fields."""
        return self._extra_fields.copy()

    def get_extra_field_keys(self) -> List[str]:
        """Get extra field keys."""
        return self._extra_field_keys.copy()
