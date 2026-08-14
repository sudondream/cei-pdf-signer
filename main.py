#!/usr/bin/env python3
"""
CEI PDF Signer - Desktop Application
Wraps the Flask web app in a native macOS window using PyWebView
"""

import sys
import os
import signal
import threading
import socket
import time
# Ensure we can find our modules when running as a bundled app
if getattr(sys, 'frozen', False):
    # Running as bundled app
    bundle_dir = os.path.dirname(sys.executable)
    # For py2app, resources are in ../Resources
    resources_dir = os.path.join(os.path.dirname(bundle_dir), 'Resources')
    if os.path.exists(resources_dir):
        os.chdir(resources_dir)
        sys.path.insert(0, resources_dir)

import webview
from app import app, driver_busy, wait_for_driver


# Loading screen HTML - shown immediately while Flask starts
LOADING_HTML = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>CEI PDF Signer</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            color: #fff;
        }
        .logo {
            font-size: 2.5em;
            font-weight: bold;
            background: linear-gradient(90deg, #00d4ff, #0099ff);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin-bottom: 40px;
        }
        .spinner {
            width: 50px;
            height: 50px;
            border: 3px solid rgba(0, 212, 255, 0.1);
            border-top-color: #00d4ff;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }
        .text {
            margin-top: 25px;
            color: #888;
            font-size: 14px;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="logo">CEI PDF Signer</div>
    <div class="spinner"></div>
    <div class="text">Se incarca...</div>
</body>
</html>
'''


def find_free_port():
    """Find a free port to run the server on"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


def start_server(port):
    """Start the Flask server in a background thread"""
    # Disable Flask's reloader and debug mode for production
    app.run(
        host='127.0.0.1',
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True
    )


def wait_for_server(port, timeout=30):
    """Wait for the server to be ready"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(('127.0.0.1', port))
                return True
        except ConnectionRefusedError:
            time.sleep(0.1)
    return False


TERMINATING_SIGNALS = {signal.SIGTERM, signal.SIGINT}


def _on_signal():
    """Drain in-flight card work, then exit.

    Dying inside a PKCS#11 call wedges the Idemia driver until the Mac is
    restarted - for every process on the machine, not just this one. Closing
    the window is handled separately; this covers being killed, which is what
    scripts/verify-release-archive.sh does to the bundle it just built. With a
    card in the reader that lands mid-enumeration, so the release check was
    manufacturing the very wedge it exists to catch.

    Waits in a dedicated thread rather than a signal.signal handler. Python
    runs such handlers on the main thread, and ours never leaves pywebview's
    Cocoa event loop - so the handler never fires while the default
    disposition has already been replaced. Measured: that combination made
    the app immune to SIGTERM, still serving requests a minute later. Blocking
    the signals everywhere and waiting for them here works whatever the main
    thread is doing.

    Exits with os._exit: SystemExit raised out here cannot unwind the main
    thread either. Draining the driver is the only cleanup that matters, and
    it has already happened by then. Bounded, because a wedged call never
    returns and refusing to die would be worse.
    """
    signal.sigwait(list(TERMINATING_SIGNALS))
    wait_for_driver()
    os._exit(0)


def main():
    # Must happen before any other thread starts, so they inherit the mask.
    signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATING_SIGNALS)
    threading.Thread(target=_on_signal, daemon=True, name='signal-watch').start()

    # Find a free port
    port = find_free_port()

    # Create window with loading screen immediately (before Flask starts)
    window = webview.create_window(
        title='CEI PDF Signer',
        html=LOADING_HTML,
        width=1280,
        height=800,
        min_size=(1000, 600),
        resizable=True,
        confirm_close=True,
        text_select=True,
    )

    def start_app():
        """Start Flask and navigate to it once ready"""
        # Start Flask server in background thread
        server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
        server_thread.start()

        # Wait for server to be ready
        if wait_for_server(port):
            # Navigate to the Flask app
            window.load_url(f'http://127.0.0.1:{port}')
        else:
            # Show error if server failed
            window.load_html('''
                <html><body style="background:#1a1a2e;color:#ff6464;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;">
                <div style="text-align:center"><h2>Error</h2><p>Server failed to start. Please restart the application.</p></div>
                </body></html>
            ''')

    # Quitting while the card driver is mid-call is what leaves it wedged
    # until the Mac is restarted - not just for this app, for every process
    # that touches the card afterwards. So hold the close, tell the user why,
    # and let the call finish. A wedged call never will, hence the deadline.
    closing_started = threading.Event()

    def on_closing():
        if not driver_busy() or closing_started.is_set():
            return True

        closing_started.set()

        def finish():
            try:
                window.evaluate_js('showClosingNotice()')
            except Exception:
                pass  # cosmetic only; the wait matters more than the notice
            wait_for_driver()
            window.destroy()

        threading.Thread(target=finish, daemon=True).start()
        return False   # cancel this close; finish() closes for real

    window.events.closing += on_closing

    # Start the GUI - the func runs in a separate thread
    webview.start(
        func=start_app,
        debug=False,
        private_mode=False,  # Allow cookies/storage
    )


if __name__ == '__main__':
    main()
