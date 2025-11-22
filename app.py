import io
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

import bcrypt
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from gridfs import GridFSBucket
from gridfs.errors import NoFile
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pymongo import MongoClient
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware


load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set. Update your .env file with a valid MongoDB Atlas URI.")

SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is not set. Add SECRET_KEY to your .env file for admin sessions.")

DATABASE_NAME = os.getenv("MONGO_DB_NAME", "article_manager")
GRIDFS_BUCKET_NAME = os.getenv("GRIDFS_BUCKET_NAME", "article_pdfs")
SESSION_COOKIE_NAME = "admin_session"
SESSION_MAX_AGE = 60 * 60 * 4  # 4 hours
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"

client = MongoClient(
    MONGO_URI,
    tls=True,
    tlsAllowInvalidCertificates=False,
    serverSelectionTimeoutMS=5000,
)


def test_mongo_connection() -> None:
    """Verify the MongoDB Atlas TLS connection during startup."""
    try:
        client.admin.command("ping")
        print("Connected to MongoDB Atlas successfully!")
    except Exception as exc:  # pragma: no cover - deployment visibility only
        print(f"MongoDB Atlas connection error: {exc}")


test_mongo_connection()

database = client[DATABASE_NAME]
articles_collection = database["articles"]
users_collection = database["users"]
gridfs_bucket = GridFSBucket(database, bucket_name=GRIDFS_BUCKET_NAME)

