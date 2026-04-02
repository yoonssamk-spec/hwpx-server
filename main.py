from fastapi import UploadFile, File
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from pathlib import Path
from datetime import datetime
import subprocess
import os
import uuid
import shutil
import zipfile
from xml.sax.saxutils import escape

app = FastAPI(title="HWPX Document Conversion API", version="1.2.0")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
WORK_DIR = BASE_DIR / "workfiles"
HWPXSKILL_DIR = BASE_DIR / "hwpxskill"
UPLOAD_DIR = BASE_DIR / "uploads"

UPLOAD_DIR.mkdir(exist_ok=True)
DOWNLOAD_DIR.mkdir(exist_ok=True)
WORK_DIR.mkdir(exist_ok=True)


class BuildRequest(BaseModel):
    mode: str = Field(..., description="reference_rebuild or template_build")
    document_type: Optional[str] = Field(default="other")
    output_filename: str
    reference_file_id: Optional[str] = None
    source_file_ids: Optional[List[str]] = []
    content_payload: Optional[Dict[str, Any]] = {}


class ExamQuestion(BaseModel):
    number: int
    type: str = Field(..., description="multiple_choice, short_answer, essay")
    prompt: str
    choices: Optional[List[str]] = None
    points: Optional[float] = None
    answer: Optional[str] = None
    explanation: Optional[str] = None


class BuildExamRequest(BaseModel):
    school_name: Optional[str] = "학교명"
    grade: Optional[str] = "학년"
    semester: Optional[str] = "학기"
    subject: Optional[str] = "과목"
    exam_title: str
    exam_date: Optional[str] = ""
    instructions: Optional[List[str]] = []
    questions: List[ExamQuestion]
    output_filename: str
    generate_student_version: bool = True
    generate_teacher_version: bool = True


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


def sanitize_filename(filename: str) -> str:
    name = filename.strip().replace("\\", "_").replace("/", "_")
    if not name.lower().endswith(".hwpx"):
        name += ".hwpx"
    return name


def make_unique_name(filename: str) -> str:
    safe_name = sanitize_filename(filename)
    unique_prefix = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "_" + uuid.uuid4().hex[:8]
    return f"{unique_prefix}_{safe_name}"


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "https://hwpx-server.onrender.com").rstrip("/")


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


