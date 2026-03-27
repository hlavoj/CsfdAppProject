from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from routers.search import router as search_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-authenticate with Webshare on startup so the first request is fast
    from services.webshare import get_token
    try:
        await get_token()
        print("Webshare authentication successful")
    except Exception as e:
        print(f"Webshare pre-auth failed (will retry on first request): {e}")
    yield


app = FastAPI(
    title="MediaSourceFinder",
    description="Finds direct stream URLs for movies/series via Webshare.cz",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(search_router)
