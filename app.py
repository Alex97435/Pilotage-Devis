"""
Application de suivi et de centralisation des devis (version Supabase)
-------------------------------------------------------------------

Cette version de l'application remplace la base locale SQLite par
Supabase (PostgreSQL + stockage) pour persister les données des devis.
Les fichiers PDF et signatures continuent d'être stockés dans des
répertoires locaux (`static/uploads` et `static/signatures`), mais leurs
noms sont enregistrés en base via Supabase.

Pour déployer cette application sur Vercel, n'oubliez pas de
définir les variables d'environnement suivantes dans Vercel ou sur
votre machine locale :

    SUPABASE_URL : l'URL de votre projet Supabase
    SUPABASE_KEY : la clé API publique (ANON key) de Supabase

Vous pouvez obtenir ces valeurs dans votre tableau de bord Supabase
(Project settings > API). Ne les stockez pas directement dans le
code, afin de ne pas exposer vos secrets.

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

# Ces variables doivent être définies dans votre environnement
# (par exemple dans Vercel ou via export en local)
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
# Chemins et répertoires locaux
##############################

# Répertoire de base du fichier app.py
BASE_DIR = Path(__file__).resolve().parent
# Dossiers pour stocker les PDF et signatures
DATA_DIR = BASE_DIR / "static" / "uploads"
SIGNATURE_DIR = BASE_DIR / "static" / "signatures"
TEMPLATES_DIR = BASE_DIR / "templates"

# Créer les dossiers s'ils n'existent pas
DATA_DIR.mkdir(parents=True, exist_ok=True)
SIGNATURE_DIR.mkdir(parents=True, exist_ok=True)

##############################
# Fonctions utilitaires pour Supabase
##############################

def supabase_table_insert(data: Dict) -> Dict | None:
    """
    Insère un enregistrement dans la table `quotes` et renvoie
    l'objet créé (dict) ou None en cas d'erreur.
    """
    if not supabase:
        return None
    resp = supabase.table("quotes").insert(data).execute()
    return resp.data[0] if resp.data else None


def supabase_table_update(quote_id: int, updates: Dict) -> None:
    """
    Met à jour un enregistrement de la table `quotes`.
    """
    if not supabase:
        return
    supabase.table("quotes").update(updates).eq("id", quote_id).execute()


def supabase_table_select(filters: Dict | None = None) -> List[Dict]:
    """
    Sélectionne des enregistrements dans la table `quotes`.
    Les filtres peuvent contenir des clés 'quote_date' (préfixe AAAA-MM)
    et/ou 'category'.
    """
    if not supabase:
        return []
    query = supabase.table("quotes")
    if filters:
        for key, value in filters.items():
            if key == "quote_date":
                # Filtre par préfixe de date (AAAA-MM)
                query = query.like(key, f"{value}%")
            else:
                query = query.eq(key, value)
    resp = query.select("*").order("quote_date", desc=True).execute()
    return resp.data or []


def supabase_table_get(quote_id: int) -> Dict | None:
    """
    Récupère un devis unique par son identifiant.
    """
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
    quote_date: str  # format YYYY-MM-DD
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
        """Retourne le mois au format AAAA-MM."""
        return self.quote_date[:7]


##############################
# Fonctions de génération de PDF et d'enregistrement des signatures
##############################

def generate_pdf(quote: Quote, signature_path: Optional[Path] = None) -> str:
    """
    Génère un fichier PDF à partir des informations du devis.
    Le PDF est créé comme une image A4 (595x842 points) et sauvegardé
    dans `DATA_DIR`. Si un chemin de signature est fourni, l'image de
    signature est collée dans le coin inférieur droit.

    :param quote: Objet Quote contenant les données du devis.
    :param signature_path: Optionnel, chemin de l'image de signature.
    :return: Le nom de fichier du PDF généré.
    """
    width, height = 595, 842  # dimensions A4 à 72 DPI
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    # Charger une police TrueType ou utiliser la police par défaut
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 14)
    except Exception:
        font = ImageFont.load_default()

    # Construire les lignes du devis
    lines: List[str] = [
        f"Devis n° {quote.id}",
        f"Client : {quote.client_name}",
        f"Date : {quote.quote_date}",
        f"Catégorie : {quote.category}",
        f"Montant : {quote.amount:.2f} EUR",
        "",
        "Description :",
    ]
    # Ajouter la description sur plusieurs lignes
    if quote.description:
        for l in quote.description.split("\n"):
            lines.append(l)
    else:
        lines.append("(aucune description)")

    x_margin = 40
    y = 780  # position verticale initiale (par le bas)
    line_height = 20
    for line in lines:
        draw.text((x_margin, y), line, fill="black", font=font)
        y -= line_height

    # Ajouter la signature si fournie
    if signature_path and signature_path.exists():
        try:
            sig_img = Image.open(signature_path).convert("RGBA")
            max_width, max_height = 200, 100
            ratio = min(max_width / sig_img.width, max_height / sig_img.height, 1.0)
            new_size = (int(sig_img.width * ratio), int(sig_img.height * ratio))
            sig_resized = sig_img.resize(new_size, Image.LANCZOS)
            # Position en bas à droite
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
    """
    Enregistre un fichier de signature envoyé par l'utilisateur
    dans le dossier `SIGNATURE_DIR` et renvoie son chemin.
    Accepte uniquement les extensions PNG et JPEG.
    """
    try:
        suffix = Path(upload_file.filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            return None
        sig_name = f"sig_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"
        sig_path = SIGNATURE_DIR / sig_name
        with open(sig_path, "wb") as f:
            content = upload_file.file.read()
            f.write(content)
        return sig_path
    except Exception:
        return None


##############################
# Application FastAPI
##############################

app = FastAPI(title="Gestion des devis (Supabase)")

# Monter le dossier static (pour servir les PDF et signatures)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Configurer Jinja2 pour les templates
templates = Jinja2Templates(directory=TEMPLATES_DIR)


@app.get("/")
def index(request: Request, month: Optional[str] = None, category: Optional[str] = None):
    """
    Affiche la liste des devis avec des filtres optionnels par mois (AAAA-MM)
    et catégorie. Utilise Supabase pour récupérer les données.
    """
    filters: Dict[str, str] = {}
    if month:
        filters["quote_date"] = month
    if category:
        filters["category"] = category

    rows = supabase_table_select(filters)
    quotes = [Quote(**row) for row in rows]

    # Extraire toutes les catégories et tous les mois disponibles
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
    """Affiche le formulaire d'import de devis depuis un fichier Excel/CSV."""
    return templates.TemplateResponse("import_excel.html", {"request": request})


