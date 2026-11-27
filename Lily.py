"""
Lily.py

Entry point only. All real orchestration logic (Ollama, GPT-SoVITS, VMC,
Discord, Twitch, memory, mood, animation) lives in lily_bot.py - this file
just keeps "Lily.py" as the stable thing you actually run, so any shortcuts
or .bat files keep working.
"""

import asyncio
import io
import os
import signal
import sys

# Force UTF-8 console output so emoji in print statements (here and in
# every module this imports, like ws_animation_server.py) don't crash
# with UnicodeEncodeError on Windows' default cp1252 console.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

print(f"Python executable: {sys.executable}")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EXPECTED_VENV = os.path.join(SCRIPT_DIR, ".venv", "Scripts", "python.exe")

# Warn (don't exit) if not running from the project's .venv, so conda,
# differently-named venvs, and other setups still work.
if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(
    os.path.abspath(EXPECTED_VENV)
):
    print(
        f"⚠️  Not running from the project's .venv ({EXPECTED_VENV}).\n"
        f"   Continuing anyway. If imports fail, activate your virtual "
        f"environment and run:\n"
        f"   pip install -r requirements.txt\n"
    )

try:
    import lily_bot

    main_loop = getattr(lily_bot, "main_loop", None)
    graceful_shutdown_handler = getattr(lily_bot, "graceful_shutdown_handler", None)

    if main_loop is None or graceful_shutdown_handler is None:
        raise AttributeError("Missing: main_loop or graceful_shutdown_handler")
except (ModuleNotFoundError, ImportError) as e:
    print(f"\n❌ Failed to import from lily_bot: {e}\n")
    print(f"   Fix with: {sys.executable} -m pip install -r requirements.txt\n")
    input("Press Enter to close...")
    sys.exit(1)
except AttributeError as e:
    print(f"\n❌ Failed to import from lily_bot - missing symbols: {e}\n")
    print("   Ensure lily_bot.py defines: main_loop, graceful_shutdown_handler\n")
    input("Press Enter to close...")
    sys.exit(1)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, graceful_shutdown_handler)
    if sys.platform == "win32":
        try:
            signal.signal(signal.SIGBREAK, graceful_shutdown_handler)
        except (AttributeError, ValueError):
            pass
    try:
        asyncio.run(main_loop())
    except Exception as e:
        import traceback

        print(f"An unexpected error occurred: {e}")
        traceback.print_exc()
        input("Press Enter to close terminal...")