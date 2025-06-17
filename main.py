from fastapi import FastAPI, File, UploadFile, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pymongo import MongoClient
from datetime import datetime
import os
import shutil
import uuid
from typing import Optional
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Clipo AI Backend",
    description="Video processing API with FFmpeg and Celery",
    version="1.0.0"
)

client = MongoClient("mongodb://localhost:27017")
db = client["clipo_ai"]
videos_collection = db["videos"]

os.makedirs("uploads", exist_ok=True)
os.makedirs("thumbnails", exist_ok=True)

app.mount("/thumbnails", StaticFiles(directory="thumbnails"), name="thumbnails")

from celery import Celery

celery_app = Celery(
    "video_processor",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0"
)

@celery_app.task
def process_video_task(video_id: str, file_path: str, filename: str):
    import subprocess
    import json
    from bson import ObjectId
    
    try:
        videos_collection.update_one(
            {"_id": ObjectId(video_id)},
            {"$set": {"status": "processing", "updated_at": datetime.utcnow()}}
        )
        
        duration_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", file_path
        ]
        result = subprocess.run(duration_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            raise Exception(f"FFprobe failed: {result.stderr}")
        
        data = json.loads(result.stdout)
        duration_seconds = float(data['format']['duration'])
        hours = int(duration_seconds // 3600)
        minutes = int((duration_seconds % 3600) // 60)
        seconds = int(duration_seconds % 60)
        duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        
        thumbnail_time = duration_seconds * 0.1
        thumbnail_filename = f"{os.path.splitext(filename)[0]}_thumb.jpg"
        thumbnail_path = os.path.join("thumbnails", thumbnail_filename)
        
        thumbnail_cmd = [
            "ffmpeg", "-i", file_path, "-ss", str(thumbnail_time),
            "-vframes", "1", "-q:v", "2", "-s", "320x240",
            "-y", thumbnail_path
        ]
        
        result = subprocess.run(thumbnail_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            raise Exception(f"FFmpeg thumbnail generation failed: {result.stderr}")
        
        thumbnail_url = f"https://{os.environ.get('CODESPACE_NAME', 'localhost')}-8000.{os.environ.get('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN', 'localhost:8000')}/thumbnails/{thumbnail_filename}"
        
        videos_collection.update_one(
            {"_id": ObjectId(video_id)},
            {
                "$set": {
                    "status": "done",
                    "duration": duration,
                    "thumbnail_url": thumbnail_url,
                    "updated_at": datetime.utcnow()
                }
            }
        )
        
        logger.info(f"Video processing completed for {video_id}")
        return {"status": "success", "duration": duration, "thumbnail_url": thumbnail_url}
        
    except Exception as e:
        logger.error(f"Error processing video {video_id}: {e}")
        videos_collection.update_one(
            {"_id": ObjectId(video_id)},
            {
                "$set": {
                    "status": "failed",
                    "error_message": str(e),
                    "updated_at": datetime.utcnow()
                }
            }
        )
        raise

@app.post("/upload-video/")
async def upload_video(file: UploadFile = File(...)):
    allowed_extensions = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm"}
    file_ext = os.path.splitext(file.filename)[1].lower()
    
    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format. Allowed: {', '.join(allowed_extensions)}"
        )
    
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    
    if file_size > 100_000_000:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large. Maximum size: 100MB"
        )
    
    try:
        unique_filename = f"{uuid.uuid4()}_{file.filename}"
        file_path = os.path.join("uploads", unique_filename)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        video_doc = {
            "filename": file.filename,
            "upload_time": datetime.utcnow().isoformat(),
            "status": "pending",
            "file_path": file_path,
            "file_size": file_size
        }
        
        result = videos_collection.insert_one(video_doc)
        video_id = str(result.inserted_id)
        
        process_video_task.delay(video_id, file_path, file.filename)
        
        return {
            "id": video_id,
            "filename": file.filename,
            "upload_time": video_doc["upload_time"],
            "status": "pending",
            "message": "Video uploaded successfully. Processing started."
        }
        
    except Exception as e:
        logger.error(f"Error uploading video: {e}")
        if 'file_path' in locals() and os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload video"
        )

@app.get("/video-status/{video_id}")
async def get_video_status(video_id: str):
    from bson import ObjectId
    
    try:
        video = videos_collection.find_one({"_id": ObjectId(video_id)})
        if not video:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Video not found"
            )
        
        return {
            "id": video_id,
            "status": video["status"]
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video ID"
        )

@app.get("/video-metadata/{video_id}")
async def get_video_metadata(video_id: str):
    from bson import ObjectId
    
    try:
        video = videos_collection.find_one({"_id": ObjectId(video_id)})
        if not video:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Video not found"
            )
        
        return {
            "filename": video["filename"],
            "upload_time": video["upload_time"],
            "status": video["status"],
            "duration": video.get("duration"),
            "thumbnail_url": video.get("thumbnail_url"),
            "file_size": video.get("file_size"),
            "error_message": video.get("error_message")
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video ID"
        )

@app.get("/health")
async def health_check():
    return {"status": "healthy", "message": "Clipo AI Backend is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)