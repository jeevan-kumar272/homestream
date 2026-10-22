import hashlib
import mimetypes
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from auth import auth_middleware, router as auth_router
from db import init_db, open_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()       # create the database tables on first start
    sync_library()  # fill the movies table from the media/movies folder
    yield


app = FastAPI(title="HomeStream", lifespan=lifespan)

# Check the sign-in cookie before every request, then add the account routes
app.middleware("http")(auth_middleware)
app.include_router(auth_router)

# Folder locations, based on where this file lives
BASE_DIR = Path(__file__).resolve().parent
MOVIES_DIR = BASE_DIR / "media" / "movies"
STATIC_DIR = BASE_DIR / "static"
HLS_DIR = BASE_DIR / "media" / "hls"
HLS_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR = BASE_DIR / "media" / "thumbs"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

# Windows doesn't always know these file types, so we register them ourselves.
# Without this, browsers may refuse to read the HLS playlist and segments.
mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")

# File types we treat as movies, and the type name browsers expect for each
MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
}

# How much of the file we read and send at a time (1 MB)
CHUNK_SIZE = 1024 * 1024

# Watching rules
RESUME_MIN_SECONDS = 10    # watched less than this: don't offer "continue watching"
FINISHED_FRACTION = 0.95   # watched this much or more: count the movie as finished
CONTINUE_LIMIT = 12        # how many movies the "continue watching" row can hold


# ---------- Finding movies on disk ----------

def iter_video_files():
    """Yield every video file inside the movies folder and its subfolders."""
    if not MOVIES_DIR.exists():
        return
    for file in sorted(MOVIES_DIR.rglob("*")):
        if file.is_file() and file.suffix.lower() in MEDIA_TYPES:
            yield file


def make_movie_id(file: Path) -> str:
    """Create a short ID that stays the same as long as the file path stays the same."""
    relative_path = file.relative_to(MOVIES_DIR).as_posix()
    return hashlib.md5(relative_path.encode("utf-8")).hexdigest()[:10]


def clean_title(raw_name: str) -> tuple[str, int | None]:
    """Turn names like 'Inception.2010.1080p' into ('Inception', 2010)."""
    name = re.sub(r"[._]+", " ", raw_name)
    year_match = re.search(r"\b(19|20)\d{2}\b", name)
    year = int(year_match.group()) if year_match else None
    if year_match:
        name = name[: year_match.start()]
    name = re.sub(r"[\(\[\s\-]+$", "", name).strip()
    return (name or raw_name), year


def describe_file(file: Path) -> dict:
    """Everything we store about one movie file."""
    # If the movie sits in its own folder, the folder name is usually cleaner
    raw_name = file.parent.name if file.parent != MOVIES_DIR else file.stem
    title, year = clean_title(raw_name)
    return {
        "id": make_movie_id(file),
        "relative_path": file.relative_to(MOVIES_DIR).as_posix(),
        "title": title,
        "year": year,
        "filename": file.name,
        "format": file.suffix.lower().lstrip("."),
        "size_mb": round(file.stat().st_size / (1024 * 1024), 1),
    }


def public_movie(movie: dict) -> dict:
    """The version of a movie we show to the outside world (never the real file path)."""
    movie_id = movie["id"]
    has_hls = (HLS_DIR / movie_id / "master.m3u8").exists()
    has_thumb = (THUMBS_DIR / f"{movie_id}.jpg").exists()
    return {
        "id": movie_id,
        "title": movie["title"],
        "year": movie["year"],
        "filename": movie["filename"],
        "format": movie["format"],
        "size_mb": movie["size_mb"],
        "stream_url": f"/api/stream/{movie_id}",
        "hls_url": f"/hls/{movie_id}/master.m3u8" if has_hls else None,
        "thumbnail_url": f"/thumbs/{movie_id}.jpg" if has_thumb else None,
    }


def build_movie_info(file: Path) -> dict:
    """Used by convert.py and thumbnails.py."""
    return public_movie(describe_file(file))


# ---------- The movies table ----------

