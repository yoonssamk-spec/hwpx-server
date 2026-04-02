from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime
import subprocess
import os
import uuid

app = FastAPI(title="HWPX Document Conversion API", version="1.1.0")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
HWPXSKILL_DIR = BASE_DIR / "hwpxskill"

DOWNLOAD_DIR.mkdir(exist_ok=True)


class BuildRequest(BaseModel):
    mode: str = Field(..., description="reference_rebuild or template_build")
    document_type: Optional[str] = Field(default="other")
    output_filename: str
    reference_file_id: Optional[str] = None
    source_file_ids: Optional[List[str]] = []
    content_payload: Optional[Dict[str, Any]] = {}


def ensure_hwpxskill_repo() -> None:
    repo_url = "https://github.com/Canine89/hwpxskill.git"

    if not HWPXSKILL_DIR.exists():
        subprocess.run(
            ["git", "clone", repo_url, str(HWPXSKILL_DIR)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    else:
        subprocess.run(
            ["git", "-C", str(HWPXSKILL_DIR), "pull"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


def map_document_type_to_template(document_type: str) -> str:
    mapping = {
        "official_letter": "gonmun",
        "report": "report",
        "minutes": "minutes",
        "proposal": "proposal",
        "exam": "report",
        "newsletter": "report",
        "form": "base",
        "other": "base",
    }
    return mapping.get(document_type, "base")


def sanitize_filename(filename: str) -> str:
    name = filename.strip().replace("\\", "_").replace("/", "_")
    if not name.lower().endswith(".hwpx"):
        name += ".hwpx"
    return name


@app.get("/health")
def health_check():
    return {
        "ok": True,
        "version": "1.1.0"
    }


@app.get("/downloads/{file_name}")
def download_generated_file(file_name: str):
    file_path = DOWNLOAD_DIR / file_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    return FileResponse(
        path=file_path,
        filename=file_name,
        media_type="application/octet-stream"
    )


@app.post("/documents/build")
def build_hwpx_document(req: BuildRequest):
    try:
        ensure_hwpxskill_repo()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"hwpxskill 준비 실패: {e}")

    safe_name = sanitize_filename(req.output_filename)
    unique_prefix = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]
    final_name = f"{unique_prefix}_{safe_name}"
    output_path = DOWNLOAD_DIR / final_name

    template_name = map_document_type_to_template(req.document_type or "other")

    build_script = HWPXSKILL_DIR / "scripts" / "build_hwpx.py"
    validate_script = HWPXSKILL_DIR / "scripts" / "validate.py"

    if not build_script.exists():
        raise HTTPException(status_code=500, detail="build_hwpx.py를 찾을 수 없습니다.")
    if not validate_script.exists():
        raise HTTPException(status_code=500, detail="validate.py를 찾을 수 없습니다.")

    if req.mode != "template_build":
        raise HTTPException(
            status_code=400,
            detail="현재 버전은 template_build만 지원합니다. reference_rebuild는 다음 단계에서 추가합니다."
        )

    try:
        build_result = subprocess.run(
            [
                "python", str(build_script),
                "--template", template_name,
                "--output", str(output_path)
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(HWPXSKILL_DIR)
        )

        validate_result = subprocess.run(
            [
                "python", str(validate_script),
                str(output_path)
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(HWPXSKILL_DIR)
        )

        public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        if not public_base_url:
            public_base_url = "https://hwpx-server.onrender.com"

        download_url = f"{public_base_url}/downloads/{final_name}"

        return JSONResponse(
            content={
                "job_id": unique_prefix,
                "status": "succeeded",
                "message": "실제 HWPX 파일 생성이 완료되었습니다.",
                "download_url": download_url,
                "output_file_id": final_name,
                "template_used": template_name,
                "warnings": [
                    "현재 1차 버전은 템플릿 기반 새 문서 생성만 지원합니다.",
                    "기존 양식(reference HWPX) 보존 편집은 다음 단계에서 추가할 예정입니다."
                ],
                "build_stdout": build_result.stdout[:1000],
                "validate_stdout": validate_result.stdout[:1000]
            }
        )

    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "HWPX 생성 또는 검증 중 오류가 발생했습니다.",
                "stdout": e.stdout,
                "stderr": e.stderr
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
