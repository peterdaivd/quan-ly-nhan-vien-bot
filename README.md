# Telegram Bot quản lý bán hàng và ví payOS

Bot gồm hai nhóm sử dụng:

- Admin xem, tìm kiếm, khóa/mở khóa người dùng và xem số liệu của từng user.
- Người dùng tự đăng ký khi gửi `/start`, tự nhập hàng, chốt bán, xem tồn, doanh thu, hoa hồng, lịch sử và nạp ví qua payOS.

Project dùng Python 3.11+, aiogram 3.x, FastAPI, SQLAlchemy, SQLite và SDK Python chính thức của payOS. BIDV là tài khoản nhận tiền đã liên kết với kênh thanh toán payOS; project không gọi API BIDV trực tiếp.

## Cài đặt trên Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Nếu PowerShell chặn kích hoạt môi trường ảo:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

## Cấu hình `.env`

```dotenv
BOT_TOKEN=TOKEN_BOTFATHER
ADMIN_IDS=TELEGRAM_ID_ADMIN
DATABASE_URL=sqlite+aiosqlite:///employee_bot.db

PAYMENT_MODE=PAYOS
PAYOS_CLIENT_ID=
PAYOS_API_KEY=
PAYOS_CHECKSUM_KEY=

PAYOS_WEBHOOK_URL=https://DOMAIN-CUA-TOI.com/api/payos/webhook
PAYOS_RETURN_URL=https://DOMAIN-CUA-TOI.com/payment/success
PAYOS_CANCEL_URL=https://DOMAIN-CUA-TOI.com/payment/cancel
PAYOS_PAYMENT_EXPIRES_MINUTES=30

API_HOST=0.0.0.0
API_PORT=8080
```

Ba key payOS chỉ được điền trực tiếp trên máy chạy backend. Không gửi key qua Telegram/chat, không đặt trong source và không commit `.env`. File `.env` đã nằm trong `.gitignore`.

## Chạy bot và FastAPI

```powershell
.\.venv\Scripts\python.exe main.py
```

Một tiến trình sẽ chạy đồng thời Telegram polling và FastAPI/Uvicorn trên `API_HOST:API_PORT`. Database và migration được chạy tự động khi khởi động. Log chỉ in webhook URL, không in API key hoặc checksum key.

Kiểm tra backend:

```text
GET https://DOMAIN-CUA-TOI.com/api/health
```

## Webhook cần nhập trên my.payos.vn

Trong kênh thanh toán tại [my.payos.vn](https://my.payos.vn), nhập chính xác URL:

```text
https://DOMAIN-CUA-TOI.com/api/payos/webhook
```

Giá trị này phải giống `PAYOS_WEBHOOK_URL` trong `.env`. Không nhập `127.0.0.1` hoặc URL HTTP local vì payOS phải truy cập được từ Internet.

Nếu backend chạy trên Windows tại nhà, đặt FastAPI sau reverse proxy HTTPS hoặc Cloudflare Tunnel:

```text
Internet HTTPS → reverse proxy/tunnel → http://127.0.0.1:8080
```

Ví dụ hostname public là `bot.example.com`:

```dotenv
PAYOS_WEBHOOK_URL=https://bot.example.com/api/payos/webhook
PAYOS_RETURN_URL=https://bot.example.com/payment/success
PAYOS_CANCEL_URL=https://bot.example.com/payment/cancel
```

Return/cancel URL chỉ hiển thị kết quả trên trình duyệt. Backend tuyệt đối không cộng ví từ query parameter của các URL này.

## Luồng nạp tiền

1. User chọn **💵 Nạp tiền** và nhập tối thiểu 1.000đ.
2. Backend tạo `order_code` nguyên duy nhất trong database trước khi gọi payOS.
3. SDK payOS tạo checkout URL/VietQR; bot gửi nút **💳 THANH TOÁN**.
4. Người dùng thanh toán qua payOS, tiền về tài khoản BIDV đã liên kết.
5. payOS gửi webhook; SDK chính thức xác minh chữ ký.
6. Backend kiểm tra `orderCode`, số tiền, `paymentLinkId`, reference và trạng thái deposit trong một transaction.
7. Backend cộng ví, tạo `wallet_transaction`, `payment_event`, đổi deposit sang `PAID`, sau đó tự báo Telegram.

Nút **🔄 KIỂM TRA** gọi trực tiếp API payOS. Nếu payOS trả `PAID`, nó dùng chính hàm xử lý idempotent của webhook. Nút **❌ HỦY** gọi API cancel payOS trước khi đổi trạng thái local.

Webhook lặp cùng reference trả HTTP 200 nhưng không cộng lại. Sai tiền hoặc `paymentLinkId` chuyển deposit sang `REVIEW`. Deposit `CANCELLED`, `EXPIRED`, `FAILED` hoặc đã `PAID` không được cộng.

## Nghiệp vụ hàng hóa

Admin tạo danh mục sản phẩm chung gồm tên, quy cách, giá và hoa hồng. User không nhập hoặc sửa các thông tin này. Luồng nhập và bán dùng Reply Keyboard theo dạng phiếu tạm:

```text
Chọn Sản phẩm → chọn/cộng số lượng → Đưa ra sản phẩm
→ có thể chọn tiếp → xem phiếu tạm → xác nhận cuối
```

Các nút `➖`, `➕`, `1️⃣`, `2️⃣`, `3️⃣`, `5️⃣`, `🔟` hỗ trợ thay đổi nhanh; user vẫn có thể gửi trực tiếp một số nguyên. Chọn lại cùng sản phẩm sẽ cộng dồn. Nhập kho hoặc trừ tồn chỉ xảy ra sau nút xác nhận cuối. Phiếu bán nhiều sản phẩm được ghi nguyên tử: nếu một sản phẩm vượt tồn thì toàn bộ phiếu được rollback.

```text
tồn mới = tồn cũ + số lượng nhập thêm - số lượng bán
doanh thu = giá bán × số lượng bán
hoa hồng % = doanh thu × phần trăm / 100
hoa hồng cố định = số lượng bán × tiền hoa hồng mỗi sản phẩm
trả công ty = doanh thu - hoa hồng
```

Tiền được lưu bằng INTEGER VND. Phần trăm dùng `Decimal`. Mọi thao tác đều xác định user từ Telegram update thực tế và kiểm tra ownership ở backend.

## Kiểm thử

```powershell
.\.venv\Scripts\python.exe -m compileall -q app main.py tests
.\.venv\Scripts\python.exe -m pytest -q
```

Test payOS bao gồm: cộng 500.000đ từ số dư 1.000.000đ, webhook lặp, chữ ký sai, sai amount/orderCode/reference/paymentLinkId, deposit đã hủy/đã thanh toán, tách hai user và rollback khi database lỗi.

## Tài liệu chính thức

- [payOS Python SDK](https://payos.vn/docs/sdks/back-end/python/)
- [SDK source chính thức](https://github.com/payOSHQ/payos-lib-python)
- [payOS API](https://payos.vn/docs/api/)
