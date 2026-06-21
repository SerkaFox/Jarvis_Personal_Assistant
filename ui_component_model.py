"""Generic UI component model -- one schema describing ANY interactive site
component (slider, carousel, accordion, tabs, hamburger menu, gallery,
language switcher, form, weather block, footer, hero, background),
independent of markup convention or which stack rendered the page. This
replaces hardcoded per-component checks (e.g. a function that specifically
knows what a "slider" looks like) with one data-driven model that
ui_component_verifier.py interprets against the rendered DOM -- adding a new
kind means adding selectors here, not writing a new checker function.
"""
from dataclasses import dataclass, field
from typing import Any

COMPONENT_KINDS = (
    "slider",
    "accordion",
    "tabs",
    "hamburger_menu",
    "gallery",
    "language_switcher",
    "form",
    "weather_block",
    "footer",
    "hero",
    "background",
)

# Free-text/Russian word -> canonical kind. "carousel"/"карусель" is
# deliberately an ALIAS of "slider", not a separate kind: same verification
# semantics (a row of items you move between), different common naming.
KIND_ALIASES: dict[str, str] = {
    "slider": "slider",
    "слайдер": "slider",
    "carousel": "slider",
    "карусель": "slider",
    "accordion": "accordion",
    "аккордеон": "accordion",
    "гармонь": "accordion",
    "гармошка": "accordion",
    "гармошку": "accordion",
    "гармошки": "accordion",
    "гармошкой": "accordion",
    "tabs": "tabs",
    "вкладки": "tabs",
    "вкладок": "tabs",
    "табы": "tabs",
    "hamburger_menu": "hamburger_menu",
    "hamburger menu": "hamburger_menu",
    "бургер": "hamburger_menu",
    "гамбургер": "hamburger_menu",
    "мобильное меню": "hamburger_menu",
    "menu": "hamburger_menu",
    "меню": "hamburger_menu",
    "gallery": "gallery",
    "галерея": "gallery",
    "language_switcher": "language_switcher",
    "переключатель языков": "language_switcher",
    "переключение языков": "language_switcher",
    "языки": "language_switcher",
    "language": "language_switcher",
    "form": "form",
    "форма": "form",
    "форму": "form",
    "формы": "form",
    "форме": "form",
    "формой": "form",
    "weather_block": "weather_block",
    "погода": "weather_block",
    "weather": "weather_block",
    "footer": "footer",
    "футер": "footer",
    "подвал": "footer",
    "hero": "hero",
    "херо": "hero",
    "background": "background",
    "фон": "background",
}

# Container selectors per kind -- semantic + common class/id/data-attribute
# conventions. Any ONE matching is enough to consider the container found;
# being a list (not a single hardcoded class) is what makes this work across
# arbitrary markup/frameworks instead of just one convention.
DEFAULT_SELECTORS: dict[str, list[str]] = {
    "slider": [
        ".slider", ".carousel", "[data-slider]", "[data-carousel]",
        ".swiper", ".slick", ".splide", ".jarvis-slider",
    ],
    "accordion": [".accordion", "[data-accordion]", ".jarvis-accordion"],
    "tabs": [".tabs", "[role=tablist]", "[data-tabs]"],
    "hamburger_menu": [".hamburger", ".burger", ".menu-toggle", "[data-menu-toggle]", "nav"],
    "gallery": [".gallery", "[data-gallery]"],
    "language_switcher": [".jarvis-lang-buttons", ".lang-switcher", "[data-lang]"],
    "form": ["form"],
    "weather_block": ["#jarvis-weather", "[data-weather]", ".weather", ".jarvis-weather"],
    "footer": ["footer", ".footer", ".jarvis-footer"],
    "hero": [".hero", "#hero", ".hero-section", ".jarvis-hero"],
    "background": ["body", ".hero"],
}

# Repeated-item selectors per kind, used to count "how many slides/items/tabs
# are there" -- a generic "structure of repeated elements" probe, not a
# single fixed class name.
ITEM_SELECTORS: dict[str, list[str]] = {
    "slider": [
        ".slide", ".carousel-item", "[data-slide]",
        ".swiper-slide", ".slick-slide", ".splide__slide", ".jarvis-slide",
    ],
    "accordion": [".accordion-item", "[data-accordion-item]", "details", ".jarvis-accordion-item"],
    "tabs": ["[role=tab]", ".tab"],
    "gallery": [".gallery-item", "img"],
}

# Controls that toggle/advance the component -- used for the interaction
# probe (click + observe DOM change). A missing nav control is NOT
# automatically a failure: it just means interactivity can't be confirmed.
NAV_SELECTORS: dict[str, list[str]] = {
    "slider": [
        ".next", ".prev", ".slider-nav", ".slider-dot", ".slide-dot",
        "[data-slide-next]", "[data-slide-prev]", "button",
    ],
    "accordion": ["[aria-expanded]", "summary", "button"],
    "tabs": ["[role=tab]", "button"],
    "hamburger_menu": ["button", "[aria-expanded]", "a"],
    "gallery": ["button", "a"],
}


def normalize_kind(text: str) -> str | None:
    """Maps free text (any language/alias) to a canonical kind, or None if no
    known component is mentioned. Longest alias wins so "hamburger menu"
    matches before a looser "menu" would."""
    lowered = (text or "").lower()
    for alias in sorted(KIND_ALIASES, key=len, reverse=True):
        if alias in lowered:
            return KIND_ALIASES[alias]
    return None


@dataclass
class ComponentModel:
    kind: str
    aliases: list[str] = field(default_factory=list)
    selectors: list[str] = field(default_factory=list)
    item_selectors: list[str] = field(default_factory=list)
    nav_selectors: list[str] = field(default_factory=list)
    expected_items: int | None = None
    expected_content: list[str] = field(default_factory=list)
    expected_behavior: str | None = None
    required_interactivity: bool = False
    related_files: list[str] = field(default_factory=list)
    verification_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "aliases": self.aliases,
            "selectors": self.selectors,
            "item_selectors": self.item_selectors,
            "nav_selectors": self.nav_selectors,
            "expected_items": self.expected_items,
            "expected_content": self.expected_content,
            "expected_behavior": self.expected_behavior,
            "required_interactivity": self.required_interactivity,
            "related_files": self.related_files,
            "verification_result": self.verification_result,
        }


def build_component_model(
    kind: str,
    *,
    expected_items: int | None = None,
    expected_content: list[str] | None = None,
    expected_behavior: str | None = None,
    required_interactivity: bool = False,
    related_files: list[str] | None = None,
) -> ComponentModel:
    """Builds a ComponentModel for a canonical or aliased kind name, filling
    in the generic default selectors for that kind."""
    canonical = KIND_ALIASES.get(kind.lower(), kind.lower())
    return ComponentModel(
        kind=canonical,
        aliases=sorted(a for a, k in KIND_ALIASES.items() if k == canonical),
        selectors=list(DEFAULT_SELECTORS.get(canonical, [])),
        item_selectors=list(ITEM_SELECTORS.get(canonical, [])),
        nav_selectors=list(NAV_SELECTORS.get(canonical, [])),
        expected_items=expected_items,
        expected_content=expected_content or [],
        expected_behavior=expected_behavior,
        required_interactivity=required_interactivity,
        related_files=related_files or [],
    )
