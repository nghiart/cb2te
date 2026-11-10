# Chaturbate Termux Telegram Recorder (CB2TE)

Hệ thống ghi hình luồng phát sóng Chaturbate tự động, tiêu thụ tài nguyên cực thấp, được thiết kế chuyên biệt để chạy liên tục 24/7 trên thiết bị **Android thông qua môi trường Termux** (không yêu cầu root). Tích hợp điều khiển từ xa và tự động tải video lên **Telegram qua giao thức MTProto (hỗ trợ file dung lượng lớn tới 2.000 MB)**.

---

## 🌟 Tính năng nổi bật

- **Tối ưu hóa chuyên sâu cho Android / Termux**:
  - Tự động kích hoạt `termux-wake-lock` khi khởi chạy để giữ CPU hoạt động khi tắt màn hình, tự giải phóng `termux-wake-unlock` khi tắt app.
  - Bộ nhớ RAM tiêu thụ cực thấp (< 70 MB), tự giải phóng heap (`malloc_trim` qua `libc.so`), an toàn tuyệt đối trước cơ chế Android Low Memory Killer (LMK).
  - Tải luồng trực tiếp xuống đĩa theo chunk nhỏ 64 KB, không ngậm hàng trăm MB trong RAM như các tool thông thường.
- **Muxing & Phân đoạn thông minh**:
  - Đóng gói MP4 bằng kỹ thuật stream copy (`-c copy -movflags +faststart`) qua FFmpeg, tải CPU < 2%, không làm nóng máy hay hao pin.
  - Tự động xoay file khi chạm ngưỡng trần an toàn **1.8 GB** để đảm bảo luôn vừa giới hạn upload 2.000 MB của Telegram.
- **Hàng đợi Upload Telegram MTProto (Telethon)**:
  - Tải lên nhóm/kênh Telegram bằng Bot Token qua giao thức MTProto.
  - Hiển thị thanh tiến độ upload thời gian thực (tự động điều tiết tần suất 5s/lần tránh bị Telegram giới hạn FloodWait).
  - Tự động xóa thông báo tiến độ tạm sau khi tải xong và gửi video hoàn chỉnh kèm caption chi tiết.
- **Vòng đời lưu trữ 48 giờ & Xả khẩn cấp**:
  - Video upload thành công được giữ lại trên máy trong 48 giờ (hoặc số giờ tuỳ chỉnh) trước khi tự động dọn dẹp.
  - Cơ chế **Emergency Disk Eviction**: Nếu bộ nhớ máy xuống dưới ngưỡng an toàn (mặc định 400 MB), hệ thống sẽ tự động dọn dẹp sớm các file đã tải cũ nhất để bảo vệ thiết bị.
- **Không bao giờ mất dữ liệu đã ghi**:
   - Hỗ trợ cả hai định dạng Chaturbate phát ra: MPEG-TS và **fMP4/CMAF** (`#EXT-X-MAP` + `.m4s`).
   - Nếu ffmpeg lỗi/thiếu, file thô `.ts`/`.m4s` **được giữ lại** (không xoá) và vẫn được upload lên Telegram; đóng gói lại sau bằng `python3 rec2_app.py remux`.
   - Tự probe lại khi token playlist hết hạn; hỗ trợ proxy HTTP và **SOCKS5** (PySocks).
- **Tự chẩn đoán sự cố ngay trên máy**:
   - `python3 rec2_app.py doctor` kiểm tra ffmpeg, quyền ghi, dung lượng, proxy, kết nối Chaturbate và tải thử playlist/segment thật để chỉ ra chính xác mắt xích bị lỗi.
- **Điều khiển từ xa toàn diện qua Telegram Bot**:
  - Ra lệnh kiểm tra trạng thái, thêm/bớt streamer, bắt đầu hoặc dừng ghi hình trực tiếp từ tin nhắn Telegram.

---

## 📱 Cài đặt trên Android Termux