app = FastAPI(title="Article Manager", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
templates = Jinja2Templates(directory="templates")
serializer = URLSafeTimedSerializer(SECRET_KEY)
ADMIN_PUBLIC_PATHS = {"/admin/login", "/admin/login/"}


def ensure_pdf(upload: UploadFile) -> None:
    if upload.content_type not in {"application/pdf", "application/x-pdf"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF files are allowed.")


def parse_object_id(identifier: str) -> ObjectId:
    try:
        return ObjectId(identifier)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid file identifier.")


def serialize_article(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_iso = created_at.isoformat()
        created_at_display = created_at.strftime("%b %d, %Y %H:%M UTC")
    else:
        created_at_iso = None
        created_at_display = ""

    pdf_file_id = doc.get("pdf_file_id")
    pdf_id_str = str(pdf_file_id) if pdf_file_id else None

    return {
        "id": str(doc.get("_id")),
        "title": doc.get("title", ""),
        "author": doc.get("author", ""),
        "abstract": doc.get("abstract", ""),
        "body": doc.get("body", ""),
        "approved": doc.get("approved", False),
        "created_at": created_at_iso,
        "created_at_display": created_at_display,
        "pdf_file_id": pdf_id_str,
        "pdf_url": f"/pdf/{pdf_id_str}" if pdf_id_str else "#",
    }


async def save_article_upload(
    title: str,
    author: str,
    abstract: str,
    body: str,
    pdf_file: UploadFile,
) -> Dict[str, str]:
    ensure_pdf(pdf_file)
    pdf_bytes = await pdf_file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")

    filename = pdf_file.filename or "article.pdf"
    gridfs_id = gridfs_bucket.upload_from_stream(filename, io.BytesIO(pdf_bytes))

    document = {
        "title": title.strip(),
        "author": author.strip(),
        "abstract": abstract.strip(),
        "body": body.strip(),
        "created_at": datetime.utcnow(),
        "approved": False,
        "pdf_file_id": gridfs_id,
    }
    result = articles_collection.insert_one(document)
    return {"article_id": str(result.inserted_id), "pdf_file_id": str(gridfs_id)}


def build_dashboard_context(request: Request, **extra: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "request": request,
        "articles": [serialize_article(doc) for doc in articles_collection.find().sort("created_at", -1)],
    }
    data.update(extra)
    return data


def get_article_or_404(article_id: str) -> Dict[str, Any]:
    article = articles_collection.find_one({"_id": parse_object_id(article_id)})
    if not article:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
    return article


def delete_pdf_file(pdf_file_id: Optional[ObjectId]) -> None:
    if not pdf_file_id:
        return
    try:
        gridfs_bucket.delete(pdf_file_id)
    except NoFile:
        pass


def create_session_token(user: Dict[str, Any]) -> str:
    payload = {"sub": str(user.get("_id")), "role": user.get("role", "")}
    return serializer.dumps(payload)


def authenticate_request(request: Request) -> Optional[Dict[str, Any]]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        payload = serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if payload.get("role") != "admin":
        return None
    try:
        obj_id = ObjectId(payload.get("sub"))
    except (InvalidId, TypeError):
        return None
    return users_collection.find_one({"_id": obj_id, "role": "admin"})


def require_admin(request: Request) -> Dict[str, Any]:
    admin_user = getattr(request.state, "admin_user", None)
    if not admin_user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required.")
    return admin_user


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.admin_user = None
        path = request.url.path
        if path.startswith("/admin"):
            admin_user = authenticate_request(request)
            request.state.admin_user = admin_user
            if path not in ADMIN_PUBLIC_PATHS and not admin_user:
                return RedirectResponse(url="/admin/login", status_code=303)
        return await call_next(request)


app.add_middleware(AdminAuthMiddleware)


@app.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    """Send root traffic to the admin dashboard."""
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/upload", response_class=HTMLResponse)
async def show_upload_form(request: Request) -> HTMLResponse:
    """Render the public article upload form."""
    return templates.TemplateResponse("upload.html", {"request": request})


@app.post("/upload-article")
async def upload_article(
    title: str = Form(...),
    author: str = Form(...),
    abstract: str = Form(...),
    body: str = Form(...),
    pdf_file: UploadFile = File(...),
) -> JSONResponse:
    upload_info = await save_article_upload(title, author, abstract, body, pdf_file)
    return JSONResponse({"message": "Article uploaded successfully."} | upload_info)


@app.get("/articles")
async def list_articles() -> List[Dict[str, Any]]:
    """Return article metadata and GridFS identifiers for client downloads."""
    articles = articles_collection.find({"approved": True}).sort("created_at", -1)
    return [serialize_article(doc) for doc in articles]


@app.get("/pdf/{file_id}")
async def get_pdf(file_id: str) -> FileResponse:
    gridfs_id = parse_object_id(file_id)

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            gridfs_bucket.download_to_stream(gridfs_id, tmp_file)
            temp_path = tmp_file.name
    except NoFile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not found.")

    article = articles_collection.find_one({"pdf_file_id": gridfs_id}, {"title": 1})
    download_name = f"{article.get('title', 'article')}.pdf" if article else "article.pdf"

    background = BackgroundTask(lambda path=temp_path: os.remove(path) if os.path.exists(path) else None)
    return FileResponse(
        temp_path,
        media_type="application/pdf",
        filename=download_name,
        background=background,
    )


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_form(request: Request) -> HTMLResponse:
    if request.state.admin_user:
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    return templates.TemplateResponse("admin_login.html", {"request": request})


@app.post("/admin/login")
async def admin_login(request: Request, email: str = Form(...), password: str = Form(...)):
    normalized_email = email.strip().lower()
    user = users_collection.find_one({"email": normalized_email, "role": "admin"})
    if not user:
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Invalid credentials."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    stored_hash = user.get("password_hash")
    if isinstance(stored_hash, str):
        stored_hash_bytes = stored_hash.encode("utf-8")
    elif isinstance(stored_hash, bytes):
        stored_hash_bytes = stored_hash
    else:
        stored_hash_bytes = b""

    if not stored_hash_bytes or not bcrypt.checkpw(password.encode("utf-8"), stored_hash_bytes):
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Invalid credentials."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    token = create_session_token(user)
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
    )
    return response


@app.get("/admin/logout")
async def admin_logout() -> RedirectResponse:
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> HTMLResponse:
    require_admin(request)
    message = None
    if request.query_params.get("uploaded") == "1":
        message = "Article uploaded successfully."
    context = build_dashboard_context(request, message=message)
    return templates.TemplateResponse("admin_dashboard.html", context)


@app.post("/admin/upload-article")
async def admin_upload_article(
    request: Request,
    title: str = Form(...),
    author: str = Form(...),
    abstract: str = Form(...),
    body: str = Form(...),
    pdf_file: UploadFile = File(...),
):
    require_admin(request)
    try:
        await save_article_upload(title, author, abstract, body, pdf_file)
    except HTTPException as exc:
        context = build_dashboard_context(request, error=exc.detail)
        return templates.TemplateResponse("admin_dashboard.html", context, status_code=exc.status_code)
    return RedirectResponse(url="/admin/dashboard?uploaded=1", status_code=303)


@app.get("/admin/view/{article_id}", response_class=HTMLResponse)
async def admin_view_article(request: Request, article_id: str) -> HTMLResponse:
    require_admin(request)
    article = serialize_article(get_article_or_404(article_id))
    return templates.TemplateResponse("admin_view_article.html", {"request": request, "article": article})


@app.get("/admin/edit/{article_id}", response_class=HTMLResponse)
async def admin_edit_article_form(request: Request, article_id: str) -> HTMLResponse:
    require_admin(request)
    article = serialize_article(get_article_or_404(article_id))
    return templates.TemplateResponse("admin_edit_article.html", {"request": request, "article": article})


@app.post("/admin/edit-article")
async def admin_edit_article(
    request: Request,
    article_id: str = Form(...),
    title: str = Form(...),
    author: str = Form(...),
    abstract: str = Form(...),
    body: str = Form(...),
) -> RedirectResponse:
    require_admin(request)
    updates = {
        "title": title.strip(),
        "author": author.strip(),
        "abstract": abstract.strip(),
        "body": body.strip(),
    }
    articles_collection.update_one({"_id": parse_object_id(article_id)}, {"$set": updates})
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/approve-article")
async def admin_approve_article(request: Request, article_id: str = Form(...)) -> RedirectResponse:
    require_admin(request)
    articles_collection.update_one({"_id": parse_object_id(article_id)}, {"$set": {"approved": True}})
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/delete-article")
async def admin_delete_article(request: Request, article_id: str = Form(...)) -> RedirectResponse:
    require_admin(request)
    article = get_article_or_404(article_id)
    delete_pdf_file(article.get("pdf_file_id"))
    articles_collection.delete_one({"_id": parse_object_id(article_id)})
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/health", include_in_schema=False)
async def healthcheck() -> Dict[str, str]:
    """Simple health endpoint for Render readiness probes."""
    return {"status": "ok"}

