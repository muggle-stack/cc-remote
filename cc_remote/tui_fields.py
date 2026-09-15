"""Named protocol fields with plain text editing and explicit advanced data."""

import json

from textual.containers import VerticalScroll
from textual.widgets import Label

from cc_remote.tui_details import details, field_label
from cc_remote.tui_modal import ModalEditor


def field_schema(schema, root):
    if "$ref" in schema:
        return field_schema(
            root.get("$defs", {}).get(schema["$ref"].rsplit("/", 1)[-1], {}),
            root,
        )
    return schema


class ParameterFields(VerticalScroll):
    DEFAULT_CSS = """
    ParameterFields { height: 1fr; }
    ParameterFields .parameter { height: 4; min-height: 3; }
    ParameterFields .complex { height: 7; }
    ParameterFields Label { height: auto; max-height: 100%; }
    """

    def __init__(self, values, schema):
        super().__init__()
        self.values, self.schema = values, schema
        self.rows = []
        self.advanced = set()

    def compose(self):
        for index, (key, value) in enumerate(self.values.items()):
            spec = field_schema(
                self.schema.get("properties", {}).get(key, {}), self.schema
            )
            variants = [
                field_schema(s, self.schema) for s in spec.get("anyOf", [spec])
            ]
            types = {s.get("type") for s in variants}
            choices = [v for s in variants for v in s.get("enum", [])]
            complex_value = bool(types & {"object", "array"})
            if complex_value and value == "":
                value = [] if "array" in types else {}
            text = (
                details(value)
                if complex_value
                else ""
                if value is None
                else "yes"
                if value is True
                else "no"
                if value is False
                else str(value)
            )
            info = " / ".join(str(v) for v in choices)
            if not info:
                info = " / ".join(sorted(t for t in types if t)) or "text"
            if "null" in types:
                info += "; blank = default"
            if complex_value:
                info += "; advanced editor available via form shortcuts"
            yield Label(f"{field_label(key)} · {info}", markup=False)
            editor = ModalEditor(
                text,
                id=f"parameter-{index}",
                locked=complex_value or key == "session_id",
                classes="parameter complex" if complex_value else "parameter",
            )
            self.rows.append((key, types, value, text, editor))
            yield editor

    def focus_editor(self):
        for _, _, _, _, editor in self.rows:
            if not editor.locked:
                editor.focus()
                return editor
        if self.rows:
            self.rows[0][-1].focus()
        return None

    def toggle_advanced(self, focused):
        for key, types, value, _, editor in self.rows:
            if editor is focused and types & {"object", "array"}:
                if key in self.advanced:
                    return
                editor.load_text(
                    json.dumps(value, ensure_ascii=False, indent=2)
                )
                editor.locked = False
                editor.set_mode("INSERT")
                self.advanced.add(key)
                return

    def payload(self):
        values = {}
        for key, types, original, initial, editor in self.rows:
            raw = editor.text
            if key in self.advanced:
                try:
                    values[key] = json.loads(raw)
                except ValueError as exc:
                    raise ValueError(
                        f"{field_label(key)}: invalid advanced data"
                    ) from exc
            elif raw == initial or editor.locked:
                values[key] = original
            elif not raw.strip() and "null" in types:
                values[key] = None
            elif "boolean" in types:
                if raw.strip().lower() not in {"yes", "no", "true", "false"}:
                    raise ValueError(f"{field_label(key)}: enter yes or no")
                values[key] = raw.strip().lower() in {"yes", "true"}
            elif "string" in types or not types - {None}:
                values[key] = raw
            else:
                try:
                    values[key] = json.loads(raw)
                except ValueError as exc:
                    raise ValueError(
                        f"{field_label(key)}: enter a number"
                    ) from exc
        return json.dumps(values, ensure_ascii=False)