### Bước 1: Chuẩn bị ứng dụng trên điện thoại
1. Tải và cài đặt **Termux** từ [F-Droid](https://f-droid.org/packages/com.termux/) *(không dùng bản Google Play Store)*.
2. Tải thêm **Termux:API** từ [F-Droid](https://f-droid.org/packages/com.termux.api/).
3. Vào Cài đặt Android -> Ứng dụng -> Termux -> **Tắt tối ưu hóa pin (Chọn Unrestricted / Không hạn chế pin)**.

### Bước 2: Cài đặt môi trường trong Termux
Mở Termux và chạy lần lượt các lệnh sau:

```bash
# 1. Cập nhật hệ thống Termux
pkg update && pkg upgrade -y

# 2. Cài đặt Python, FFmpeg, Git và Termux API
pkg install python ffmpeg git termux-api -y

# 3. Cấp quyền bộ nhớ thiết bị cho Termux (bấm Cho phép trên màn hình)
termux-setup-storage

# 4. Cài đặt thư viện Telethon (+ PySocks nếu dùng proxy SOCKS5)
pip install telethon pysocks

# 5. Tải mã nguồn dự án về máy
git clone https://github.com/nghiart/cb2te.git
cd cb2te
```

---

## ⚙️ Cấu hình hệ thống

Dự án cung cấp sẵn 2 file mẫu: `settings.example.txt` và `favorites.example.txt`.

### 1. Cấu hình Telegram & Hệ thống (`settings.txt`)

Tạo file cấu hình từ file mẫu:
```bash
cp settings.example.txt settings.txt
nano settings.txt
```

Nội dung cấu hình chi tiết:
```ini
# 1. Lấy api_id và api_hash tại: https://my.telegram.org
API_ID = 12345678
API_HASH = "0123456789abcdef0123456789abcdef"

# 2. Bot Token tạo từ @BotFather trên Telegram
BOT_TOKEN = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"

# 3. ID của Group hoặc Channel nhận video (có dấu trừ phía trước)
# Nhớ thêm Bot vào Group/Channel và cấp quyền gửi file
CHAT_ID = -1001234567890

# 4. Telegram User ID của Admin (người có quyền gõ lệnh bot)
ADMIN_IDS = 123456789

# 5. Thời gian giữ video trên máy trước khi tự xóa (tính bằng giờ, mặc định: 48)
RETENTION_HOURS = 48

# 6. Số phòng ghi đồng thời tối đa (khuyến nghị để là 2 trên điện thoại)
MAX_CONCURRENT = 2

# 7. Bộ nhớ trống tối thiểu (MB) để kích hoạt xóa khẩn cấp khi sắp đầy đĩa
MIN_FREE_MB = 400

# 8. Proxy HTTP hoặc SOCKS5 (để trống nếu không dùng)
PROXY = 
```

### 2. Danh sách theo dõi tự động (`favorites.txt`)

Tạo file danh sách theo dõi từ file mẫu:
```bash
cp favorites.example.txt favorites.txt
nano favorites.txt
```

Điền username hoặc link mỗi streamer trên một dòng:
```text
sweet_girl
hot_model_99
https://chaturbate.com/another_streamer/
# streamer_tam_nghi  <-- Dấu # ở đầu dòng để tạm dừng theo dõi
```

---

## 🚀 Hướng dẫn vận hành

### 1. Khởi chạy toàn bộ hệ thống 24/7 (Daemon + Bot + Auto Upload)
Đây là chế độ chính để treo máy:
```bash
python3 rec2_app.py bot
```
Khi chạy lệnh này:
- CPU được giữ hoạt động liên tục khi tắt màn hình (`termux-wake-lock`).
- Hệ thống thăm dò `favorites.txt` cứ mỗi 30–35 giây.
- Streamer online sẽ tự động được ghi hình -> tự mux MP4 -> đưa vào hàng đợi upload lên Telegram -> đặt lịch xóa sau 48h.
- Để tắt ứng dụng an toàn: Nhấn `Ctrl + C` (hệ thống sẽ tự đóng gói phần video đã tải và giải phóng wake-lock).

### 2. Xem trạng thái hệ thống qua Terminal
```bash
python3 rec2_app.py status
```
Hiển thị dung lượng bộ nhớ trống, danh sách streamer đang theo dõi và các video đã upload trong 48 giờ qua.

### 3. Ghi hình trực tiếp 1 streamer từ dòng lệnh (Không qua Bot)
```bash
python3 rec2_app.py rec <username_hoặc_link>
```
*Ví dụ: `python3 rec2_app.py rec sweet_girl`*

### 4. Chẩn đoán khi không thấy video nào được lưu
```bash
python3 rec2_app.py doctor            # kiểm tra toàn bộ pipeline + 1 streamer trong favorites
python3 rec2_app.py doctor --probe 3  # kiểm tra 3 streamer
python3 rec2_app.py doctor sweet_girl # kiểm tra 1 streamer cụ thể
```
In ra bảng **PASS / WARN / FAIL** cho: ffmpeg, ffprobe, quyền ghi + dung lượng, `settings.txt`, proxy, termux-api, Telethon, history, probe Chaturbate, playlist HLS, init segment (fMP4) và tải thử segment.

### 5. Đóng gói lại file thô sau khi sửa ffmpeg
Nếu hệ thống từng giữ lại file `.ts`/`.m4s` (vì mux lỗi lúc ghi):
```bash
python3 rec2_app.py remux                # quét thư mục recordings/
python3 rec2_app.py remux recordings/    # hoặc chỉ định đường dẫn
```

---

## 💬 Danh sách câu lệnh Telegram Bot

Sau khi khởi chạy daemon `python3 rec2_app.py bot`, bạn có thể nhắn tin trực tiếp cho Bot hoặc nhắn trong Group Admin:

| Lệnh | Mô tả |
| :--- | :--- |
| `/status` | Báo cáo chi tiết các phòng đang ghi, RAM, dung lượng trống của máy và tiến độ hàng đợi upload. |
| `/rec <user>` | Bắt đầu theo dõi và ghi hình ngay lập tức một streamer (kể cả khi chưa có trong favorites). |
| `/stop <user>` | Dừng phiên ghi hình của một streamer cụ thể và đóng gói video hiện tại. |
| `/stop all` | Dừng tất cả các phiên ghi hình đang chạy. |
| `/fav` | Xem danh sách các streamer đang được theo dõi trong `favorites.txt`. |
| `/add <user>` | Thêm một streamer mới vào `favorites.txt` và bắt đầu theo dõi. |
| `/del <user>` | Xóa streamer khỏi danh sách `favorites.txt`. |
| `/files` | Xem danh sách video đã tải lên trong 48h qua kèm thời gian đếm ngược trước khi tự xóa. |
| `/help` | Hiển thị bảng trợ giúp danh sách câu lệnh. |

---

## 🧪 Kiểm thử tự động (Unit & Integration Tests)

Hệ thống được phát triển theo phương pháp Test-Driven Development (TDD) với độ bao phủ kiểm thử toàn diện:

```bash
# Chạy toàn bộ 120 bài kiểm thử trong dự án
python3 -m unittest discover -s tests -v
```

---

## 📄 Cấu trúc thư mục dự án

```text
cb2te/
├── bot/
│   ├── handlers.py          # Bộ xử lý lệnh Telegram Bot
│   ├── telegram_bot.py      # Khởi tạo Telethon TelegramClient
│   └── uploader.py          # Hàng đợi upload MTProto 2GB với thanh tiến độ
├── core/
│   ├── android.py           # Android wake-lock, memory trim & storage helpers
│   ├── config.py            # Parser cấu hình settings.txt & favorites.txt
│   ├── doctor.py            # Chẩn đoán pipeline (PASS/WARN/FAIL) cho lệnh `doctor`
│   ├── muxer.py             # FFmpeg stream-copy muxer (3 chiến lược + timeout)
│   ├── net.py               # Header trình duyệt, proxy HTTP/SOCKS5, HTTP client
│   ├── recorder.py          # HLS recorder (MPEG-TS + fMP4/CMAF), probe 3 tầng
│   └── task_manager.py      # Bộ điều phối đa luồng & giới hạn concurrency
├── storage/
│   ├── cleaner.py           # Quét dọn video sau 48h & xả khẩn cấp đĩa trống
│   └── history.py           # Quản lý cơ sở dữ liệu lịch sử file (history.json)
├── docs/                    # Thiết kế + báo cáo phân tích sự cố
├── tests/                   # Toàn bộ test suite tự động (120 unit/e2e tests)
├── rec2_app.py              # File chạy chính (CLI & Daemon 24/7)
├── settings.example.txt     # File cấu hình mẫu
├── favorites.example.txt    # Danh sách streamer mẫu
└── README.md                # Hướng dẫn sử dụng
```

---

## ⚠️ Lưu ý khi vận hành thực tế trên điện thoại
- Cắm sạc và kết nối Wi-Fi ổn định nếu bạn muốn treo máy 24/7.
- Thư mục lưu trữ mặc định là `recordings/` trong thư mục dự án. Bạn có thể đổi vị trí lưu sang bộ nhớ chung hoặc thẻ nhớ ngoài bằng biến môi trường:
  ```bash
  export RECORDINGS_DIR="/sdcard/Movies/Recordings"
  ```
- Nếu thư mục tạm (`$TMPDIR`) và `RECORDINGS_DIR` khác ổ đĩa, hệ thống vẫn an toàn (dùng copy+unlink thay vì `rename`).
- **Chaturbate chặn theo IP (geo-block)**: nếu `doctor` báo `geo_blocked` hoặc `HLS ... HTTP 403`, hãy đặt `PROXY` trong `settings.txt`:
  ```ini
  PROXY = socks5://user:pass@host:1080   # cần: pip install pysocks
  # hoặc
  PROXY = http://user:pass@host:8080
  ```

---

## 🔧 Khắc phục sự cố

Xem báo cáo phân tích đầy đủ: [docs/2026-11-10-live-stream-not-saved-diagnosis.md](docs/2026-11-10-live-stream-not-saved-diagnosis.md)

| Triệu chứng | Nguyên nhân thường gặp | Cách xử lý |
| :--- | :--- | :--- |
| Không có video nào, log im lặng | thiếu ffmpeg / API bị chặn / geo-block | `python3 rec2_app.py doctor` |
| Có file `.ts`/`.m4s` nhưng không có `.mp4` | ffmpeg lỗi hoặc chưa cài | `pkg install ffmpeg` rồi `python3 rec2_app.py remux` |
| Phòng `public` nhưng báo offline | IP bị giới hạn CDN | đặt `PROXY` (ưu tiên cùng quốc gia) |
| Dừng bằng `Ctrl+C` bị mất video | mux chưa xong | bản mới đã chờ tới 120 s; đợi thêm vài giây |
| Proxy SOCKS5 không hoạt động | thiếu PySocks | `pip install pysocks` |
