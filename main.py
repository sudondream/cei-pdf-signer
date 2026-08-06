#!/usr/bin/env python3
"""
CEI PDF Signer - Desktop Application
Wraps the Flask web app in a native macOS window using PyWebView
"""

import sys
import os
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
from app import app


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


def main():
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

    # Start the GUI - the func runs in a separate thread
    webview.start(
        func=start_app,
        debug=False,
        private_mode=False,  # Allow cookies/storage
    )


if __name__ == '__main__':
    main()
