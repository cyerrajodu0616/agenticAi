"""Local web UI entrypoint: python -m assistant.web"""
import uvicorn

from assistant import config
from assistant.web.app import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=config.WEB_PORT)
