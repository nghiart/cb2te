#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CB2TE - Trình cài đặt tự động cho Termux (Android, không cần root)
#
# Cách dùng:
#   cd ~/cb2te
#   bash install_termux.sh
#
# Script chỉ cài đúng những gì cần thiết, có thể chạy lại nhiều lần.
# ---------------------------------------------------------------------------
set -uo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;36m'
NC='\033[0m'

step() { printf "\n${BLUE}==> %s${NC}\n" "$1"; }
ok()   { printf "${GREEN}   ✔ %s${NC}\n" "$1"; }
warn() { printf "${YELLOW}   ! %s${NC}\n" "$1"; }
fail() { printf "${RED}   ✘ %s${NC}\n" "$1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || {
    fail "Không vào được thư mục dự án: $SCRIPT_DIR"
    exit 1
}

printf "${GREEN}"
cat <<'BANNER'
  ____ ____  ____ _____
 / ___| __ )|___ /_   _|___
| |   |  _ \  |_ \ | |/ _ \
| |___| |_) |___) || |  __/
 \____|____/|____/ |_|\___|
  Chaturbate -> Telegram Recorder (Termux)
BANNER
printf "${NC}"

if ! command -v pkg >/dev/null 2>&1; then
    warn "Không tìm thấy 'pkg' — script này dành cho Termux."
    warn "Trên Linux/PC hãy cài thủ công: python3, ffmpeg, pip install -r requirements.txt"
fi

step "1/5 Cập nhật Termux (có thể mất vài phút)"
if command -v pkg >/dev/null 2>&1; then
    pkg update -y && pkg upgrade -y || warn "pkg update/upgrade lỗi — vẫn tiếp tục"
    ok "Đã cập nhật gói hệ thống"
fi

step "2/5 Cài Python, FFmpeg, Git, Termux:API"
if command -v pkg >/dev/null 2>&1; then
    pkg install -y python ffmpeg git termux-api || {
        fail "Cài gói thất bại. Kiểm tra mạng rồi chạy lại script."
        exit 1
    }
fi
ok "Python: $(python3 --version 2>&1)"
if command -v ffmpeg >/dev/null 2>&1; then
    ok "FFmpeg: $(ffmpeg -version 2>/dev/null | head -n 1)"
else
    fail "Chưa có ffmpeg — chạy: pkg install ffmpeg"
fi

step "3/5 Cấp quyền bộ nhớ (bấm Cho phép trên màn hình)"
if command -v termux-setup-storage >/dev/null 2>&1; then
    termux-setup-storage || warn "Không cấp quyền bộ nhớ (không bắt buộc nếu lưu trong thư mục Termux)"
else
    warn "Không có termux-setup-storage — bỏ qua"
fi

step "4/5 Cài thư viện Python"
python3 -m pip install --upgrade pip >/dev/null 2>&1 || warn "Không nâng cấp được pip"
if [ -f requirements.txt ]; then
    python3 -m pip install --upgrade -r requirements.txt || {
        fail "Cài thư viện thất bại. Thử: pip install telethon pysocks python-socks[asyncio]"
        exit 1
    }
else
    python3 -m pip install --upgrade telethon pysocks "python-socks[asyncio]" || exit 1
fi
ok "Đã cài telethon + pysocks + python-socks"

step "5/5 Tạo file cấu hình"
if [ ! -f settings.txt ]; then
    cp settings.example.txt settings.txt
    ok "Đã tạo settings.txt (cần điền thông tin Telegram)"
else
    ok "settings.txt đã tồn tại — giữ nguyên"
fi
if [ ! -f favorites.txt ]; then
    cp favorites.example.txt favorites.txt
    ok "Đã tạo favorites.txt"
else
    ok "favorites.txt đã tồn tại — giữ nguyên"
fi

step "Kiểm tra hệ thống"
python3 rec2_app.py doctor --probe 0 || true

printf "\n${GREEN}==============================================${NC}\n"
printf "${GREEN} Cài đặt xong! Các bước tiếp theo:${NC}\n"
printf "${GREEN}==============================================${NC}\n"
cat <<'NEXT'
 1. Mở file cấu hình và điền thông tin Telegram:
       nano settings.txt
    (API_ID, API_HASH, BOT_TOKEN, CHAT_ID, ADMIN_IDS)

 2. Thêm streamer muốn theo dõi:
       nano favorites.txt

 3. Kiểm tra lại (nên thấy toàn PASS):
       python3 rec2_app.py doctor

 4. Chạy 24/7:
       python3 rec2_app.py bot

 5. Tắt an toàn: nhấn Ctrl + C (đợi nó đóng gói xong file đang ghi)

 Tài liệu đầy đủ: README.md
NEXT
printf "\n"