def build_base_hwpx(output_path: Path, template_name: str = "report") -> None:
    build_script = HWPXSKILL_DIR / "scripts" / "build_hwpx.py"
    validate_script = HWPXSKILL_DIR / "scripts" / "validate.py"

    if not build_script.exists():
        raise HTTPException(status_code=500, detail="build_hwpx.py를 찾을 수 없습니다.")
    if not validate_script.exists():
        raise HTTPException(status_code=500, detail="validate.py를 찾을 수 없습니다.")

    subprocess.run(
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

    subprocess.run(
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


def exam_header_text(req: BuildExamRequest) -> str:
    lines = [
        req.school_name,
        f"{req.grade} {req.semester} {req.subject}",
        req.exam_title
    ]
    if req.exam_date:
        lines.append(f"시행일: {req.exam_date}")
    return "\n".join([x for x in lines if x])


def format_question_text(q: ExamQuestion, include_answer: bool = False) -> str:
    lines = []

    point_text = f" [{q.points}점]" if q.points is not None else ""
    lines.append(f"{q.number}. {q.prompt}{point_text}")

    if q.type == "multiple_choice" and q.choices:
        choice_symbols = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧"]
        for idx, choice in enumerate(q.choices):
            symbol = choice_symbols[idx] if idx < len(choice_symbols) else f"({idx+1})"
            lines.append(f"   {symbol} {choice}")

    elif q.type == "short_answer":
        lines.append("   답: ______________________________")

    elif q.type == "essay":
        lines.append("   답안:")
        lines.append("   ________________________________________")
        lines.append("   ________________________________________")
        lines.append("   ________________________________________")

    if include_answer:
        if q.answer:
            lines.append(f"   정답: {q.answer}")
        if q.explanation:
            lines.append(f"   해설: {q.explanation}")

    return "\n".join(lines)


def compose_exam_text(req: BuildExamRequest, include_answer: bool = False) -> str:
    parts = []
    parts.append(exam_header_text(req))
    parts.append("")

    if req.instructions:
        parts.append("[유의사항]")
        for idx, item in enumerate(req.instructions, start=1):
            parts.append(f"{idx}. {item}")
        parts.append("")

    parts.append("[문항]")
    for q in req.questions:
        parts.append(format_question_text(q, include_answer=include_answer))
        parts.append("")

    return "\n".join(parts).strip()


def replace_text_in_hwpx(hwpx_path: Path, new_text: str) -> None:
    """
    생성된 HWPX(zip)를 풀어서 section XML 안의 텍스트 노드를
    시험지 본문으로 교체하는 단순 버전.
    """
    work_id = uuid.uuid4().hex
    extract_dir = WORK_DIR / work_id
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(hwpx_path, "r") as zf:
            zf.extractall(extract_dir)

        section_candidates = list(extract_dir.glob("Contents/section*.xml"))
        if not section_candidates:
            raise RuntimeError("section XML을 찾을 수 없습니다.")

        section_path = section_candidates[0]
        xml_text = section_path.read_text(encoding="utf-8", errors="ignore")

        escaped = escape(new_text).replace("\n", "</hp:t><hp:lineBreak/><hp:t>")

        # 가장 첫 텍스트 블록을 찾아 교체하는 단순 방식
        import re
        pattern = r"<hp:t>.*?</hp:t>"
        replacement = f"<hp:t>{escaped}</hp:t>"
        xml_text_new, count = re.subn(pattern, replacement, xml_text, count=1, flags=re.DOTALL)

        if count == 0:
            raise RuntimeError("본문 텍스트 영역을 찾지 못했습니다.")

        section_path.write_text(xml_text_new, encoding="utf-8")

        rebuilt_path = hwpx_path.with_suffix(".rebuilt.hwpx")
        with zipfile.ZipFile(rebuilt_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in extract_dir.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(extract_dir))

        shutil.move(str(rebuilt_path), str(hwpx_path))

    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


@app.get("/health")
def health_check():
    return {
        "ok": True,
        "version": "1.2.0"
    }
@app.post("/files/upload")
async def upload_file(file: UploadFile = File(...)):
    file_id = uuid.uuid4().hex
    file_path = UPLOAD_DIR / f"{file_id}_{file.filename}"

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    return {
        "file_id": file_id,
        "filename": file.filename,
        "stored_path": str(file_path)
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

    if req.mode != "template_build":
        raise HTTPException(
            status_code=400,
            detail="현재 버전은 template_build만 지원합니다."
        )

    final_name = make_unique_name(req.output_filename)
    output_path = DOWNLOAD_DIR / final_name
    template_name = map_document_type_to_template(req.document_type or "other")

    try:
        build_base_hwpx(output_path, template_name=template_name)
        download_url = f"{public_base_url()}/downloads/{final_name}"

        return JSONResponse(
            content={
                "job_id": final_name,
                "status": "succeeded",
                "message": "실제 HWPX 파일 생성이 완료되었습니다.",
                "download_url": download_url,
                "output_file_id": final_name,
                "template_used": template_name
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


@app.post("/documents/build-exam")
def build_exam_document(req: BuildExamRequest):
    try:
        ensure_hwpxskill_repo()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"hwpxskill 준비 실패: {e}")

    results = {}

    try:
        if req.generate_student_version:
            student_name = make_unique_name("학생용_" + req.output_filename)
            student_path = DOWNLOAD_DIR / student_name

            build_base_hwpx(student_path, template_name="report")
            student_text = compose_exam_text(req, include_answer=False)
            replace_text_in_hwpx(student_path, student_text)

            results["student_version"] = {
                "output_file_id": student_name,
                "download_url": f"{public_base_url()}/downloads/{student_name}"
            }

        if req.generate_teacher_version:
            teacher_name = make_unique_name("교사용_" + req.output_filename)
            teacher_path = DOWNLOAD_DIR / teacher_name

            build_base_hwpx(teacher_path, template_name="report")
            teacher_text = compose_exam_text(req, include_answer=True)
            replace_text_in_hwpx(teacher_path, teacher_text)

            results["teacher_version"] = {
                "output_file_id": teacher_name,
                "download_url": f"{public_base_url()}/downloads/{teacher_name}"
            }

        return JSONResponse(
            content={
                "status": "succeeded",
                "message": "시험지 HWPX 생성이 완료되었습니다.",
                "exam_title": req.exam_title,
                "question_count": len(req.questions),
                "results": results,
                "warnings": [
                    "현재 버전은 문제 텍스트를 본문 영역에 삽입하는 방식입니다.",
                    "학교 고유 시험지 양식 유지형 변환은 다음 단계에서 reference 기반으로 확장할 예정입니다."
                ]
            }
        )

    except subprocess.CalledProcessError as e:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "시험지 생성 중 오류가 발생했습니다.",
                "stdout": e.stdout,
                "stderr": e.stderr
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

@app.post("/files/upload")
async def upload_file(file: UploadFile = File(...)):
    file_id = uuid.uuid4().hex
    file_path = UPLOAD_DIR / f"{file_id}_{file.filename}"

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    return {
        "file_id": file_id,
        "filename": file.filename,
        "stored_path": str(file_path)
    }
@app.post("/documents/build-reference")
def build_from_reference(reference_file_id: str, output_filename: str):
    try:
        ensure_hwpxskill_repo()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    ref_files = list(UPLOAD_DIR.glob(f"{reference_file_id}_*"))
    if not ref_files:
        raise HTTPException(status_code=404, detail="reference 파일 없음")

    ref_path = ref_files[0]

    final_name = make_unique_name(output_filename)
    output_path = DOWNLOAD_DIR / final_name

    analyze_script = HWPXSKILL_DIR / "scripts" / "analyze_template.py"
    build_script = HWPXSKILL_DIR / "scripts" / "build_hwpx.py"

    subprocess.run(
        ["python", str(analyze_script), str(ref_path)],
        check=True,
        cwd=str(HWPXSKILL_DIR)
    )

    subprocess.run(
        [
            "python", str(build_script),
            "--template", "base",
            "--output", str(output_path)
        ],
        check=True,
        cwd=str(HWPXSKILL_DIR)
    )

    return {
        "status": "succeeded",
        "download_url": f"{public_base_url()}/downloads/{final_name}"
    }
