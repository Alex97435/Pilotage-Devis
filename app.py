"""
Application de suivi et de centralisation des devis (version Supabase)
avec gestion des entreprises (companies).
"""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from PIL import Image, ImageDraw, ImageFont
from supabase import create_client, Client  # type: ignore
import pandas as pd

# Configuration Supabase
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    raise RuntimeError("Définissez SUPABASE_URL et SUPABASE_KEY dans l'environnement.")

# Répertoires temporaires pour les PDF/signatures
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
_tmp_dir = Path(tempfile.gettempdir())
DATA_DIR = Path(os.environ.get("DATA_DIR", str(_tmp_dir / "uploads")))
SIGNATURE_DIR = Path(os.environ.get("SIGNATURE_DIR", str(_tmp_dir / "signatures")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SIGNATURE_DIR.mkdir(parents=True, exist_ok=True)

# Modèles Pydantic
class Company(BaseModel):
    id: int
    name: str
    created_at: str

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
    company_id: Optional[int] = None
    created_at: str
    updated_at: str

    @property
    def month(self) -> str:
        return self.quote_date[:7]

# Fonctions utilitaires Supabase
def supabase_table_insert(table: str, data: Dict) -> Dict | None:
    if not supabase:
        return None
    resp = supabase.table(table).insert(data).execute()
    return resp.data[0] if resp.data else None

def supabase_table_update(table: str, record_id: int, updates: Dict) -> None:
    if not supabase:
        return
    supabase.table(table).update(updates).eq("id", record_id).execute()

def supabase_table_select(table: str, filters: Dict | None = None) -> List[Dict]:
    if not supabase:
        return []
    query = supabase.table(table)
    if filters:
        for key, value in filters.items():
            query = query.eq(key, value)
    resp = query.select("*").order("created_at").execute()
    return resp.data or []

def supabase_table_get(table: str, record_id: int) -> Dict | None:
    if not supabase:
        return None
    resp = supabase.table(table).select("*").eq("id", record_id).execute()
    return resp.data[0] if resp.data else None

# PDF et signature
def generate_pdf(quote: Quote, signature_path: Optional[Path] = None) -> str:
    width, height = 595, 842
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    lines = [
        f"Devis n° {quote.id}",
        f"Entreprise : {quote.company_id or '-'}",
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
    filename = f"quote_{quote.id}_{datetime.now():%Y%m%d%H%M%S}.pdf"
    filepath = DATA_DIR / filename
    img.save(filepath, "PDF", resolution=100.0)
    return filename

def save_signature(upload_file: UploadFile) -> Optional[Path]:
    try:
        suffix = Path(upload_file.filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            return None
        sig_name = f"sig_{datetime.now():%Y%m%d%H%M%S}{suffix}"
        sig_path = SIGNATURE_DIR / sig_name
        with open(sig_path, "wb") as f:
            f.write(upload_file.file.read())
        return sig_path
    except Exception:
        return None

# Application FastAPI
app = FastAPI(title="Gestion des devis (Supabase)")

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Page d'accueil avec filtre par entreprise et par statut
@app.get("/")
def index(
    request: Request,
    company_id: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
):
    # Récupérer les sociétés
    company_rows = supabase_table_select("companies")
    companies = [Company(**c) for c in company_rows]

    # Préparer les filtres pour les devis
    filters: Dict[str, int] = {}
    if company_id:
        filters["company_id"] = company_id

    quote_rows = supabase_table_select("quotes", filters)
    quotes = [Quote(**r) for r in quote_rows]

    # Recherche textuelle
    if search:
        q_lower = search.lower()
        quotes = [
            q for q in quotes
            if q_lower in q.client_name.lower()
            or (q.description and q_lower in q.description.lower())
        ]

    # Calcul du statut
    def compute_status(q: Quote) -> str:
        if q.invoice_amount is not None:
            if q.amount is None or q.amount == 0 or q.invoice_amount >= q.amount:
                return "Payée"
            return "Refusé"
        if q.signed_pdf_filename:
            return "Envoyé"
        dt_quote = datetime.strptime(q.quote_date, "%Y-%m-%d").date()
        if (datetime.now().date() - dt_quote).days > 30:
            return "Expiré"
        return "Brouillon"

    # Préparer les devis pour l'affichage et filtrer par statut
    display_quotes = []
    for q in quotes:
        st = compute_status(q)
        if status and st != status:
            continue
        q_dict = q.dict()
        q_dict["status"] = st
        q_dict["amount_ht"] = q.amount or 0.0
        q_dict["amount_ttc"] = round((q.amount or 0.0) * 1.2, 2)
        display_quotes.append(q_dict)

    # Statistiques
    stats = {
        "signed": len([q for q in quotes if q.signed_pdf_filename]),
        "pending": len([q for q in quotes if not q.signed_pdf_filename and (datetime.now().date() - datetime.strptime(q.quote_date, "%Y-%m-%d").date()).days <= 30]),
        "expired": len([q for q in quotes if not q.signed_pdf_filename and (datetime.now().date() - datetime.strptime(q.quote_date, "%Y-%m-%d").date()).days > 30]),
        "recent_total": len([q for q in quotes if (datetime.now().date() - datetime.strptime(q.quote_date, "%Y-%m-%d").date()).days <= 30]),
    }

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "companies": companies,
            "selected_company_id": company_id,
            "quotes": display_quotes,
            "statuses": ["Payée", "Refusé", "Envoyé", "Expiré", "Brouillon"],
            "selected_status": status,
            "stats": stats,
            "search_query": search or "",
        },
    )

# Liste des entreprises
@app.get("/companies")
def list_companies(request: Request):
    rows = supabase_table_select("companies")
    companies = [Company(**r) for r in rows]
    return templates.TemplateResponse("companies.html", {"request": request, "companies": companies})

# Formulaire de création d'entreprise
@app.get("/companies/new")
def new_company_form(request: Request):
    return templates.TemplateResponse("company_new.html", {"request": request})

@app.post("/companies/new")
async def create_company(name: str = Form(...)):
    supabase_table_insert("companies", {"name": name, "created_at": datetime.now().isoformat()})
    return RedirectResponse(url="/companies", status_code=303)

# Formulaire de création de devis
@app.get("/new")
def new_quote_form(request: Request):
    companies = [Company(**c) for c in supabase_table_select("companies")]
    return templates.TemplateResponse("new.html", {"request": request, "companies": companies})

# Création de devis (avec option PDF)
@app.post("/new")
async def create_quote(
    request: Request,
    client_name: str = Form(...),
    quote_date: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    amount: float = Form(0.0),
    company_id: int = Form(...),
    pdf_upload: UploadFile = File(None),
):
    now = datetime.now().isoformat()
    data = {
        "client_name": client_name,
        "quote_date": quote_date,
        "category": category,
        "description": description or None,
        "amount": amount,
        "company_id": company_id,
        "created_at": now,
        "updated_at": now,
    }
    created = supabase_table_insert("quotes", data)
    if not created:
        return RedirectResponse(url="/", status_code=303)
    quote_id = created["id"]
    quote = Quote(**created)
    pdf_filename: Optional[str] = None
    if pdf_upload and pdf_upload.filename:
        suffix = Path(pdf_upload.filename).suffix.lower()
        if suffix == ".pdf":
            pdf_name = f"imported_quote_{quote_id}_{datetime.now():%Y%m%d%H%M%S}{suffix}"
            pdf_path = DATA_DIR / pdf_name
            with open(pdf_path, "wb") as f:
                f.write(await pdf_upload.read())
            pdf_filename = pdf_name
    if not pdf_filename:
        pdf_filename = generate_pdf(quote)
    supabase_table_update("quotes", quote_id, {"pdf_filename": pdf_filename, "updated_at": now})
    return RedirectResponse(url=f"/?company_id={company_id}", status_code=303)

# Routes d'importation (Excel/CSV ou PDF)
@app.get("/import_excel")
def import_excel_form(request: Request):
    return templates.TemplateResponse("import_excel.html", {"request": request})

@app.post("/import_excel")
async def import_excel(request: Request, excel_file: UploadFile = File(...)):
    suffix = Path(excel_file.filename).suffix.lower()
    now = datetime.now().isoformat()

    # Cas d'un PDF : enregistrer le fichier et créer un devis minimal
    if suffix == ".pdf":
        content = await excel_file.read()
        pdf_name = f"imported_{datetime.now():%Y%m%d%H%M%S}.pdf"
        pdf_path = DATA_DIR / pdf_name
        with open(pdf_path, "wb") as f:
            f.write(content)
        created = supabase_table_insert("quotes", {
            "client_name": "Import PDF",
            "quote_date": datetime.now().strftime("%Y-%m-%d"),
            "category": "Import",
            "description": excel_file.filename,
            "amount": 0.0,
            "company_id": None,
            "pdf_filename": pdf_name,
            "created_at": now,
            "updated_at": now,
        })
        if not created:
            raise HTTPException(status_code=500, detail="Erreur lors de l'enregistrement du PDF")
        return RedirectResponse(url="/", status_code=303)

    # Cas Excel/CSV : lire le fichier et créer des devis
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

    for _, row_data in df.iterrows():
        client = str(row_data.get("client_name", "")).strip()
        date = str(row_data.get("quote_date", "")).strip()
        cat = str(row_data.get("category", "")).strip()
        desc = str(row_data.get("description", ""))
        amt = float(row_data.get("amount", 0.0))
        comp = row_data.get("company_id")
        if not client or not date or not cat:
            continue
        created = supabase_table_insert("quotes", {
            "client_name": client,
            "quote_date": date,
            "category": cat,
            "description": desc or None,
            "amount": amt,
            "company_id": int(comp) if comp else None,
            "created_at": now,
            "updated_at": now,
        })
        if not created:
            continue
        quote_id = created["id"]
        quote = Quote(**created)
        pdf_filename = generate_pdf(quote)
        supabase_table_update("quotes", quote_id, {"pdf_filename": pdf_filename, "updated_at": now})
    os.remove(tmp_path)
    return RedirectResponse(url="/", status_code=303)

# Détail du devis
@app.get("/quote/{quote_id}")
def quote_detail(request: Request, quote_id: int):
    row = supabase_table_get("quotes", quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse("quote_detail.html", {"request": request, "quote": quote})

# Télécharger le devis (signé ou non)
@app.get("/quote/{quote_id}/download")
def download_pdf(quote_id: int, signed: bool = False):
    row = supabase_table_get("quotes", quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    filename = quote.signed_pdf_filename if (signed and quote.signed_pdf_filename) else quote.pdf_filename
    if not filename:
        return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
    file_path = DATA_DIR / filename
    return FileResponse(path=file_path, media_type="application/pdf", filename=filename)

# Formulaire de signature
@app.get("/quote/{quote_id}/sign")
def sign_form(request: Request, quote_id: int):
    row = supabase_table_get("quotes", quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse("sign.html", {"request": request, "quote": quote})

# Enregistrer la signature
@app.post("/quote/{quote_id}/sign")
async def sign_quote(request: Request, quote_id: int, signature: UploadFile = File(...)):
    sig_path = save_signature(signature)
    if not sig_path:
        return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
    row = supabase_table_get("quotes", quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    signed_filename = generate_pdf(quote, signature_path=sig_path)
    now = datetime.now().isoformat()
    supabase_table_update("quotes", quote_id, {"signed_pdf_filename": signed_filename, "updated_at": now})
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)

# Enregistrer le montant facturé
@app.post("/quote/{quote_id}/invoice")
async def submit_invoice(
    request: Request,
    quote_id: int,
    invoice_amount: float = Form(...),
    invoice_comment: str = Form("")
):
    row = supabase_table_get("quotes", quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    now = datetime.now().isoformat()
    supabase_table_update(
        "quotes",
        quote_id,
        {
            "invoice_amount": invoice_amount,
            "invoice_comment": invoice_comment or None,
            "updated_at": now,
        },
    )
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)

# Démarrage local (facultatif)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
