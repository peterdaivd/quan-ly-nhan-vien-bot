from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def user_menu(payment_label: str = "💸 Nộp tiền") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Nhập hàng"), KeyboardButton(text="📊 Chốt bán hàng")],
            [KeyboardButton(text="📦 Hàng đang có"), KeyboardButton(text="💰 Doanh thu & hoa hồng")],
            [KeyboardButton(text="💳 Ví của tôi"), KeyboardButton(text="💵 Nạp tiền")],
            [KeyboardButton(text="📜 Lịch sử"), KeyboardButton(text="👤 Tài khoản")],
            [KeyboardButton(text="💰 Hoa hồng của bạn"), KeyboardButton(text="🚚 Tiền ship")],
            [KeyboardButton(text=payment_label)],
        ], resize_keyboard=True,
    )


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="📱 Chia sẻ số điện thoại", request_contact=True)
        ]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def account_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Cập nhật số điện thoại")],
            [KeyboardButton(text="⬅️ Quay lại")],
        ],
        resize_keyboard=True,
    )
