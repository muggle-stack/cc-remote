"""Group one Vim change plus Insert input as a single undo transaction."""

from textual.document._history import EditHistory


class VimHistory(EditHistory):
    # Textual's default splits deletion, insertion, paste and newlines into
    # separate batches. Vim's c{motion} ... Esc is one edit instead. Keep this
    # version-specific adapter here, covered by keyboard undo/redo tests.
    group_open = False
    group_started = False

    def begin(self):
        if not self.group_open:
            self.checkpoint()
            self.group_open = True
            self.group_started = False

    def finish(self):
        self.group_open = self.group_started = False
        self.checkpoint()

    def record(self, edit):
        result = edit._edit_result
        if result is not None and not edit.text and not result.replaced_text:
            return
        if self.group_open and self.group_started and self._undo_stack:
            self._undo_stack[-1].append(edit)
            self._redo_stack.clear()
        else:
            super().record(edit)
            if self.group_open:
                self.group_started = True

    def clear(self):
        self.group_open = self.group_started = False
        super().clear()
