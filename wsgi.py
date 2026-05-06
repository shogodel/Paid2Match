"""WSGI entry point for uWSGI.

This module creates the Flask application instance for WSGI servers.
uWSGI imports 'app' from this module to serve the application.

Environment Variables:
    FLASK_ENV: 'production' (default), 'development', 'staging', 'testing'
"""
import os
import sys
from app import create_app

try:
    app = create_app(os.getenv('FLASK_ENV', 'production'))
    if app is None:
        raise RuntimeError("Failed to create Flask application")
except Exception as e:
    print(f"Error creating Flask app: {e}", file=sys.stderr)
    sys.exit(1)

if __name__ == '__main__':
    # Development server only (not for production)
    port = int(os.getenv('FLASK_PORT', 8000))
    app.run(host='0.0.0.0', port=port, debug=app.config.get('DEBUG', False))
