"""Entry point. `python app.py` runs the dev server on http://localhost:8080;
`gunicorn app:app` (see Procfile) imports `app` from here. The code lives in scooter_api/.
"""
import logging
import os

from scooter_api import create_app

app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    app.run("localhost", int(os.environ.get("PORT", 8080)))
