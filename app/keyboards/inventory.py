from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

PAGE_SIZE = 8


def _reply(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
    )


def operation_menu(action: str) -> ReplyKeyboardMarkup:
    return _reply([
        ["📦 Sản phẩm"],
        [f"✅ Xác nhận {action}"],
        ["🗑 Xóa lựa chọn", "⬅️ Quay lại"],
    ])


def product_grid(labels: list[str], page: int, pages: int) -> ReplyKeyboardMarkup:
    rows = [labels[index:index + 2] for index in range(0, len(labels), 2)]
    if pages > 1:
        navigation = []
        if page > 0:
            navigation.append("◀️ Trang trước")
        navigation.append(f"📄 {page + 1}/{pages}")
        if page + 1 < pages:
            navigation.append("Trang sau ▶️")
        rows.append(navigation)
    rows.extend([["⬅️ Quay lại", "📋 Xem lựa chọn"]])
    return _reply(rows)


def quantity_keyboard(quantity: int) -> ReplyKeyboardMarkup:
    return _reply([
        ["➖", f"🔢 {quantity}", "➕"],
        ["1️⃣", "2️⃣", "3️⃣", "5️⃣", "🔟"],
        ["✅ Đưa ra sản phẩm"],
        ["🗑 Xóa lựa chọn", "⬅️ Quay lại"],
    ])


def cart_keyboard(action: str) -> ReplyKeyboardMarkup:
    return _reply([
        [f"✅ Xác nhận {action}"],
        ["📦 Sản phẩm"],
        ["🗑 Xóa lựa chọn", "⬅️ Quay lại"],
    ])


def final_confirmation_keyboard() -> ReplyKeyboardMarkup:
    return _reply([
        ["✅ Xác nhận"],
        ["✏️ Chọn thêm sản phẩm"],
        ["🗑 Xóa lựa chọn"],
        ["❌ Hủy"],
    ])


def period_keyboard(prefix: str, *, history: bool = False):
    # Báo cáo dùng callback để giữ nguyên cách chọn khoảng thời gian hiện có.
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[
        InlineKeyboardButton(text="Hôm nay", callback_data=f"{prefix}:today"),
        InlineKeyboardButton(text="Tháng này", callback_data=f"{prefix}:month"),
        InlineKeyboardButton(text="Tất cả", callback_data=f"{prefix}:all"),
    ]]
    if history:
        rows.insert(1, [InlineKeyboardButton(text="7 ngày", callback_data=f"{prefix}:7days")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