def sync_library():
    """Make the movies table match the files on disk: add new, update changed, remove missing."""
    if not MOVIES_DIR.exists():
        return  # folder (or drive) missing: leave the table alone, so nobody's history is lost

    on_disk = {movie["id"]: movie for movie in map(describe_file, iter_video_files())}
    compared = ("title", "year", "filename", "format", "size_mb")

    with open_db() as conn:
        known = {row["id"]: row for row in conn.execute(
            "SELECT id, title, year, filename, format, size_mb FROM movies")}

        for movie in on_disk.values():
            old = known.get(movie["id"])
            if old is not None and all(old[key] == movie[key] for key in compared):
                continue  # nothing changed
            conn.execute(
                """INSERT INTO movies (id, relative_path, title, year, filename, format, size_mb)
                   VALUES (:id, :relative_path, :title, :year, :filename, :format, :size_mb)
                   ON CONFLICT(id) DO UPDATE SET
                       title = excluded.title, year = excluded.year, filename = excluded.filename,
                       format = excluded.format, size_mb = excluded.size_mb""",
                movie,
            )

        # Movies whose files are gone. Their watch progress goes with them (ON DELETE CASCADE).
        gone = [(movie_id,) for movie_id in known if movie_id not in on_disk]
        conn.executemany("DELETE FROM movies WHERE id = ?", gone)


def find_movie_file(movie_id: str) -> Path | None:
    """Find the real file on disk for a movie ID. Used only inside the server."""
    with open_db() as conn:
        row = conn.execute("SELECT relative_path FROM movies WHERE id = ?", (movie_id,)).fetchone()
    if row is None:
        return None
    path = MOVIES_DIR / row["relative_path"]
    return path if path.is_file() else None


def progress_info(row) -> dict | None:
    """Turn the progress columns of a database row into the dictionary the page uses."""
    if row["position_seconds"] is None:
        return None  # this person never started this movie
    finished = bool(row["finished"])
    return {
        "position": row["position_seconds"],
        "duration": row["duration_seconds"],
        "finished": finished,
        "resumable": not finished and row["position_seconds"] >= RESUME_MIN_SECONDS,
    }


def movie_from_row(row) -> dict:
    movie = public_movie(dict(row))
    movie["progress"] = progress_info(row)
    return movie


# ---------- Streaming helpers ----------

def parse_range(range_header: str, file_size: int) -> tuple[int, int]:
    """
    Read a Range header like 'bytes=1000-1999' and return (start, end).
    Supports 'bytes=500-' (from 500 to the end) and 'bytes=-500' (last 500 bytes).
    Raises ValueError if the range is invalid.
    """
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise ValueError("Bad range format")

    start_text, end_text = match.groups()

    if start_text == "" and end_text == "":
        raise ValueError("Empty range")

    if start_text == "":
        # Suffix range: the last N bytes of the file
        length = int(end_text)
        if length == 0:
            raise ValueError("Zero length range")
        start = max(file_size - length, 0)
        end = file_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
        end = min(end, file_size - 1)

    if start >= file_size or start > end:
        raise ValueError("Range outside the file")

    return start, end


def read_file_chunks(path: Path, start: int, end: int):
    """Read the file from start to end, one chunk at a time."""
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


# ---------- API routes ----------

@app.get("/")
def home():
    """Serve the web page."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"message": "HomeStream server is running!"}


@app.get("/api/movies")
def list_movies(request: Request, search: str | None = None):
    sync_library()  # so a movie you just copied into the folder shows up on refresh

    # The ? is a placeholder. The database fills it in safely, so whatever is in the
    # value can never be mistaken for SQL code. Never build SQL by joining strings together.
    with open_db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.title, m.year, m.filename, m.format, m.size_mb,
                      p.position_seconds, p.duration_seconds, p.finished
               FROM movies m
               LEFT JOIN progress p ON p.movie_id = m.id AND p.user_id = ?
               ORDER BY m.title COLLATE NOCASE, m.year""",
            (request.state.user["id"],),
        ).fetchall()

    movies = [movie_from_row(row) for row in rows]

    if search:
        search_text = search.strip().lower()
        movies = [m for m in movies if search_text in m["title"].lower()]

    return {"count": len(movies), "movies": movies}


