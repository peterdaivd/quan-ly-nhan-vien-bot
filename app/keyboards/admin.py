from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def admin_menu(*, super_admin: bool = False) -> ReplyKeyboardMarkup:
    if not super_admin:
        rows = [
            [KeyboardButton(text="👥 Nhân viên của tôi"), KeyboardButton(text="🔗 Mời nhân viên")],
            [KeyboardButton(text="📊 Tổng hợp nhóm"), KeyboardButton(text="📦 Hàng hóa nhóm")],
            [KeyboardButton(text="💰 Xem hoa hồng")],
            [KeyboardButton(text="💰 Nhân viên nộp cho tôi")],
            [KeyboardButton(text="💸 Nộp tiền Admin tổng")],
            [KeyboardButton(text="👤 Chế độ cá nhân")],
        ]
    else:
        rows = [
            [KeyboardButton(text="👑 Quản lý Admin")],
            [KeyboardButton(text="👥 Tất cả nhân viên"), KeyboardButton(text="📦 Quản lý sản phẩm")],
            [KeyboardButton(text="💰 Quản lý hoa hồng"), KeyboardButton(text="📊 Thống kê")],
            [KeyboardButton(text="📋 Hoạt động nhân viên"), KeyboardButton(text="👤 Chế độ cá nhân")],
            [KeyboardButton(text="👥 Danh sách người dùng")],
            [KeyboardButton(text="🗑 Người dùng đã xóa"), KeyboardButton(text="📨 Yêu cầu chờ duyệt")],
            [KeyboardButton(text="⏰ Nhắc nộp tiền")],
        ]
    return ReplyKeyboardMarkup(
        keyboard=rows, resize_keyboard=True,
    )
