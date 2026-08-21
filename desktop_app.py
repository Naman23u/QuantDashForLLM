import os
import sys
import time
import threading
import multiprocessing
import traceback
import webview

def log_debug(msg):
    try:
        base = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, 'app_startup.log'), 'a', encoding='utf-8') as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass

try:
    from app import app
except Exception as e:
    log_debug(f"FATAL: Failed to import app: {e}\n{traceback.format_exc()}")

class Api:
    def select_script(self):
        window = webview.windows[0]
        result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False, file_types=('Python Files (*.py)', 'All Files (*.*)'))
        if result and len(result) > 0:
            return result[0]
        return None

    def select_data(self):
        window = webview.windows[0]
        result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=('Data Files (*.parquet;*.csv)', 'All Files (*.*)'))
        if result and len(result) > 0:
            return list(result)
        return None

def start_server():
    try:
        log_debug("Starting Flask server on port 5050...")
        app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)
    except Exception as e:
        log_debug(f"FATAL: Flask server crashed: {e}\n{traceback.format_exc()}")

if __name__ == '__main__':
    # CRITICAL: Freeze support prevents child processes in ProcessPoolExecutor
    # from spawning recursive Flask servers / UI windows (fork bomb on Windows).
    multiprocessing.freeze_support()
    log_debug(f"QuantDash main process started (PID: {os.getpid()})")

    # Start the Flask server in a background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    
    api = Api()
    try:
        log_debug("Creating webview window...")
        webview.create_window(
            'QuantDash - Strategy Factory', 
            'http://127.0.0.1:5050',
            js_api=api,
            width=1280,
            height=800,
            background_color='#0f1115'
        )
        log_debug("Starting webview event loop...")
        webview.start(debug=False, private_mode=True)
        log_debug("Webview event loop terminated cleanly.")
    except Exception as e:
        log_debug(f"FATAL: Webview error: {e}\n{traceback.format_exc()}")


