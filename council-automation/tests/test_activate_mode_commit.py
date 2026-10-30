"""Unit tests for PerplexityCouncil.activate_mode's palette-commit step.

Locks the 2026-10-30 fix: Perplexity's slash-command palette became a Lexical
typeahead (#typeahead-menu, role="menuitem") that ignores Space, so every
research run for ~45h typed "/research", pressed Space, and failed as
"Failed to activate research mode" while signed in. The commit step must click
the matching menu item when one rendered, and fall back to the key otherwise.

Run with:
    python -m pytest tests/test_activate_mode_commit.py -v
(from council-automation/, so `import council_browser` resolves via cwd.)
"""
import asyncio

import council_browser as cb


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self.page = page
        self.pressed: list[str] = []

    async def type(self, text: str, delay: int = 0) -> None:
        self.page.typed += text

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakeLocator:
    """Models page.locator(sel).filter(has_text=re).first for the palette."""

    def __init__(self, page: "FakePage", item_text: str | None) -> None:
        self.page = page
        self.item_text = item_text
        self.pattern = None

    def filter(self, has_text) -> "FakeLocator":
        self.pattern = has_text
        return self

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        if self.item_text is None:
            return 0
        return 1 if self.pattern.search(self.item_text) else 0

    async def click(self, timeout: int = 0) -> None:
        self.page.mode_active = True


class FakePage:
    """The 2026-10-30 UI: only a click on the palette item engages the mode."""

    url = "https://www.perplexity.ai/"

    def __init__(self, item_text: str | None) -> None:
        self.item_text = item_text
        self.typed = ""
        self.mode_active = False
        self.keyboard = FakeKeyboard(self)

    async def click(self, selector: str) -> None:
        pass

    async def wait_for_timeout(self, ms: int) -> None:
        pass

    def locator(self, selector: str) -> FakeLocator:
        assert selector == cb.PALETTE_MENUITEM_SELECTOR
        return FakeLocator(self, self.item_text)

    async def evaluate(self, script: str) -> bool:
        # Every verifier tier is an evaluate(); report the mode button state.
        return self.mode_active


def _council(mode: str) -> "cb.PerplexityCouncil":
    c = object.__new__(cb.PerplexityCouncil)
    c.perplexity_mode = mode
    c.selectors = {}
    return c


def test_research_clicks_typeahead_item_instead_of_space() -> None:
    page = FakePage("Deep Research")
    assert asyncio.run(_council("research").activate_mode(page)) is True
    assert page.typed == "/research"
    assert page.keyboard.pressed == []  # Space was the bug; it must not be needed


def test_falls_back_to_commit_key_when_no_palette_item() -> None:
    page = FakePage(None)
    # No item rendered: the key is pressed, and since the fake UI ignores it the
    # verifier honestly reports failure instead of an optimistic pass.
    assert asyncio.run(_council("research").activate_mode(page)) is False
    assert page.keyboard.pressed == [cb.PERPLEXITY_COMMIT_KEY]


def test_item_for_another_mode_is_not_clicked() -> None:
    page = FakePage("Model council")
    assert asyncio.run(_council("research").activate_mode(page)) is False
    assert page.keyboard.pressed == [cb.PERPLEXITY_COMMIT_KEY]
