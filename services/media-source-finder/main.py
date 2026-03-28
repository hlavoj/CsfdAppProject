from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()

import logging
import logging.handlers
from pathlib import Path

# --- Logger setup (must be before router imports) ---
LOG_PATH = Path(__file__).parent / "logs" / "timing.log"
LOG_PATH.parent.mkdir(exist_ok=True)

logger = logging.getLogger("timing")
logger.setLevel(logging.DEBUG)
logger.propagate = False

_file_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_file_handler)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_console_handler)

# --- App ---
from fastapi import FastAPI
from routers.search import router as search_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services.webshare import get_token
    try:
        await get_token()
        logger.info("Webshare authentication successful")
    except Exception as e:
        logger.warning(f"Webshare pre-auth failed (will retry on first request): {e}")
    yield


app = FastAPI(
    title="MediaSourceFinder",
    description="Finds direct stream URLs for movies/series via Webshare.cz",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(search_router)
