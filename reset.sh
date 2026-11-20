#!/bin/bash
# ==============================================================================
# Reset script for cb2te (Chaturbate Telegram Recorder)
# Supports Android Termux ($PREFIX) and macOS / Linux.
# Usage:
#   ./reset.sh           # Stop all processes and clean temporary junk files
#   ./reset.sh restart   # Stop, clean, and restart daemon in background
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "🛑 [1/4] Stopping all running recorder processes..."

# Find and kill 1st.py / rec2_app.py
PIDS=$(pgrep -f "python3.*(1st\.py|rec2_app\.py)" || true)
if [ -n "$PIDS" ]; then
    echo "Found recorder PID(s): $PIDS"
    kill $PIDS 2>/dev/null || true
    sleep 2
    # Force kill if still lingering
    REMAINING=$(pgrep -f "python3.*(1st\.py|rec2_app\.py)" || true)
    if [ -n "$REMAINING" ]; then
        echo "Force killing remaining PID(s): $REMAINING"
        kill -9 $REMAINING 2>/dev/null || true
    fi
else
    echo "No running 1st.py daemon found."
fi

# Kill any orphaned ffmpeg recording subprocesses spawned by cb2te
FFMPEG_PIDS=$(pgrep -f "ffmpeg.*chaturbate" || true)
if [ -n "$FFMPEG_PIDS" ]; then
    echo "Terminating orphaned ffmpeg processes: $FFMPEG_PIDS"
    kill $FFMPEG_PIDS 2>/dev/null || true
    sleep 1
    kill -9 $FFMPEG_PIDS 2>/dev/null || true
fi

echo "🧹 [2/4] Cleaning temporary / incomplete recording artifacts..."

# Clean unfinished spools and parts in project and temp directory
find . -maxdepth 2 \( -name "*.spool" -o -name "*.part" -o -name "*.part.mp4" \) -delete 2>/dev/null || true
if [ -n "$TMPDIR" ] && [ -d "$TMPDIR" ]; then
    find "$TMPDIR" -maxdepth 1 \( -name "*_*.spool" -o -name "*_*.part" \) -delete 2>/dev/null || true
fi

# Clean Android Download incomplete parts
if [ -d "$HOME/storage/downloads/CB2TE" ]; then
    find "$HOME/storage/downloads/CB2TE" -maxdepth 3 \( -name "*.part" -o -name "*.part.mp4" -o -name "*.spool" \) -delete 2>/dev/null || true
fi
if [ -d "/sdcard/Download/CB2TE" ]; then
    find "/sdcard/Download/CB2TE" -maxdepth 3 \( -name "*.part" -o -name "*.part.mp4" -o -name "*.spool" \) -delete 2>/dev/null || true
fi

# Clean SQLite journal locks if bot crashed
rm -f *.session-journal 2>/dev/null || true

# Clean __pycache__
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "✅ [3/4] Reset and cleanup complete!"

# Check if restart argument provided
if [ "$1" = "restart" ] || [ "$1" = "-r" ]; then
    echo "🚀 [4/4] Restarting daemon in background..."
    nohup python3 -u 1st.py bot > daemon.log 2>&1 </dev/null &
    NEW_PID=$!
    sleep 2
    if kill -0 "$NEW_PID" 2>/dev/null; then
        echo "✅ Bot daemon started successfully with PID: $NEW_PID"
        echo "📄 Logs: tail -f daemon.log"
    else
        echo "❌ Bot failed to start. Check daemon.log:"
        cat daemon.log | tail -n 20
        exit 1
    fi
else
    echo "ℹ️ [4/4] System is stopped. Run './reset.sh restart' or 'python3 1st.py bot' to start."
fi