@app.post("/import_excel")
async def import_excel(request: Request, excel_file: UploadFile = File(...)):
    """
    Traite un fichier Excel ou CSV, importe chaque ligne comme un devis,
    génère un PDF et enregistre le nom du PDF dans la base Supabase.
    Les colonnes attendues sont : client_name, quote_date, category,
    description et amount.
    """
    import tempfile
    import pandas as pd
    suffix = Path(excel_file.filename).suffix.lower()
    now = datetime.now().isoformat()
    try:
        content = await excel_file.read()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        # Lecture du fichier selon son extension
        if suffix in [".xlsx", ".xls"]:
            df = pd.read_excel(tmp_path)
        elif suffix in [".csv", ".txt"]:
            df = pd.read_csv(tmp_path)
        else:
            return RedirectResponse(url="/import_excel", status_code=303)
        # Importer chaque ligne
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
            # Générer le PDF et mettre à jour
            pdf_filename = generate_pdf(quote)
            supabase_table_update(quote_id, {"pdf_filename": pdf_filename, "updated_at": now})
            imported_count += 1
        # Supprimer le fichier temporaire
        os.remove(tmp_path)
        return RedirectResponse(url="/", status_code=303)
    except Exception:
        return RedirectResponse(url="/import_excel", status_code=303)


@app.get("/new")
def new_quote_form(request: Request):
    """Affiche le formulaire de création d'un nouveau devis."""
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
    """
    Crée un devis dans Supabase. Si un PDF est fourni, il est enregistré
    tel quel ; sinon, un PDF est généré à partir des données.
    """
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
    # Si un PDF est téléversé
    if pdf_upload and pdf_upload.filename:
        suffix = Path(pdf_upload.filename).suffix.lower()
        if suffix == ".pdf":
            pdf_name = f"imported_quote_{quote_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}{suffix}"
            pdf_path = DATA_DIR / pdf_name
            content = await pdf_upload.read()
            with open(pdf_path, "wb") as f:
                f.write(content)
            pdf_filename = pdf_name
    # Sinon, générer le PDF
    if not pdf_filename:
        pdf_filename = generate_pdf(quote)
    # Mettre à jour la ligne Supabase
    supabase_table_update(quote_id, {"pdf_filename": pdf_filename, "updated_at": now})
    return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)


@app.get("/quote/{quote_id}")
def quote_detail(request: Request, quote_id: int):
    """Affiche les détails d'un devis."""
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse("quote_detail.html", {"request": request, "quote": quote})


@app.get("/quote/{quote_id}/download")
def download_pdf(quote_id: int, signed: bool = False):
    """
    Permet de télécharger le PDF (signé ou non). Le fichier est
    récupéré dans `DATA_DIR` selon le nom enregistré en base.
    """
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
    """Affiche le formulaire pour téléverser une signature."""
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    return templates.TemplateResponse("sign.html", {"request": request, "quote": quote})


@app.post("/quote/{quote_id}/sign")
async def sign_quote(request: Request, quote_id: int, signature: UploadFile = File(...)):
    """
    Applique la signature téléversée au devis, génère un PDF signé et
    met à jour la base Supabase avec le nom du nouveau fichier.
    """
    sig_path = save_signature(signature)
    if not sig_path:
        return RedirectResponse(url=f"/quote/{quote_id}", status_code=303)
    row = supabase_table_get(quote_id)
    if not row:
        return RedirectResponse(url="/", status_code=303)
    quote = Quote(**row)
    # Générer le PDF signé
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
    """
    Enregistre le montant de la facture finale et un commentaire. Les valeurs
    sont stockées dans la base Supabase.
    """
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
# Démarrage local (facultatif)
##############################

if __name__ == "__main__":
    import uvicorn

    # Pour lancer l'application en local via 'python app.py'
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
