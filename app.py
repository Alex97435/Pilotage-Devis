"""
Application de suivi et de centralisation des devis (version Supabase)
avec gestion des entreprises.
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
from supabase import create_client, Client  # type: ignore

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

# Accueil filtré par entreprise (optionnel)
@app.get("/")
def index(
    request: Request,
    company_id: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
):
    """
    Affiche les devis pour une entreprise donnée (company_id).
    Si aucun company_id n'est fourni, tous les devis sont affichés.
    """
    # Récupérer les entreprises pour le menu
    company_rows = supabase_table_select("companies")
    companies = [Company(**c) for c in company_rows]

    # Préparer les filtres pour les devis
    filters: Dict = {}
    if company_id:
        filters["company_id"] = company_id

    quote_rows = supabase_table_select("quotes", filters)
    quotes = [Quote(**r) for r in quote_rows]

    # Recherche simple
    if search:
        q_lower = search.lower()
        quotes = [
            q for q in quotes
            if q_lower in q.client_name.lower()
            or (q.description and q_lower in q.description.lower())
        ]

    # Déterminer le statut pour chaque devis
    def compute_status(q: Quote) -> str:
        if q.invoice_amount is not None:
            if q.amount is None or q.amount == 0 or q.invoice_amount >= q.amount:
                return "Payée"
            return "Refusé"
        if q.signed_pdf_filename:
            return "Envoyé"
        # Test expiration (>30 jours)
        dt_quote = datetime.strptime(q.quote_date, "%Y-%m-%d").date()
        if (datetime.now().date() - dt_quote).days > 30:
            return "Expiré"
        return "Brouillon"

    # Filtrer par statut
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

    # Compter les statuts
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

# Création d'un devis (liée à une entreprise)
@app.get("/new")
def new_quote_form(request: Request):
    companies = [Company(**c) for c in supabase_table_select("companies")]
    return templates.TemplateResponse("new.html", {"request": request, "companies": companies})

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

# Routes /quote/{id}, /sign, /invoice, etc. demeurent identiques à celles fournies précédemment.
