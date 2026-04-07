import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Union

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import logging

logger = logging.getLogger(__name__)

load_dotenv()

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_http_client: Optional[httpx.AsyncClient] = None

API_VERSION = "1.0"

QOBUZ_BASE_URL = "https://www.qobuz.com/api.json/0.2"

APP_ID: str = os.getenv("QOBUZ_APP_ID")
APP_SECRET: str = os.getenv("QOBUZ_APP_SECRET")
USER_AUTH_TOKEN: str = os.getenv("QOBUZ_USER_AUTH_TOKEN")
COUNTRY_CODE: str = os.getenv("COUNTRY_CODE")
APPLE_TOKEN :str = os.getenv("APPLE_TOKEN")

# ---------------------------------------------------------------------------
# App lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        http2=True,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=8.0, pool=12.0),
        limits=httpx.Limits(
            max_keepalive_connections=200,
            max_connections=300,
            keepalive_expiry=30.0,
        ),
    )
    try:
        yield
    finally:
        if _http_client:
            await _http_client.aclose()


app = FastAPI(
    title="Qobuz-RestAPI",
    version=API_VERSION,
    description="Qobuz Music Proxy",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def get_http_client() -> httpx.AsyncClient:
    if _http_client is None:
        return httpx.AsyncClient(http2=True)
    return _http_client


def _qobuz_headers() -> dict:
    """Common headers required by Qobuz API."""
    headers = {
        "X-App-Id": APP_ID,
    }
    if USER_AUTH_TOKEN:
        headers["X-User-Auth-Token"] = USER_AUTH_TOKEN
    return headers

async def qobuz_get(
    endpoint: str,
    params: Optional[dict] = None,
    require_auth: bool = True,
) -> dict:
    'Perform a GET request against the Qobuz API.'
    if not APP_ID:
        raise HTTPException(status_code=500, detail="QOBUZ_APP_ID not configured")
    if require_auth and not USER_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="QOBUZ_USER_AUTH_TOKEN not configured")

    url = f"{QOBUZ_BASE_URL}/{endpoint.lstrip('/')}"
    client = await get_http_client()

    try:
        resp = await client.get(url, headers=_qobuz_headers(), params=params or {})
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(status_code=401, detail="Qobuz authentication failed – check QOBUZ_USER_AUTH_TOKEN")
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="Resource not found on Qobuz")
        if e.response.status_code == 429:
            raise HTTPException(status_code=429, detail="Qobuz rate limit exceeded")
        logger.error("Upstream Qobuz error %s %s", e.response.status_code, url, exc_info=e)
        raise HTTPException(status_code=e.response.status_code, detail="Upstream Qobuz API error")
    except httpx.TimeoutException:
        raise HTTPException(status_code=429, detail="Qobuz upstream timeout")
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Connection error to Qobuz: {e}")


