from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 /list", callback_data="cmd:list")
    b.button(text="📊 /stats", callback_data="cmd:stats")
    b.button(text="❓ /unknown", callback_data="cmd:unknown")
    b.adjust(3)
    return b.as_markup()
