import os
import tempfile
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl, Field
from fastapi.staticfiles import StaticFiles

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ClipRequest(BaseModel):
    url: HttpUrl = Field(..., description="YouTube video URL")
    count: int = Field(1, ge=1, le=20, description="How many 60s clips to produce")
    strategy: str = Field(
        "sequential",
        description="How to pick start times: 'sequential' or 'random'",
    )
    start: Optional[float] = Field(None, ge=0, description="Optional manual start time in seconds for the first clip")


class ClipInfo(BaseModel):
    index: int
    start: float
    duration: float
    url: str


@app.get("/")
def read_root():
    return {"message": "YouTube clipper backend running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/clip", response_model=List[ClipInfo])
def create_clips(payload: ClipRequest):
    """
    Download the YouTube video to a temp file, slice it into N clips of 60 seconds,
    and return a list of direct download URLs (served from a temp directory).
    """
    # Lazy import heavy deps so the server can start even if optional deps are missing
    try:
        import yt_dlp  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp not available: {e}")

    try:
        from moviepy.editor import VideoFileClip  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"moviepy not available: {e}")

    # Prepare tmp working dir per request
    workdir = tempfile.mkdtemp(prefix="ytclip_")
    video_path = os.path.join(workdir, "source.mp4")

    # 1) Download best mp4 using yt-dlp
    ydl_opts = {
        "outtmpl": video_path,
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([str(payload.url)])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")

    # 2) Load with moviepy to get duration
    try:
        clip = VideoFileClip(video_path)
        total = float(clip.duration or 0)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load video: {e}")

    if total <= 0:
        raise HTTPException(status_code=400, detail="Invalid video duration")

    # Determine start times
    import random

    one_minute = 60.0
    max_start = max(0.0, total - one_minute)

    starts: List[float] = []
    if payload.strategy == "random":
        for _ in range(payload.count):
            starts.append(round(random.uniform(0, max_start), 3))
    else:  # sequential by default
        start_time = payload.start if payload.start is not None else 0.0
        for i in range(payload.count):
            s = min(start_time + i * one_minute, max_start)
            starts.append(round(s, 3))

    # 3) Cut and write each subclip
    results: List[ClipInfo] = []
    for i, s in enumerate(starts):
        sub = clip.subclip(s, min(s + one_minute, total))
        out_path = os.path.join(workdir, f"clip_{i+1:02d}.mp4")
        try:
            sub.write_videofile(
                out_path,
                codec="libx264",
                audio_codec="aac",
                temp_audiofile=os.path.join(workdir, f"temp_audio_{i}.m4a"),
                remove_temp=True,
                verbose=False,
                logger=None,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to write clip {i+1}: {e}")

        # Expose via a static mount path
        url = f"/clips/{os.path.basename(workdir)}/{os.path.basename(out_path)}"
        results.append(ClipInfo(index=i + 1, start=s, duration=min(one_minute, total - s), url=url))

    # Keep resources mounted for download
    clip.close()

    return results


# Static files serving for generated clips
TMP_PARENT = tempfile.gettempdir()
PUBLIC_ROOT = os.path.join(TMP_PARENT, "ytclips_public")
if not os.path.exists(PUBLIC_ROOT):
    os.makedirs(PUBLIC_ROOT, exist_ok=True)

# Symlink each workdir into PUBLIC_ROOT when used

def _ensure_public_link(workdir: str):
    base = os.path.basename(workdir)
    link = os.path.join(PUBLIC_ROOT, base)
    if not os.path.exists(link):
        try:
            os.symlink(workdir, link)
        except Exception:
            # If symlink fails (e.g., on limited FS), fallback to exposing the file directly later
            pass
    return link


@app.middleware("http")
async def link_generated_dirs(request: Request, call_next):
    # if requesting /clips/<workdir>/<file>, ensure symlink exists
    path = request.url.path
    if path.startswith("/clips/"):
        parts = path.split("/")
        if len(parts) >= 4:
            workdir_name = parts[2]
            _ensure_public_link(os.path.join(TMP_PARENT, workdir_name))
    response = await call_next(request)
    return response


app.mount("/clips", StaticFiles(directory=PUBLIC_ROOT), name="clips")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
