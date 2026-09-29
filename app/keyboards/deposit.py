from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def deposit_actions(deposit_id: int, checkout_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 THANH TOÁN", url=checkout_url)],
        [
            InlineKeyboardButton(text="🔄 KIỂM TRA", callback_data=f"deposit:check:{deposit_id}"),
            InlineKeyboardButton(text="❌ HỦY", callback_data=f"deposit:cancel:{deposit_id}"),
        ],
    ])
