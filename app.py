"""
Application de suivi et de centralisation des devis (version Supabase)
-------------------------------------------------------------------

Cette version de l'application remplace la base locale SQLite par
Supabase (PostgreSQL + stockage) pour persister les données des devis.
Les fichiers PDF et signatures continuent d'être stockés sur le disque,
mais sur Vercel le système de fichiers est en lecture seule ; nous
utilisons donc un dossier temporaire (/tmp) pour les fichiers générés.

Prérequis (à installer via pip et dans requirements.txt) :
    - fastapi
    - uvicorn
    - pillow
    - jinja2
    - python-multipart
    - pandas
    - openpyxl
    - supabase
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont

# Import du client Supabase
from supabase import create_client, Client  # type: ignore

##############################
# Configuration de Supabase
##############################

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    raise RuntimeError(
        "Les variables d'environnement SUPABASE_URL et SUPABASE_KEY doivent être définies."
    )

##############################
# Chemins et répertoires
##############################

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

# Utilise /tmp pour le stockage des fichiers générés (écriture autorisée sur Vercel)
_tmp_dir = Path(tempfile.gettempdir())
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_tmp_dir / "uploads")))
SIGNATURE_DIR = Path(os.environ.get("SIGNATURE_DIR", str(_tmp_dir / "signatures")))

# Crée les dossiers s'ils n'existent pas
DATA_DIR.mkdir(parents=True, exist_ok=True)
SIGNATURE_DIR.mkdir(parents=True, exist_ok=True)

##############################
# Fonctions utilitaires Supabase
##############################

def supabase_table_insert(data: Dict) -> Dict | None:
    """Insère un enregistrement dans la table `quotes` et renvoie l'objet créé."""
    if not supabase:
        return None
    resp = supabase.table("quotes").insert(data).execute()
    return resp.data[0] if resp.data else None

def supabase_table_update(quote_id: int, updates: Dict) -> None:
    """Met à jour un enregistrement de la table `quotes`."""
    if not supabase:
        return
    supabase.table("quotes").update(updates).eq("id", quote_id).execute()

def supabase_table_select(filters: Dict | None = None) -> List[Dict]:
    """Récupère des enregistrements selon des filtres simples."""
    if not supabase:
        return []
    query = supabase.table("quotes")
    if filters:
        for key, value in filters.items():
            if key == "quote_date":
                query = query.like("quote_date", f"{value}%")
            else:
                query = query.eq(key, value)
    resp = query.select("*").order("quote_date", desc=True).execute()
    return resp.data or []

def supabase_table_get(quote_id: int) -> Dict | None:
    """Récupère un devis par son id."""
    if not supabase:
        return None
    resp = supabase.table("quotes").select("*").eq("id", quote_id).execute()
    return resp.data[0] if resp.data else None

##############################
# Modèle Pydantic
##############################

class Quote(BaseModel):
    id: int
    client_name: str
    quote_date: str
    category: str
    description: Optional[str] = ""
    amount: Optional[float] = 0.0
    pdf_filename: Optional[str] = None
    signed_pdf_filename: Optional[str] = None
    invoice_amount: Optional[float] = None
    invoice_comment: Optional[str] = None
    created_at: str
    updated_at: str

    @property
    def month(self) -> str:
        return self.quote_date[:7]

##############################
# Génération de PDF et signatures
##############################

def generate_pdf(quote: Quote, signature_path: Optional[Path] = None) -> str:
    """Génère un PDF à partir des infos du devis et d'une signature éventuelle."""
    width, height = 595, 842
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    lines = [
        f"Devis n° {quote.id}",
        f"Client : {quote.client_name}",
        f"Date : {quote.quote_date}",
        f"Catégorie : {quote.category}",
        f"Montant : {quote.amount:.2f} EUR",
        "",
        "Description :",
    ]
    if quote.description:
        lines += quote.description.split("\n")
    else:
        lines.append("(aucune description)")

    x_margin, y, line_height = 40, 780, 20
    for line in lines:
        draw.text((x_margin, y), line, fill="black", font=font)
        y -= line_height

    if signature_path and signature_path.exists():
        try:
            sig_img = Image.open(signature_path).convert("RGBA")
            max_width, max_height = 200, 100
            ratio = min(max_width / sig_img.width, max_height / sig_img.height, 1.0)
            new_size = (int(sig_img.width * ratio), int(sig_img.height * ratio))
            sig_resized = sig_img.resize(new_size, Image.LANCZOS)
            sig_x = width - x_margin - new_size[0]
            sig_y = 40
            img.paste(sig_resized, (sig_x, sig_y), sig_resized)
        except Exception:
            pass

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"quote_{quote.id}_{timestamp}.pdf"
    filepath = DATA_DIR / filename
    img.save(filepath, "PDF", resolution=100.0)
    return filename

