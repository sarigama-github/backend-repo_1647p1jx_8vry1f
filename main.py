import os
import tempfile
import shutil
import subprocess
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


TMP_PARENT = tempfile.gettempdir()
PUBLIC_ROOT = os.path.join(TMP_PARENT, "ytclips_public")
if not os.path.exists(PUBLIC_ROOT):
    os.makedirs(PUBLIC_ROOT, exist_ok=True)


def _ensure_public_link(workdir: str):
    base = os.path.basename(workdir)
    link = os.path.join(PUBLIC_ROOT, base)
    if not os.path.exists(link):
        try:
            os.symlink(workdir, link)
        except Exception:
            # Fallback: copy tree if symlink not permitted (best-effort, not ideal for big files)
            try:
                if not os.path.exists(link):
                    shutil.copytree(workdir, link)
            except Exception:
                pass
    return link


@app.middleware("http")
async def link_generated_dirs(request: Request, call_next):
    path = request.url.path
    if path.startswith("/clips/"):
        parts = path.split("/")
        if len(parts) >= 4:
            workdir_name = parts[2]
            _ensure_public_link(os.path.join(TMP_PARENT, workdir_name))
    response = await call_next(request)
    return response


app.mount("/clips", StaticFiles(directory=PUBLIC_ROOT), name="clips")


def _run(cmd: List[str]):
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        return completed.stdout.strip(), completed.stderr.strip()
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Command failed: {' '.join(cmd)} | {e.stderr.strip()}")


@app.post("/api/clip", response_model=List[ClipInfo])
def create_clips(payload: ClipRequest):
    # Lazy import yt_dlp and imageio_ffmpeg
    try:
        import yt_dlp  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp not available: {e}")

    try:
        from imageio_ffmpeg import get_ffmpeg_exe  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"imageio-ffmpeg not available: {e}")

    ffmpeg_path = get_ffmpeg_exe()
    if not os.path.exists(ffmpeg_path):
        raise HTTPException(status_code=500, detail="ffmpeg binary not found")

    # Prepare working directory
    workdir = tempfile.mkdtemp(prefix="ytclip_")
    video_path = os.path.join(workdir, "source.mp4")

    # 0) Probe metadata (duration) without full download when possible
    info = None
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True}) as ydl:
            info = ydl.extract_info(str(payload.url), download=False)
    except Exception:
        info = None

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

    # Determine total duration (prefer metadata; as fallback, let ffmpeg read it)
    total: float = 0.0
    if info and isinstance(info, dict) and info.get("duration"):
        try:
            total = float(info.get("duration", 0.0))
        except Exception:
            total = 0.0

    if total <= 0:
        # Fallback: ask ffmpeg to print duration via show_entries
        # Some ffmpeg builds include ffprobe; imageio-ffmpeg may not. We'll parse duration from stderr of ffmpeg -i
        try:
            _, err = _run([ffmpeg_path, "-i", video_path])
            # Parse 'Duration: 00:01:23.45'
            import re
            m = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", err)
            if m:
                h = float(m.group(1))
                mnt = float(m.group(2))
                s = float(m.group(3))
                total = h * 3600 + mnt * 60 + s
        except Exception:
            pass

    if total <= 0:
        raise HTTPException(status_code=400, detail="Invalid video duration")

    # Start times
    import random
    one_minute = 60.0
    max_start = max(0.0, total - one_minute)

    starts: List[float] = []
    if payload.strategy == "random":
        for _ in range(payload.count):
            starts.append(round(random.uniform(0, max_start), 3))
    else:
        start_time = payload.start if payload.start is not None else 0.0
        for i in range(payload.count):
            s = min(start_time + i * one_minute, max_start)
            starts.append(round(s, 3))

    # Ensure public link prepared
    _ensure_public_link(workdir)

    # 2) Produce subclips with ffmpeg directly
    results: List[ClipInfo] = []
    for i, s in enumerate(starts):
        out_path = os.path.join(workdir, f"clip_{i+1:02d}.mp4")
        # Use -ss before -i for fast seek, and -t for duration
        cmd = [
            ffmpeg_path,
            "-y",
            "-ss", str(s),
            "-t", str(min(one_minute, max(0.001, total - s))),
            "-i", video_path,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-c:a", "aac",
            out_path,
        ]
        _run(cmd)
        url = f"/clips/{os.path.basename(workdir)}/{os.path.basename(out_path)}"
        results.append(ClipInfo(index=i + 1, start=s, duration=min(one_minute, total - s), url=url))

    return results


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
