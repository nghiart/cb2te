# CB2TE — Ghi hình Chaturbate tự động → Telegram (Termux Android)

> Hệ thống chạy **24/7 trên điện thoại Android (Termux)**: Tự động phát hiện streamer online, ghi hình độ phân giải gốc (lên tới 1080p kèm âm thanh đầy đủ), đóng gói MP4, tải lên Telegram (hỗ trợ file tới 2 GB), tạm dừng an toàn khi bộ nhớ thấp và hỗ trợ dọn dẹp linh hoạt. Điều khiển từ xa qua Telegram Bot.

<p align="center">
  <b>Chaturbate</b> → <i>Kiểm tra 60s/lần</i> → <b>Ghi luồng HLS 1080p (ffmpeg)</b> → <b>Đóng gói MP4</b> → <b>Upload Telegram</b> → <b>Lưu 48h / Dọn tự động & thủ công</b>
</p>

---

## 📑 Mục lục

1. [Tính năng nổi bật](#1-tính-năng-nổi-bật)
2. [Điện thoại cần chuẩn bị](#2-điện-thoại-cần-chuẩn-bị)
3. [Cài đặt nhanh trên Termux](#3-cài-đặt-nhanh-trên-termux)
4. [Cấu hình hệ thống](#4-cấu-hình-hệ-thống)
5. [Khởi chạy hệ thống](#5-khởi-chạy-hệ-thống)
6. [Danh sách lệnh Telegram Bot](#6-danh-sách-lệnh-telegram-bot)
7. [Các lệnh CLI trong Termux](#7-các-lệnh-cli-trong-termux)
8. [Tổ chức thư mục lưu trữ](#8-tổ-chức-thư-mục-lưu-trữ)
9. [Cấu trúc mã nguồn](#9-cấu-trúc-mã-nguồn)

---

## 1. Tính năng nổi bật

- **Chất lượng ghi hình tối đa (1080p + Audio):**
  - Tự động nhận diện độ phân giải stream cao nhất (trần 1080p).
  - Tách và map đồng thời luồng video 1080p và luồng âm thanh AAC từ master playlist, khắc phục hoàn toàn hiện tượng video không có tiếng hoặc token HLS bị 403.
- **Tối ưu tài nguyên điện thoại:**
  - Sử dụng `ffmpeg -c copy` trực tiếp: CPU < 2%, RAM < 80 MB, máy mát, không hao pin, không bị Android Low Memory Killer tắt ngang.
  - Tự động giữ `termux-wake-lock` giúp tiến trình tiếp tục chạy khi tắt màn hình.
- **Giám sát & Thông báo thông minh:**
  - Quét danh sách theo dõi định kỳ mỗi 60 giây.
  - Chỉ gửi thông báo Telegram khi có streamer thay đổi trạng thái (online, offline, private).
- **Cơ chế bảo vệ bộ nhớ & Dọn dẹp:**
  - **Tự động tạm dừng:** Khi dung lượng trống ổ đĩa thấp hơn ngưỡng an toàn (`min_free_mb`, mặc định 400 MB), hệ thống tạm ngừng nhận ghi phòng mới và ngắt phiên an toàn để tránh tràn ổ đĩa.
  - **Xóa tự động (Retention):** Tự động dọn file đã tải lên sau 48 giờ.
  - **Xóa thủ công từ xa:** Cho phép gõ lệnh `/clean` trên Telegram để dọn toàn bộ hoặc xóa từng phòng cụ thể ngay lập tức.
- **Tải lên Telegram không giới hạn:**
  - Tận dụng Telethon MTProto upload trực tiếp file lên tới 2 GB vào nhóm/kênh Telegram riêng tư.

---

## 2. Điện thoại cần chuẩn bị

| Yêu cầu | Chi tiết |
|---|---|
| Hệ điều hành | Android 8.0 trở lên (không cần root máy) |
| Ứng dụng Termux | Tải từ **F-Droid** ([termux-app](https://f-droid.org/packages/com.termux/)) |
| Termux:API | Tải từ **F-Droid** ([termux-api](https://f-droid.org/packages/com.termux.api/)) |
| Dung lượng trống | Tối thiểu 1 GB – 2 GB bộ nhớ trong |
| Kết nối mạng | Wi-Fi ổn định và cắm sạc nếu treo 24/7 |

> ⚠️ **Lưu ý:** Vào **Cài đặt Android → Ứng dụng → Termux → Pin → Không hạn chế (Unrestricted)** để tránh Android tắt Termux khi màn hình tắt.

---

## 3. Cài đặt nhanh trên Termux

Mở Termux và thực hiện lần lượt các bước:

```bash
# 1. Cấp quyền truy cập bộ nhớ cho Termux
termux-setup-storage

# 2. Cài đặt các gói công cụ cơ bản
pkg update -y
pkg install -y git python ffmpeg openssl

# 3. Tải mã nguồn dự án
git clone https://github.com/nghiart/cb2te.git
cd cb2te

# 4. Cài đặt thư viện Python
pip install -r requirements.txt
```

---

## 4. Cấu hình hệ thống

Sao chép file cấu hình mẫu sang file chính thức:

```bash
cp settings.example.json settings.json
cp favorites.example.json favorites.json
```

### Chỉnh sửa `settings.json`:
```json
{
  "api_id": 12345678,
  "api_hash": "your_api_hash_here",
  "bot_token": "1234567890:AAExampleTokenHere",
  "chat_id": -1001234567890,
  "admin_ids": [123456789],
  "retention_hours": 48,
  "max_concurrent": 2,
  "min_free_mb": 400,
  "proxy": "",
  "monitor_interval_sec": 60,
  "notify_on_status_change": true,
  "session_max_duration_sec": 900,
  "use_ffmpeg_recorder": true,
  "max_recording_height": 1080
}
```

* `api_id`, `api_hash`: Lấy từ trang [my.telegram.org](https://my.telegram.org).
* `bot_token`: Tạo từ `@BotFather` trên Telegram.
* `chat_id`: ID của nhóm hoặc kênh nhận file video (bot phải được thêm vào làm Admin).
* `admin_ids`: Danh sách ID tài khoản Telegram được phép điều khiển bot (lấy qua `@userinfobot`).
* `min_free_mb`: Mức dung lượng trống tối thiểu (MB) để tự động pause ghi nếu bộ nhớ gần đầy.
* `max_recording_height`: Độ phân giải trần khi ghi (mặc định `1080`).

---

## 5. Khởi chạy hệ thống

### Chạy chế độ 24/7 (Daemon + Telegram Bot):
```bash
python3 1st.py bot
```

Hoặc chạy ngầm trong nền (vẫn chạy sau khi đóng cửa sổ Termux):
```bash
nohup python3 1st.py bot > daemon.log 2>&1 < /dev/null &
```

---

## 6. Danh sách lệnh Telegram Bot

Gửi lệnh trực tiếp vào đoạn chat với Bot:

| Lệnh | Mô tả |
|---|---|
| `/status` | Xem tổng quan hệ thống: tiến trình ghi, dung lượng trống, hàng đợi upload |
| `/status <user>` | Xem chi tiết độ phân giải, luồng HLS, trạng thái online của phòng cụ thể |
| `/rec <user>` | Kích hoạt theo dõi và tự động ghi hình một streamer ngay lập tức |
| `/stop <user\|all>` | Dừng ghi một streamer hoặc tất cả streamer đang thu |
| `/upload <user\|file>` | Đưa video còn tồn trong máy vào hàng đợi upload thủ công |
| `/fav` | Liệt kê danh sách streamer đang theo dõi |
| `/add <user>` | Thêm streamer vào danh sách theo dõi |
| `/del <user>` | Xóa streamer khỏi danh sách theo dõi |
| `/files` | Liệt kê các video đã ghi thành công trong vòng 48h |
| `/clean [user\|all]` | Xóa video đã lưu trên máy để giải phóng bộ nhớ thủ công |
| `/help` | Hiển thị bảng trợ giúp hướng dẫn lệnh |

---

## 7. Các lệnh CLI trong Termux

Ngoài việc điều khiển qua Telegram, bạn có thể thực hiện kiểm tra trực tiếp từ terminal:

```bash
# Kiểm tra tình trạng lưu trữ và danh sách theo dõi
python3 1st.py status

# Ghi hình nhanh 1 streamer độc lập từ CLI
python3 1st.py rec <username>

# Chẩn đoán lỗi mạng / ffmpeg / quyền ghi đĩa
python3 1st.py doctor

# Đóng gói lại các file spool tạm nếu bị ngắt đột ngột
python3 1st.py remux
```

---

## 8. Tổ chức thư mục lưu trữ

Các file video được phân chia thư mục rõ ràng theo nền tảng và tên phòng:

```text
recordings/
└── chaturbate/
    ├── streamer_a/
    │   └── chaturbate_streamer_a_20261114_160000.mp4
    └── streamer_b/
        └── chaturbate_streamer_b_20261114_163000.mp4
```

---

## 9. Cấu trúc mã nguồn

```text
cb2te/
├── 1st.py                   # Điểm khởi chạy chính CLI & Daemon 24/7
├── bot/
│   ├── handlers.py          # Xử lý các lệnh Telegram Bot (/status, /clean, /rec...)
│   ├── telegram_bot.py      # Kết nối và xác thực Telethon client
│   └── uploader.py          # Quản lý hàng đợi tải video MTProto (hỗ trợ tới 2 GB)
├── core/
│   ├── android.py           # Quản lý wake-lock, kiểm tra dung lượng trống ổ đĩa
│   ├── config.py            # Quản lý nạp và lưu trữ cấu hình JSON / TXT
│   ├── doctor.py            # Bộ công cụ chẩn đoán môi trường và kết nối
│   ├── muxer.py             # Đóng gói remux MP4 với ffmpeg
│   ├── net.py               # Quản lý yêu cầu mạng, browser headers và proxy
│   ├── recorder.py          # Thăm dò phòng, phân tích playlist 1080p và ghi ffmpeg
│   └── task_manager.py      # Điều phối đa luồng, giới hạn phiên và kiểm soát bộ nhớ
├── storage/
│   ├── cleaner.py           # Bộ dọn dẹp theo thời gian retention 48h & xả khẩn cấp
│   └── history.py           # Cơ sở dữ liệu JSON theo dõi lịch sử file đã upload
├── tests/                   # Bộ kiểm thử tự động (129 unit tests)
├── install_termux.sh        # Script cài đặt môi trường cho Termux
├── reset.sh                 # Script dọn dẹp tiến trình và reset bot
└── requirements.txt         # Danh mục thư viện Python
```

Chạy kiểm thử:
```bash
python3 -m unittest discover -s tests
```