def _build_file_url_secret(track_id: int, format_id: int, unix_ts: int) -> str:
    """
    Build the request signature required for stream/download URL generation.
    Qobuz signs the request with:
        MD5( "trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{unix_ts}{secret}" )
    """
    data = f"trackgetFileUrlformat_id{format_id}intentstreamtrack_id{track_id}{unix_ts}{APP_SECRET}"
    return hashlib.md5(data.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
async def index():
    return {
        "version": API_VERSION,
        "service": "Qobuz-RestAPI",
        "Author":"Esposito",
        "docs": "/docs",
    }


# ------------------------------------------------------------------
# User login (exchange credentials for user_auth_token)
# ------------------------------------------------------------------

"""
@app.get("/login/")
async def login(email: str = Query(...), password: str = Query(...)):
    '''
    Obtain a user_auth_token from Qobuz.
    The MD5 of the password must be sent (Qobuz requirement).
    Returns the full user object including the auth token.
    '''
    if not APP_ID:
        raise HTTPException(status_code=500, detail="QOBUZ_APP_ID not configured")

    params = {
        "username": email,
        "password": hashlib.md5(password.encode()).hexdigest(),
        "app_id": APP_ID,
    }
    data = await qobuz_get("user/login", params=params, require_auth=False)
    return {"version": API_VERSION, "data": data}"""


# ------------------------------------------------------------------
# User info
# ------------------------------------------------------------------

"""
@app.get("/user/")
async def get_user():
   'Fetch authenticated user info including subscription details.'
    data = await qobuz_get("user/get")
    return {"version": API_VERSION, "data": data}"""


# ------------------------------------------------------------------
# Track info
# ------------------------------------------------------------------


@app.get("/info/")
async def get_track_info(id: int):
    """Fetch metadata for a single track."""
    data = await qobuz_get("track/get", params={"track_id": id})
    return {"version": API_VERSION, "data": data}


# ------------------------------------------------------------------
# Track stream URL
# ------------------------------------------------------------------

@app.get("/track/")
async def get_track_url(
    id: int,
    quality: int = Query(
        default=27,
        description=(
            "Qobuz format_id: 5=MP3 320, 6=FLAC 16bit, 7=FLAC 24bit ≤96kHz, "
            "27=FLAC 24bit >96kHz (Hi-Res)"
        ),
    ),
):
    'Return the stream/download URL for a track.'
    if not APP_SECRET:
        raise HTTPException(status_code=500, detail="QOBUZ_APP_SECRET not configured (needed for URL signing)")

    unix_ts = int(time.time())
    sig = _build_file_url_secret(id, quality, unix_ts)

    params = {
        "track_id": id,
        "format_id": quality,
        "intent": "stream",
        "request_ts": unix_ts,
        "request_sig": sig,
    }
    data = await qobuz_get("track/getFileUrl", params=params)
    return {"version": API_VERSION, "data": data}

# ------------------------------------------------------------------
# Search
# ------------------------------------------------------------------


@app.get("/search/")
async def search(
    s: Optional[str] = Query(default=None, description="Search tracks"),
    a: Optional[str] = Query(default=None, description="Search artists"),
    al: Optional[str] = Query(default=None, description="Search albums"),
    p: Optional[str] = Query(default=None, description="Search playlists"),
    limit: int = Query(default=25, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """
    Unified search endpoint.
    Pass exactly one of: s (tracks), a (artists), al (albums), p (playlists).
    """
    type_map = {
        "tracks": s,
        "artists": a,
        "albums": al,
        "playlists": p,
    }

    query, search_type = next(
        ((v, k) for k, v in type_map.items() if v), (None, None)
    )
    if query is None:
        raise HTTPException(status_code=400, detail="Provide one of s, a, al, or p")

    params = {
        "query": query,
        "type": search_type,
        "limit": limit,
        "offset": offset,
    }
    data = await qobuz_get("catalog/search", params=params)
    return {"version": API_VERSION, "data": data}


# ------------------------------------------------------------------
# Album
# ------------------------------------------------------------------


@app.get("/album/")
async def get_album(id: str = Query(..., description="Album ID")):
    """Fetch album metadata including full track list."""
    data = await qobuz_get("album/get", params={"album_id": id})
    return {"version": API_VERSION, "data": data}


# ------------------------------------------------------------------
# Artist
# ------------------------------------------------------------------


@app.get("/artist/")
async def get_artist(
    id: int = Query(..., description="Artist ID"),
    limit: int = Query(default=25, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    extra: Optional[str] = Query(
        default="albums",
        description="Extra info to include: albums, playlists, tracks_appears_on, albums_with_last_release, focus_all",
    ),
):
    """Fetch artist metadata plus related content."""
    params = {
        "artist_id": id,
        "limit": limit,
        "offset": offset,
        "extra": extra,
    }
    data = await qobuz_get("artist/get", params=params)
    return {"version": API_VERSION, "data": data}


# ------------------------------------------------------------------
# Playlist
# ------------------------------------------------------------------


@app.get("/playlist/")
async def get_playlist(
    id: int = Query(..., description="Playlist ID"),
    limit: int = Query(default=500, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """Fetch playlist metadata plus tracks."""
    params = {
        "playlist_id": id,
        "limit": limit,
        "offset": offset,
        "extra": "tracks",
    }
    data = await qobuz_get("playlist/get", params=params)
    return {"version": API_VERSION, "data": data}


# ------------------------------------------------------------------
# User playlists
# ------------------------------------------------------------------

"""
@app.get("/user/playlists/")
async def get_user_playlists(
    user_id: Optional[int] = Query(default=None, description="User ID (omit for self)"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    'List playlists owned or favourited by the authenticated user (or another user).'
    params: dict = {"limit": limit, "offset": offset}
    if user_id:
        params["user_id"] = user_id
    data = await qobuz_get("playlist/getUserPlaylists", params=params)
    return {"version": API_VERSION, "data": data}"""


# ------------------------------------------------------------------
# Favourites
# ------------------------------------------------------------------

"""
@app.get("/favorites/")
async def get_favorites(
    type: str = Query(
        default="tracks",
        description="Type: tracks, albums, or artists",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    'Fetch the authenticated user's favourites.'
    allowed = {"tracks", "albums", "artists"}
    if type not in allowed:
        raise HTTPException(status_code=400, detail=f"type must be one of {allowed}")

    params = {"type": type, "limit": limit, "offset": offset}
    data = await qobuz_get("favorite/getUserFavorites", params=params)
    return {"version": API_VERSION, "data": data}"""


# ------------------------------------------------------------------
# Recommendations / similar content
# ------------------------------------------------------------------


@app.get("/recommendations/")
async def get_recommendations(
    album_id: Optional[str] = Query(default=None, description="Album ID"),
    artist_id: Optional[int] = Query(default=None, description="Artist ID"),
    limit: int = Query(default=20, ge=1, le=100),
):
    """
    Fetch similar albums for a given album or artist.
    Qobuz exposes `catalog/getFeatured` for editorial picks; use album/get similarAlbums
    relation if available, otherwise fall back to `catalog/getFeatured`.
    """
    if album_id:
        data = await qobuz_get(
            "album/get",
            params={"album_id": album_id, "extra": "similar_albums"},
        )
        return {"version": API_VERSION, "data": data}

    if artist_id:
        data = await qobuz_get(
            "artist/get",
            params={
                "artist_id": artist_id,
                "extra": "albums",
                "limit": limit,
            },
        )
        return {"version": API_VERSION, "data": data}

    raise HTTPException(status_code=400, detail="Provide album_id or artist_id")


# ------------------------------------------------------------------
# Cover art
# ------------------------------------------------------------------


@app.get("/cover/")
async def get_cover(
    album_id: Optional[str] = Query(default=None, description="Album ID"),
    track_id: Optional[int] = Query(default=None, description="Track ID (resolves album cover)"),
    q: Optional[str] = Query(default=None, description="Search query"),
    size: int = Query(
        default=600,
        description="Image size in px – Qobuz supports: 50, 230, 600",
    ),
):
    """
    Return cover art URLs for an album, track, or search query.
    Qobuz image URL pattern:
        https://static.qobuz.com/images/covers/{hash}_{size}.jpg
    """

    def build_cover(image_hash: str, title: Optional[str] = None, item_id=None):
        base = f"https://static.qobuz.com/images/covers/{image_hash}"
        return {
            "id": item_id,
            "name": title,
            "50": f"{base}_50.jpg",
            "230": f"{base}_230.jpg",
            "600": f"{base}_600.jpg",
            "max": f"{base}_max.jpg",
        }

    if album_id:
        data = await qobuz_get("album/get", params={"album_id": album_id})
        image = data.get("image", {})
        img_hash = (image.get("large") or "").split("/covers/")[-1].split("_")[0]
        if not img_hash:
            raise HTTPException(status_code=404, detail="Cover not found")
        return {"version": API_VERSION, "covers": [build_cover(img_hash, data.get("title"), data.get("id"))]}

    if track_id:
        data = await qobuz_get("track/get", params={"track_id": track_id})
        album = data.get("album", {})
        image = album.get("image", {})
        img_hash = (image.get("large") or "").split("/covers/")[-1].split("_")[0]
        if not img_hash:
            raise HTTPException(status_code=404, detail="Cover not found")
        return {"version": API_VERSION, "covers": [build_cover(img_hash, album.get("title"), album.get("id"))]}

    if q:
        data = await qobuz_get(
            "catalog/search",
            params={"query": q, "type": "albums", "limit": 10},
        )
        albums = data.get("albums", {}).get("items", [])
        covers = []
        for alb in albums:
            image = alb.get("image", {})
            img_hash = (image.get("large") or "").split("/covers/")[-1].split("_")[0]
            if img_hash:
                covers.append(build_cover(img_hash, alb.get("title"), alb.get("id")))
        if not covers:
            raise HTTPException(status_code=404, detail="Cover not found")
        return {"version": API_VERSION, "covers": covers}

    raise HTTPException(status_code=400, detail="Provide album_id, track_id, or q")


# ------------------------------------------------------------------
# Lyrics  (Qobuz does not provide a native lyrics endpoint;
#           we delegate to the Qobuz track metadata which may include
#           a `lyrics` field, otherwise return 404)
# ------------------------------------------------------------------


@app.get("/lyrics/")
async def get_lyrics(id: int):
    """Fetch track metadata and extract embedded lyrics if present."""
    data = await qobuz_get("track/get", params={"track_id": id, "extra": "lyrics"})
    lyrics = data.get("lyrics")
    if not lyrics:
        raise HTTPException(status_code=404, detail="Lyrics not found for this track")
    return {"version": API_VERSION, "lyrics": lyrics}

# ------------------------------------------------------------------
# Catalogue / featured / new releases
# ------------------------------------------------------------------

@app.get("/featured/")
async def get_featured(
    type: str = Query(
        default="new-releases-full",
        description=(
            "Type: new-releases-full, press-awards-full, editor-picks, "
            "most-streamed, best-sellers"
        ),
    ),
    genre_id: Optional[int] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    country: str = Query(default=COUNTRY_CODE, description="Country passata dal frontend"),
):
    """Fetch Qobuz editorial featured albums."""

    # FIX 1: Rimuoviamo "store_id" perché Qobuz blocca la richiesta (Errore 400)
    # se cerchiamo di forzare un paese diverso da quello dell'account loggato.
    params: dict = {"type": type, "limit": limit, "offset": offset}

    if genre_id:
        params["genre_id"] = genre_id

    # FIX 2: Usiamo "album/getFeatured" invece del vecchio "catalog/getFeatured"
    data = await qobuz_get("album/getFeatured", params=params)
    return {"version": API_VERSION, "data": data}


# ------------------------------------------------------------------
# Genres
# ------------------------------------------------------------------


@app.get("/genres/")
async def get_genres(parent_id: Optional[int] = Query(default=None)):
    """List available genres (optionally filtered by parent genre)."""
    params: dict = {}
    if parent_id is not None:
        params["parent_id"] = parent_id
    data = await qobuz_get("genre/list", params=params, require_auth=False)
    return {"version": API_VERSION, "data": data}



# ------------------------------------------------------------------
# Apple Music Search Proxy (Bypass CORS per iTunes)
# ------------------------------------------------------------------
@app.get("/apple/search")
async def get_apple_search(term: str, country: str = "it", limit: str="10"):
    """Proxy per la ricerca su iTunes, bypassa il blocco CORS del browser."""
    # Usiamo l'endpoint globale passandogli il paese come parametro

    url = f"https://api.music.apple.com/v1/catalog/{country}/search"
    
    params = {
        "term": term,
        "limit": limit
    }

    headers = {
        "Authorization": f"Bearer {APPLE_TOKEN}",
        "Origin": "https://music.apple.com",# Facciamo credere ad Apple di essere il loro sito!
        "Referer": "https://music.apple.com/"
    }
    
    client = await get_http_client()
    try:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Errore ricerca iTunes: {e}")
        return {"results": []}


# ------------------------------------------------------------------
# Apple Music Animated Artwork Proxy
# ------------------------------------------------------------------

@app.get("/apple/animated-art")
async def get_apple_animated_art(album_id: str, country: str = "it"):
    """Fetch animated artwork directly from Apple Music bypassing browser CORS."""
    apple_url = f"https://amp-api.music.apple.com/v1/catalog/{country}/albums/{album_id}?extend=editorialVideo"
    
    headers = {
        "Authorization": f"Bearer {APPLE_TOKEN}",
        "Origin": "https://music.apple.com", # Facciamo credere ad Apple di essere il loro sito!
        "Referer": "https://music.apple.com/"
    }
    
    client = await get_http_client()
    try:
        resp = await client.get(apple_url, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# ------------------------------------------------------------------
# Apple Music Playlist Proxy
# ------------------------------------------------------------------
@app.get("/apple/playlist")
async def get_apple_playlist(
    playlist_id: str,
    country: str = "it",
    next_url: Optional[str] = Query(default=None, description="URL pagina successiva (paginazione)")
):
    """Proxy per fetch playlist Apple Music, bypassa CORS e vincolo Origin."""
    
    if next_url:
        # Paginazione: usiamo l'URL diretto fornito da Apple
        url = next_url if next_url.startswith("http") else f"https://api.music.apple.com{next_url}"
    else:
        url = f"https://api.music.apple.com/v1/catalog/{country}/playlists/{playlist_id}"

    headers = {
        "Authorization": f"Bearer {APPLE_TOKEN}",
        "Origin": "https://music.apple.com",
        "Referer": "https://music.apple.com/"
    }

    client = await get_http_client()
    try:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=f"Apple API error: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":

    uvicorn.run(app, host="0.0.0.0", port=7979)