def save_signature(upload_file: UploadFile) -> Optional[Path]:
    """Enregistre une signature téléchargée et retourne son chemin."""
    try:
        suffix = Path(upload_file.filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            return None
        sig_name = f"sig_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"
        sig_path = SIGNATURE_DIR / sig_name
        with open(sig_path, "wb") as f:
            f.write(upload_file.file.read())
        return sig_path
    except Exception:
        return None

##############################
# Application FastAPI
##############################

app = FastAPI(title="Gestion des devis (Supabase)")

# Monter le dossier static (assets de templates)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

@app.get("/")
def index(request: Request, month: Optional[str] = None, category: Optional[str] = None):
    filters: Dict[str, str] = {}
    if month:
        filters["quote_date"] = month
    if category:
        filters["category"] = category
    rows = supabase_table_select(filters)
    quotes = [Quote(**row) for row in rows]
    all_rows = supabase_table_select()
    categories = sorted({r["category"] for r in all_rows if r.get("category")})
    month_list = sorted({r["quote_date"][:7] for r in all_rows if r.get("quote_date")}, reverse=True)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "quotes": quotes,
            "selected_month": month,
            "selected_category": category,
            "categories": categories,
            "months": month_list,
        },
    )

@app.get("/import_excel")
def import_excel_form(request: Request):
    return templates.TemplateResponse("import_excel.html", {"request": request})

@app.post("/import_excel")
async def import_excel(request: Request, excel_file: UploadFile = File(...)):
    import tempfile
    import pandas as pd
    suffix = Path(excel_file.filename).suffix.lower()
    now = datetime.now().isoformat()
    try:
        content = await excel_file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        if suffix in [".xlsx", ".xls"]:
            df = pd.read_excel(tmp_path)
        elif suffix in [".csv", ".txt"]:
            df = pd.read_csv(tmp_path)
        else:
            return RedirectResponse(url="/import_excel", status_code=303)
        imported_count = 0
        for _, row_data in df.iterrows():
            client = str(row_data.get("client_name", "")).strip()
            date = str(row_data.get("quote_date", "")).strip()
            cat = str(row_data.get("category", "")).strip()
            desc = str(row_data.get("description", ""))
            amt = float(row_data.get("amount", 0.0))
            if not client or not date or not cat:
                continue
            created = supabase_table_insert({
                "client_name": client,
                "quote_date": date,
                "category": cat,
                "description": desc or None,
                "amount": amt,
                "created_at": now,
                "updated_at": now,
            })
            if not created:
                continue
            quote_id = created["id"]
            quote = Quote(**created)
            pdf_filename = generate_pdf(quote)
            supabase_table_update(quote_id, {"pdf_filename": pdf_filename, "updated_at": now})
            imported_count += 1
        os.remove(tmp_path)
        return RedirectResponse(url="/", status_code=303)
    except Exception:
        return RedirectResponse(url="/import_excel", status_code=303)

@app.get("/new")
def new_quote_form(request: Request):
    return templates.TemplateResponse("new.html", {"request": request})

@app.post("/new")
async def create_quote(
    request: Request,
    client_name: str = Form(...),
    quote_date: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    amount: float = Form(0.0),
    pdf_upload: UploadFile = File(None),
):
    now = datetime.now().isoformat()
    data = {
        "client_name": client_name,
        "quote_date": quote_date,
        "category": category,
        "description": description or None,
        "amount": amount,
        "created_at": now,
        "updated_at": now,
    }
    created = supabase_table_insert(data)
    if not created:
        return RedirectResponse(url="/", status_code=303)
    quote_id = created["id"]
    quote = Quote(**created)
    pdf_filename: Optional[str] = None
    if pdf_upload and pdf_upload.filename:
        suffix = Path(pdf_upload.filename).suffix.lower()
        if suffix == ".pdf":
            pdf_name = f"imported_quote_{quote_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"
            pdf_path = DATA_DIR / pdf_name
            with open(pdf_path, "wb") as f:
                f.write(await pdf_upload.read())
            pdf_filename = pdf_name
    if not pdf_filename:
        pdf_filename = generate_pdf(quote)
    supabase_table_update(quote_id, {"pdf_filename": pdf_filename, "updated_at": now})
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)

@app.get("/quote/{quote_id}")
def quote_detail(request: Request, quote_id: int):
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse("quote_detail.html", {"request": request, "quote": quote})

@app.get("/quote/{quote_id}/download")
def download_pdf(quote_id: int, signed: bool = False):
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    filename = quote.signed_pdf_filename if (signed and quote.signed_pdf_filename) else quote.pdf_filename
    if not filename:
        return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
    file_path = DATA_DIR / filename
    return FileResponse(path=file_path, media_type="application/pdf", filename=filename)

@app.get("/quote/{quote_id}/sign")
def sign_form(request: Request, quote_id: int):
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse("sign.html", {"request": request, "quote": quote})

@app.post("/quote/{quote_id}/sign")
async def sign_quote(request: Request, quote_id: int, signature: UploadFile = File(...)):
    sig_path = save_signature(signature)
    if not sig_path:
        return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    signed_filename = generate_pdf(quote, signature_path=sig_path)
    now = datetime.now().isoformat()
    supabase_table_update(quote_id, {"signed_pdf_filename": signed_filename, "updated_at": now})
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)

@app.post("/quote/{quote_id}/invoice")
async def submit_invoice(
    request: Request,
    quote_id: int,
    invoice_amount: float = Form(...),
    invoice_comment: str = Form("")
):
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    now = datetime.now().isoformat()
    supabase_table_update(
        quote_id,
        {
            "invoice_amount": invoice_amount,
            "invoice_comment": invoice_comment or None,
            "updated_at": now,
        },
    )
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)

##############################
# Démarrage local
##############################

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
