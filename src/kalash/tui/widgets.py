"""Legacy widget stubs — kept for import compatibility."""

from textual.widget import Widget


class HeaderWidget(Widget):
    DEFAULT_CSS = "HeaderWidget { display: none; }"

    def update_info(self, **kwargs) -> None:
        pass


class StatusBar(Widget):
    DEFAULT_CSS = "StatusBar { display: none; }"


class InputWidget(Widget):
    DEFAULT_CSS = "InputWidget { display: none; }"


class TranscriptView(Widget):
    DEFAULT_CSS = "TranscriptView { display: none; }"


class ToolCallWidget(Widget):
    DEFAULT_CSS = "ToolCallWidget { display: none; }"


class ApprovalWidget(Widget):
    DEFAULT_CSS = "ApprovalWidget { display: none; }"
