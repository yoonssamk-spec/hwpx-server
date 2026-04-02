from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from fastapi.responses import JSONResponse

app = FastAPI(title="HWPX Document Conversion API", version="1.0.0")


class BuildRequest(BaseModel):
    mode: str
    document_type: Optional[str] = "other"
    output_filename: str
    reference_file_id: Optional[str] = None
    source_file_ids: Optional[List[str]] = []
    content_payload: Optional[Dict[str, Any]] = {}


@app.get("/health")
def health_check():
    return {
        "ok": True,
        "version": "1.0.0"
    }


@app.post("/documents/build")
def build_hwpx_document(req: BuildRequest):
    return JSONResponse(
        content={
            "job_id": "demo-job-001",
            "status": "succeeded",
            "message": "Mock build completed successfully.",
            "download_url": "https://example.com/demo.hwpx"
        }
    )