@app.get("/api/continue")
def continue_watching(request: Request):
    """The signed-in person's unfinished movies, most recently watched first."""
    with open_db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.title, m.year, m.filename, m.format, m.size_mb,
                      p.position_seconds, p.duration_seconds, p.finished
               FROM progress p
               JOIN movies m ON m.id = p.movie_id
               WHERE p.user_id = ? AND p.finished = 0 AND p.position_seconds >= ?
               ORDER BY p.updated_at DESC
               LIMIT ?""",
            (request.state.user["id"], RESUME_MIN_SECONDS, CONTINUE_LIMIT),
        ).fetchall()
    return {"movies": [movie_from_row(row) for row in rows]}


@app.get("/api/movies/{movie_id}")
def get_movie(movie_id: str, request: Request):
    with open_db() as conn:
        row = conn.execute(
            """SELECT m.id, m.title, m.year, m.filename, m.format, m.size_mb,
                      p.position_seconds, p.duration_seconds, p.finished
               FROM movies m
               LEFT JOIN progress p ON p.movie_id = m.id AND p.user_id = ?
               WHERE m.id = ?""",
            (request.state.user["id"], movie_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Movie not found")
    return movie_from_row(row)


class ProgressUpdate(BaseModel):
    position: float = Field(ge=0, le=1_000_000)   # seconds into the movie
    duration: float = Field(gt=0, le=1_000_000)   # length of the movie in seconds


@app.put("/api/progress/{movie_id}")
def save_progress(movie_id: str, update: ProgressUpdate, request: Request):
    # The person comes from the sign-in cookie, never from the request body.
    # Otherwise anyone could send someone else's user id and change their history.
    user_id = request.state.user["id"]

    position = min(update.position, update.duration)
    finished = position >= update.duration * FINISHED_FRACTION

    with open_db() as conn:
        exists = conn.execute("SELECT 1 FROM movies WHERE id = ?", (movie_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Movie not found")

        # "Upsert": create the row the first time, update it every time after
        conn.execute(
            """INSERT INTO progress (user_id, movie_id, position_seconds, duration_seconds, finished, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, movie_id) DO UPDATE SET
                   position_seconds = excluded.position_seconds,
                   duration_seconds = excluded.duration_seconds,
                   finished = excluded.finished,
                   updated_at = excluded.updated_at""",
            (user_id, movie_id, position, update.duration, int(finished), int(time.time())),
        )
    return {"ok": True, "finished": finished}


@app.get("/api/stream/{movie_id}")
def stream_movie(movie_id: str, range_header: str | None = Header(default=None, alias="Range")):
    path = find_movie_file(movie_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Movie not found")

    file_size = path.stat().st_size
    media_type = MEDIA_TYPES[path.suffix.lower()]
    headers = {"Accept-Ranges": "bytes"}

    # No Range header: send the whole file
    if range_header is None:
        headers["Content-Length"] = str(file_size)
        return StreamingResponse(
            read_file_chunks(path, 0, file_size - 1),
            media_type=media_type,
            headers=headers,
        )

    # Range header present: send only the requested part
    try:
        start, end = parse_range(range_header, file_size)
    except ValueError:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{file_size}"})

    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    headers["Content-Length"] = str(end - start + 1)

    return StreamingResponse(
        read_file_chunks(path, start, end),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


# Serve the converted HLS playlists and video segments.
# StaticFiles already supports range requests, so seeking works here too.
app.mount("/hls", StaticFiles(directory=HLS_DIR), name="hls")

# Serve the thumbnail pictures
app.mount("/thumbs", StaticFiles(directory=THUMBS_DIR), name="thumbs")

# Serve CSS, JavaScript and other frontend files from the static folder.
# These lines stay at the bottom so they never hide the API routes above.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")