from datetime import datetime, time, timedelta
from io import BytesIO
import hmac
import logging
import mimetypes
import os
from pathlib import Path
import re
import secrets
from uuid import uuid4
import warnings

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request, send_file,
    url_for,
)
from flask_login import (
    LoginManager, UserMixin, current_user, login_required, login_user, logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from pdf2image import convert_from_bytes
from pdf2image.exceptions import (
    PDFInfoNotInstalledError, PDFPageCountError, PDFPopplerTimeoutError,
)
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError
import pytesseract
from sqlalchemy import case, func, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv


basedir = Path(__file__).resolve().parent
load_dotenv(basedir / ".env")
instance_dir = basedir / "instance"
instance_dir.mkdir(exist_ok=True)
upload_folder = Path(
    os.environ.get("UPLOAD_FOLDER", str(instance_dir / "uploads"))
).expanduser()
if not upload_folder.is_absolute():
    upload_folder = basedir / upload_folder

app = Flask(__name__, template_folder="templates")
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)),
    UPLOAD_FOLDER=str(upload_folder),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SAMESITE="Lax",
)

environment = os.environ.get("APP_ENV", "development").lower()
database_url = os.environ.get(
    "DATABASE_URL", f"sqlite:///{(basedir / 'autentke.db').as_posix()}"
)
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
if environment == "production":
    missing_settings = [
        name for name, value in (
            ("SECRET_KEY", os.environ.get("SECRET_KEY")),
            ("ADMIN_USERNAME", os.environ.get("ADMIN_USERNAME")),
            ("ADMIN_PASSWORD_HASH", os.environ.get("ADMIN_PASSWORD_HASH")),
            ("DATABASE_URL", os.environ.get("DATABASE_URL")),
        ) if not value
    ]
    if missing_settings:
        raise RuntimeError(
            "Missing production environment settings: " + ", ".join(missing_settings)
        )
    app.config["SESSION_COOKIE_SECURE"] = True
app.config["SQLALCHEMY_DATABASE_URI"] = database_url

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Entre para acessar o painel administrativo."
login_manager.login_message_category = "warning"

UPLOAD_DIR = Path(app.config["UPLOAD_FOLDER"]).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = app.config["MAX_CONTENT_LENGTH"]
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".pdf"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
Image.MAX_IMAGE_PIXELS = 30_000_000
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

LOJA_CONFIG = {
    "nome": "Autentke",
    "instagram": "autentke.joias",
    "meta_padrao": 5000.0,
    "percentual_reinvestimento": 0.6,
    "whatsapp": re.sub(r"\D", "", os.environ.get("LOJA_WHATSAPP", "")),
}


class AdminUser(UserMixin):
    def __init__(self, username):
        self.id = username


@login_manager.user_loader
def load_user(user_id):
    if ADMIN_USERNAME and hmac.compare_digest(user_id, ADMIN_USERNAME):
        return AdminUser(user_id)
    return None


def converter_link_drive(url):
    if not url:
        return ""
    drive_patterns = (
        r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",
        r"docs\.google\.com/uc\?id=([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/uc\?export=view&id=([a-zA-Z0-9_-]+)",
    )
    for pattern in drive_patterns:
        match = re.search(pattern, url)
        if match:
            return f"https://docs.google.com/uc?export=view&id={match.group(1)}"
    return url


class Colecao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    qtd_pecas = db.Column(db.Integer, nullable=False)
    valor_frete = db.Column(db.Float, default=0.0)
    valor_mimos = db.Column(db.Float, default=0.0)
    arquivada = db.Column(db.Boolean, default=False)
    produtos = db.relationship("Produto", backref="colecao", lazy=True)

    @property
    def rateio_unidade(self):
        if self.qtd_pecas and self.qtd_pecas > 0:
            return (self.valor_frete + self.valor_mimos) / self.qtd_pecas
        return 0.0


class Produto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    categoria = db.Column(db.String(50), default="Outros")
    foto_url = db.Column(db.String(500))
    custo_base = db.Column(db.Float, nullable=False)
    markup = db.Column(db.Float, default=2.5)
    preco_venda_manual = db.Column(db.Float)
    preco_venda = db.Column(db.Float)
    vendido = db.Column(db.String(3), default="NÃO")
    colecao_id = db.Column(db.Integer, db.ForeignKey("colecao.id"), nullable=False)

    cliente_nome = db.Column(db.String(100))
    cliente_telefone = db.Column(db.String(20))
    forma_pagamento = db.Column(db.String(50))
    num_parcelas = db.Column(db.Integer, default=1)
    origem_venda = db.Column(db.String(50))
    tag_campanha = db.Column(db.String(50))
    data_venda = db.Column(db.DateTime)
    desconto_aplicado = db.Column(db.Float, default=0.0)
    documento_pedido_id = db.Column(
        db.Integer, db.ForeignKey("documento_pedido.id"), nullable=True
    )
    documento_pedido = db.relationship("DocumentoPedido", backref="produtos")
    anexo = db.relationship(
        "AnexoProduto", backref="produto", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def preco_ideal(self):
        if self.preco_venda_manual and self.preco_venda_manual > 0:
            return self.preco_venda_manual
        rateio = self.colecao.rateio_unidade if self.colecao else 0.0
        return (self.custo_base + rateio) * self.markup

    @property
    def lucro_liquido(self):
        if self.vendido == "SIM":
            rateio = self.colecao.rateio_unidade if self.colecao else 0.0
            return (self.preco_venda or 0.0) - (self.custo_base + rateio)
        return 0.0


class AnexoProduto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    produto_id = db.Column(
        db.Integer, db.ForeignKey("produto.id"), nullable=False, unique=True
    )
    nome_original = db.Column(db.String(255), nullable=False)
    nome_armazenado = db.Column(db.String(36), nullable=False, unique=True)
    tipo = db.Column(db.String(100), nullable=False)
    data_criacao = db.Column(db.DateTime, default=datetime.now, nullable=False)


class DocumentoPedido(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome_original = db.Column(db.String(255), nullable=False)
    nome_armazenado = db.Column(db.String(36), nullable=False, unique=True)
    tipo = db.Column(db.String(100), nullable=False)
    data_criacao = db.Column(db.DateTime, default=datetime.now, nullable=False)


class DespesaFixa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(100), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    categoria = db.Column(db.String(50), default="Operacional")
    data = db.Column(db.DateTime, default=datetime.now)


class ItemBrinde(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False, unique=True)
    unidade = db.Column(db.String(12), nullable=False)
    quantidade_estoque = db.Column(db.Float, nullable=False, default=0)
    consumo_por_venda = db.Column(db.Float, nullable=False, default=0)
    custo_unitario = db.Column(db.Float, nullable=False, default=0)
    ativo = db.Column(db.Boolean, nullable=False, default=True)


class ConsumoBrinde(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    produto_id = db.Column(db.Integer, db.ForeignKey("produto.id"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("item_brinde.id"), nullable=False)
    quantidade = db.Column(db.Float, nullable=False)
    custo_unitario = db.Column(db.Float, nullable=False)
    data = db.Column(db.DateTime, nullable=False, default=datetime.now)


class MetaMensal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    valor_meta = db.Column(db.Float, nullable=False)
    mes_ano = db.Column(db.String(7), unique=True)


def parse_moeda(value, field_name):
    raw = (value or "").strip().replace("R$", "").replace(" ", "")
    if not raw:
        raise ValueError(f"Informe {field_name}.")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        amount = float(raw)
    except ValueError as exc:
        raise ValueError(f"Informe um valor válido para {field_name}.") from exc
    if not 0 <= amount <= 1_000_000_000:
        raise ValueError(f"{field_name.capitalize()} deve ser maior ou igual a zero.")
    return amount


def validar_texto(value, field_name, max_length):
    value = (value or "").strip()
    if not value:
        raise ValueError(f"Informe {field_name}.")
    if len(value) > max_length:
        raise ValueError(f"{field_name.capitalize()} deve ter até {max_length} caracteres.")
    return value


def migrar_esquema_sqlite(engine):
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    migrations = []
    if inspector.has_table("colecao"):
        existing_columns = {column["name"] for column in inspector.get_columns("colecao")}
        if "arquivada" not in existing_columns:
            migrations.append(
                "ALTER TABLE colecao ADD COLUMN arquivada BOOLEAN NOT NULL DEFAULT 0"
            )
    if inspector.has_table("produto"):
        existing_columns = {column["name"] for column in inspector.get_columns("produto")}
        if "documento_pedido_id" not in existing_columns:
            migrations.append(
                "ALTER TABLE produto ADD COLUMN documento_pedido_id "
                "INTEGER REFERENCES documento_pedido(id)"
            )
    if migrations:
        with engine.begin() as connection:
            for migration in migrations:
                connection.execute(text(migration))
        if "arquivada" in migrations[0]:
            logger.info("Migração local aplicada: coluna colecao.arquivada")
        if any("documento_pedido_id" in migration for migration in migrations):
            logger.info("Migração local aplicada: vínculo de anexo de pedido")


def calcular_metricas(apenas_atual=True):
    hoje = datetime.now()
    inicio = datetime(hoje.year, hoje.month, 1) if apenas_atual else None
    fim = (datetime(hoje.year + (hoje.month == 12), hoje.month % 12 + 1, 1)
           if apenas_atual else None)

    vendas_query = Produto.query.filter(Produto.vendido == "SIM")
    despesas_query = DespesaFixa.query
    if apenas_atual:
        vendas_query = vendas_query.join(Colecao).filter(
            Colecao.arquivada.is_(False),
            Produto.data_venda >= inicio,
            Produto.data_venda < fim,
        )
        despesas_query = despesas_query.filter(
            DespesaFixa.data >= inicio, DespesaFixa.data < fim
        )

    total_vendas = vendas_query.count()
    faturamento = db.session.query(
        func.coalesce(func.sum(Produto.preco_venda), 0.0)
    ).select_from(Produto)
    lucro = db.session.query(
        func.coalesce(
            func.sum(
                Produto.preco_venda - Produto.custo_base
                - case(
                    (Colecao.qtd_pecas > 0,
                     (Colecao.valor_frete + Colecao.valor_mimos) / Colecao.qtd_pecas),
                    else_=0.0,
                )
            ),
            0.0,
        )
    ).select_from(Produto).join(Colecao)
    if apenas_atual:
        faturamento = faturamento.join(Colecao).filter(
            Colecao.arquivada.is_(False),
            Produto.vendido == "SIM",
            Produto.data_venda >= inicio,
            Produto.data_venda < fim,
        )
        lucro = lucro.filter(
            Colecao.arquivada.is_(False),
            Produto.vendido == "SIM",
            Produto.data_venda >= inicio,
            Produto.data_venda < fim,
        )
    else:
        faturamento = faturamento.filter(Produto.vendido == "SIM")
        lucro = lucro.filter(Produto.vendido == "SIM")

    total_faturamento = float(faturamento.scalar() or 0)
    lucro_bruto = float(lucro.scalar() or 0)
    total_despesas = float(
        despesas_query.with_entities(
            func.coalesce(func.sum(DespesaFixa.valor), 0.0)
        ).scalar() or 0
    )
    return {
        "faturamento": total_faturamento,
        "lucro_liquido": lucro_bruto - total_despesas,
        "total_vendas": total_vendas,
        "ticket_medio": total_faturamento / total_vendas if total_vendas else 0.0,
        "total_despesas": total_despesas,
    }


def ler_anexo(upload):
    if not upload or not upload.filename:
        raise ValueError("Selecione uma foto ou PDF.")
    filename = secure_filename(upload.filename)
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("Formato não permitido. Envie JPG, PNG, WEBP ou PDF.")
    content = upload.read(MAX_UPLOAD_BYTES + 1)
    upload.stream.seek(0)
    if not content:
        raise ValueError("O arquivo está vazio.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("O arquivo excede o limite de 10 MB.")
    if extension == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("O conteúdo do arquivo não é um PDF válido.")
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("A imagem enviada é inválida ou não suportada.") from exc
    return content, filename, extension


def ler_foto_produto(upload):
    if not upload or not upload.filename:
        return None
    filename = secure_filename(upload.filename)
    extension = Path(filename).suffix.lower()
    if extension not in PHOTO_EXTENSIONS:
        raise ValueError("A foto da joia deve estar no formato JPG, PNG ou WEBP.")
    content = upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("A foto excede o limite de 10 MB.")
    if not content:
        raise ValueError("A foto enviada está vazia.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                extension = {
                    "JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
                }.get(image.format)
                if not extension:
                    raise ValueError("A foto deve ser JPG, PNG ou WEBP.")
                image.verify()
    except (
        UnidentifiedImageError, OSError, Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("A foto enviada é inválida ou não suportada.") from exc
    return content, extension


def preprocessar_imagem(image):
    image = ImageOps.exif_transpose(image).convert("L")
    image.thumbnail((3200, 3200))
    if image.width < 1600:
        scale = min(3, 1600 / image.width, 3200 / image.height)
        if scale > 1:
            image = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.Resampling.LANCZOS,
            )
    return image


def interpretar_valor_ocr(value):
    cleaned = re.sub(r"(?i)r\s*\$|[^0-9,.]", "", value)
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    elif "." not in cleaned and len(cleaned) > 2:
        cleaned = f"{cleaned[:-2]}.{cleaned[-2:]}"
    try:
        result = float(cleaned)
    except ValueError:
        return None
    return result if 0 < result <= 1_000_000_000 else None


def parse_tabela_ocr(words, image_width, image_height):
    price_words = [
        word for word in words
        if 0.80 <= word[0] / image_width < 0.89
        and 0.24 <= word[1] / image_height <= 0.93
        and any(char.isdigit() for char in word[2])
    ]
    anchors = []
    for word in sorted(price_words, key=lambda item: item[1]):
        if not anchors or word[1] - anchors[-1][0] > image_height * 0.012:
            anchors.append([word[1], [word]])
        else:
            anchors[-1][1].append(word)
            anchors[-1][0] = round(
                sum(token[1] for token in anchors[-1][1]) / len(anchors[-1][1])
            )
    if len(anchors) < 3:
        return []

    rows = []
    for row_index, (anchor_y, row_price_words) in enumerate(anchors):
        previous_y = anchors[row_index - 1][0] if row_index else image_height * 0.25
        next_y = anchors[row_index + 1][0] if row_index + 1 < len(anchors) else image_height * 0.94
        top = (previous_y + anchor_y) / 2
        bottom = (anchor_y + next_y) / 2

        description_words = [
            word for word in words
            if 0.31 <= word[0] / image_width < 0.59
            and top <= word[1] < bottom
            and word[3] >= 25
        ]
        description_lines = []
        for word in sorted(description_words, key=lambda item: item[1]):
            line = next(
                (line for line in description_lines
                 if abs(word[1] - line[0]) <= image_height * 0.006),
                None,
            )
            if line is None:
                description_lines.append([word[1], [word]])
            else:
                line[1].append(word)
                line[0] = round(sum(token[1] for token in line[1]) / len(line[1]))
        description = " ".join(
            " ".join(word[2] for word in sorted(line_words, key=lambda item: item[0]))
            for _, line_words in sorted(description_lines, key=lambda line: line[0])
        )
        description = re.sub(r"\s+", " ", description).strip(" .:-")
        if not description:
            continue

        price_text = " ".join(word[2] for word in sorted(row_price_words))
        unit_cost = interpretar_valor_ocr(price_text)
        base_words = [
            word for word in words
            if 0.66 <= word[0] / image_width < 0.75
            and abs(word[1] - anchor_y) <= image_height * 0.02
        ]
        discount_words = [
            word for word in words
            if 0.75 <= word[0] / image_width < 0.80
            and abs(word[1] - anchor_y) <= image_height * 0.02
        ]
        base_price = next(
            (interpreted for word in sorted(base_words, key=lambda item: item[0])
             if (interpreted := interpretar_valor_ocr(word[2])) is not None),
            None,
        )
        discount_text = "".join(word[2] for word in discount_words)
        discount_match = re.search(r"\d{1,2}(?:[,.]\d{1,2})?", discount_text)
        if discount_match:
            try:
                discount = float(discount_match.group().replace(",", "."))
            except ValueError:
                discount = None
            if discount is not None and 0 <= discount <= 100 and base_price is not None:
                discounted_price = base_price * (1 - discount / 100)
                if discounted_price > 0 and (
                    unit_cost is None
                    or (
                        abs(unit_cost - discounted_price) >= 0.99
                        and unit_cost * 0.75 <= discounted_price <= unit_cost * 1.25
                    )
                ):
                    unit_cost = discounted_price

        normalized = description.upper()
        if "BRINCO" in normalized:
            category = "Brincos"
        elif "ANEL" in normalized:
            category = "Anéis"
        elif "PIERCING" in normalized:
            category = "Piercings"
        elif "PULSEIRA" in normalized:
            category = "Pulseiras"
        elif "COLAR" in normalized:
            category = "Colares"
        else:
            category = "Outros"
        is_packaging = bool(re.search(r"\b(BAG|ZIPLOCK|EMBALAGEM|SACOLA)\b", normalized))
        rows.append({
            "nome": description[:100],
            "custo_base": round(unit_cost, 2) if unit_cost is not None else None,
            "categoria": category,
            "incluido": not is_packaging,
            "revisar": unit_cost is None or category == "Outros",
        })
    return rows


def extrair_itens_anexo(content, extension):
    if extension == ".pdf":
        reader = PdfReader(BytesIO(content))
        if len(reader.pages) > 10:
            raise ValueError("O PDF pode ter no máximo 10 páginas.")
        images = convert_from_bytes(content, first_page=1, last_page=10, dpi=180)
    else:
        with Image.open(BytesIO(content)) as image:
            images = [image.copy()]

    items = []
    for image in images:
        prepared = preprocessar_imagem(image)
        width, height = prepared.size
        description_box = (
            int(width * 0.30), int(height * 0.25),
            int(width * 0.60), int(height * 0.93),
        )
        price_box = (
            int(width * 0.64), int(height * 0.25),
            int(width * 0.90), int(height * 0.93),
        )
        words = []
        for box in (description_box, price_box):
            cropped = prepared.crop(box)
            data = pytesseract.image_to_data(
                cropped, lang="por+eng", config="--psm 6",
                output_type=pytesseract.Output.DICT,
            )
            for index, raw_text in enumerate(data["text"]):
                token = raw_text.strip()
                if token:
                    words.append((
                        int(data["left"][index]) + box[0],
                        int(data["top"][index]) + box[1],
                        token,
                        float(data["conf"][index]),
                    ))
        items.extend(parse_tabela_ocr(words, width, height))
    return items


def extrair_texto_anexo(content, extension):
    try:
        if extension == ".pdf":
            reader = PdfReader(BytesIO(content))
            if len(reader.pages) > 10:
                raise ValueError("O PDF pode ter no máximo 10 páginas.")
            text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
            if text:
                return text
            images = convert_from_bytes(content, first_page=1, last_page=5, dpi=180)
            return "\n".join(
                pytesseract.image_to_string(preprocessar_imagem(image), lang="por+eng")
                for image in images
            )
        with Image.open(BytesIO(content)) as image:
            return pytesseract.image_to_string(
                preprocessar_imagem(image), lang="por+eng"
            )
    except ValueError:
        raise
    except (
        OSError, RuntimeError, PdfReadError, PDFInfoNotInstalledError,
        PDFPageCountError, PDFPopplerTimeoutError, pytesseract.TesseractError,
    ) as exc:
        logger.exception("Falha na leitura local do anexo")
        raise RuntimeError(
            "Não foi possível ler o arquivo. Verifique a instalação do Tesseract "
            "e do Poppler para PDFs escaneados."
        ) from exc


def sugerir_dados(text):
    lines = [re.sub(r"\s+", " ", line).strip(" :-") for line in text.splitlines()]
    amount = r"(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:[.,]\d{2}))"
    amount_patterns = (
        re.compile(
            rf"(?i)(?:pre[cç]o(?:\s+unit[aá]rio)?|valor(?:\s+de\s+compra)?|"
            rf"custo|total|compra)\s*:?\s*(?:r\s*\$?\s*)?{amount}"
        ),
        re.compile(rf"(?i)r\s*\$?\s*{amount}"),
    )
    cost = None
    for line in lines:
        for amount_pattern in amount_patterns:
            match = amount_pattern.search(line)
            if not match:
                continue
            try:
                candidate = parse_moeda(match.group(1), "custo")
            except ValueError:
                continue
            if candidate > 0:
                cost = candidate
                break
        if cost is not None:
            break
    name = None
    for line in lines:
        normalized = re.sub(r"(?i)^(?:produto|nome|descri[cç][aã]o)\s*:?\s*", "", line)
        if (
            3 <= len(normalized) <= 100
            and not re.search(r"(?i)(r\s*\$|pre[cç]o|valor|custo|total|cnpj|cpf)", normalized)
            and not re.fullmatch(r"[\d\s.,/-]+", normalized)
        ):
            name = normalized
            break
    return {"nome": name or "", "custo_base": cost}


def salvar_anexo(upload, produto):
    content, original_name, extension = ler_anexo(upload)
    destination = salvar_anexo_conteudo(
        produto, content, original_name, extension, upload.mimetype
    )
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        destination.unlink(missing_ok=True)
        raise
    except OSError:
        destination.unlink(missing_ok=True)
        raise


def salvar_anexo_conteudo(produto, content, original_name, extension, mimetype=None):
    stored_name = f"{uuid4().hex}{extension}"
    destination = UPLOAD_DIR / stored_name
    destination.write_bytes(content)
    db.session.add(AnexoProduto(
        produto=produto,
        nome_original=original_name[:255],
        nome_armazenado=stored_name,
        tipo=mimetypes.guess_type(original_name)[0] or mimetype or "application/octet-stream",
    ))
    return destination


def salvar_foto_produto(upload):
    validated = ler_foto_produto(upload)
    if not validated:
        return None, None
    content, extension = validated
    foto_dir = UPLOAD_DIR / "fotos"
    foto_dir.mkdir(parents=True, exist_ok=True)
    stored_name = f"{uuid4().hex}{extension}"
    destination = foto_dir / stored_name
    destination.write_bytes(content)
    return url_for("foto_produto", filename=stored_name), destination


def salvar_documento_pedido(content, original_name, extension, mimetype=None):
    stored_name = f"{uuid4().hex}{extension}"
    destination = UPLOAD_DIR / stored_name
    destination.write_bytes(content)
    documento = DocumentoPedido(
        nome_original=original_name[:255],
        nome_armazenado=stored_name,
        tipo=mimetypes.guess_type(original_name)[0] or mimetype or "application/octet-stream",
    )
    db.session.add(documento)
    return documento, destination


@login_manager.unauthorized_handler
def unauthorized():
    return redirect(url_for("login", next=request.full_path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        valid_username = ADMIN_USERNAME and hmac.compare_digest(username, ADMIN_USERNAME)
        try:
            valid_password = ADMIN_PASSWORD_HASH and check_password_hash(
                ADMIN_PASSWORD_HASH, password
            )
        except (ValueError, TypeError):
            valid_password = False
        if valid_username and valid_password:
            login_user(AdminUser(ADMIN_USERNAME), remember=False)
            next_url = request.args.get("next", "")
            if (
                next_url.startswith("/")
                and not next_url.startswith("//")
                and "\\" not in next_url
            ):
                return redirect(next_url)
            return redirect(url_for("index"))
        flash("Usuário ou senha inválidos.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Sessão encerrada.", "success")
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    mes_atual = datetime.now().strftime("%Y-%m")
    search_query = request.args.get("q", "").strip()[:100]
    cat_filter = request.args.get("categoria", "").strip()[:50]
    query_estoque = Produto.query.join(Colecao).filter(
        Produto.vendido == "NÃO", Colecao.arquivada.is_(False)
    )
    if search_query:
        query_estoque = query_estoque.filter(Produto.nome.ilike(f"%{search_query}%"))
    if cat_filter:
        query_estoque = query_estoque.filter(Produto.categoria == cat_filter)
    estoque_atual = query_estoque.options(
        joinedload(Produto.colecao)
    ).order_by(Produto.nome).all()
    estoque_antigo = Produto.query.join(Colecao).filter(
        Produto.vendido == "NÃO", Colecao.arquivada.is_(True)
    ).options(joinedload(Produto.colecao)).order_by(Produto.nome).all()

    inicio_mes = datetime(datetime.now().year, datetime.now().month, 1)
    proximo_mes = datetime(
        datetime.now().year + (datetime.now().month == 12),
        datetime.now().month % 12 + 1, 1,
    )
    vendas_atuais = Produto.query.filter(
        Produto.vendido == "SIM",
        Produto.colecao.has(Colecao.arquivada.is_(False)),
        Produto.data_venda >= inicio_mes,
        Produto.data_venda < proximo_mes,
    ).options(joinedload(Produto.colecao)).order_by(Produto.data_venda.desc()).all()
    metricas = calcular_metricas(apenas_atual=True)
    meta_obj = MetaMensal.query.filter_by(mes_ano=mes_atual).first()
    meta_valor = meta_obj.valor_meta if meta_obj else LOJA_CONFIG["meta_padrao"]
    progresso_meta = metricas["faturamento"] / meta_valor * 100 if meta_valor else 0
    msg_meta = (
        "META BATIDA! Parabéns! 🎉" if progresso_meta >= 100
        else "Faltam R$ %.2f para bater a meta." % max(0, meta_valor - metricas["faturamento"])
    )
    colecoes = Colecao.query.order_by(Colecao.arquivada, Colecao.nome).all()
    despesas = DespesaFixa.query.order_by(DespesaFixa.data.desc()).limit(50).all()
    brindes = ItemBrinde.query.order_by(ItemBrinde.nome).all()
    estrelas = (
        db.session.query(Produto.nome, func.count(Produto.id))
        .filter_by(vendido="SIM").group_by(Produto.nome)
        .order_by(func.count(Produto.id).desc()).limit(3).all()
    )
    return render_template(
        "index.html", m=metricas, estoque=estoque_atual,
        estoque_antigo=estoque_antigo, vendas=vendas_atuais, colecoes=colecoes,
        despesas=despesas, brindes=brindes, meta_valor=meta_valor,
        progresso_meta=progresso_meta,
        msg_meta=msg_meta, estrelas=estrelas, loja=LOJA_CONFIG,
        search_query=search_query, cat_filter=cat_filter,
    )


@app.route("/api/metricas/total")
@login_required
def api_metricas_total():
    return jsonify(calcular_metricas(apenas_atual=False))


@app.route("/colecao/arquivar/<int:id>", methods=["POST"])
@login_required
def arquivar_colecao(id):
    colecao = db.get_or_404(Colecao, id)
    colecao.arquivada = not colecao.arquivada
    db.session.commit()
    flash("Status do lote atualizado.", "success")
    return redirect(url_for("index"))


@app.route("/produto/extrair-anexo", methods=["POST"])
@login_required
def extrair_anexo():
    try:
        content, _, extension = ler_anexo(request.files.get("anexo"))
        items = extrair_itens_anexo(content, extension)
        if items:
            return jsonify({"items": items})
        suggestions = sugerir_dados(extrair_texto_anexo(content, extension))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    if not suggestions["nome"] and suggestions["custo_base"] is None:
        return jsonify({
            "error": "Não encontrei nome ou custo legível. Preencha os campos manualmente."
        }), 422
    suggestions["nome_encontrado"] = bool(suggestions["nome"])
    suggestions["custo_encontrado"] = suggestions["custo_base"] is not None
    return jsonify(suggestions)


@app.route("/produto/add-lote", methods=["POST"])
@login_required
def add_produtos_lote():
    saved_files = []
    try:
        names = request.form.getlist("produto_nome[]")
        costs = request.form.getlist("produto_custo[]")
        categories = request.form.getlist("produto_categoria[]")
        manual_prices = request.form.getlist("produto_manual[]")
        photos = request.files.getlist("produto_foto[]")
        selected = {int(value) for value in request.form.getlist("produto_incluir[]")}
        if not names or len(names) > 100 or not (
            len(names) == len(costs) == len(categories) == len(manual_prices)
        ):
            raise ValueError("A lista de itens está vazia ou incompleta.")
        if photos and len(photos) != len(names):
            raise ValueError("As fotos não correspondem à lista de joias.")
        if not selected or any(index < 0 or index >= len(names) for index in selected):
            raise ValueError("Selecione ao menos uma joia para cadastrar.")
        colecao_id = request.form.get("colecao_id", type=int)
        colecao = db.session.get(Colecao, colecao_id) if colecao_id else None
        if not colecao or colecao.arquivada:
            raise ValueError("Selecione um lote ativo.")
        markup = float(request.form.get("markup") or 2.5)
        if not 0.01 <= markup <= 100:
            raise ValueError("O markup deve estar entre 0,01 e 100.")
        upload = request.files.get("anexo")
        content, original_name, extension = ler_anexo(upload)

        produtos = []
        for index in sorted(selected):
            nome = validar_texto(names[index], "nome da joia", 100)
            custo = parse_moeda(costs[index], "custo")
            categoria = validar_texto(categories[index], "categoria", 50)
            manual = (
                parse_moeda(manual_prices[index], "preço manual")
                if manual_prices[index].strip() else None
            )
            produtos.append(Produto(
                nome=nome, categoria=categoria, custo_base=custo,
                colecao_id=colecao.id, markup=markup,
                preco_venda_manual=manual,
            ))
        db.session.add_all(produtos)
        db.session.flush()
        documento, file_path = salvar_documento_pedido(
            content, original_name, extension, upload.mimetype
        )
        saved_files.append(file_path)
        db.session.flush()
        for produto in produtos:
            produto.documento_pedido = documento
        for index, produto in zip(sorted(selected), produtos):
            if photos:
                photo_url, photo_path = salvar_foto_produto(photos[index])
                if photo_path:
                    saved_files.append(photo_path)
                    produto.foto_url = photo_url
        db.session.commit()
        flash(
            f"{len(produtos)} joia(s) cadastrada(s) com o anexo do pedido.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        flash(str(exc), "error")
    except (SQLAlchemyError, OSError):
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        logger.exception("Falha ao cadastrar joias do pedido")
        flash("Não foi possível cadastrar os itens do pedido. Tente novamente.", "error")
    return redirect(url_for("index"))


@app.route("/produto/add", methods=["POST"])
@login_required
def add_produto():
    saved_files = []
    try:
        nome = validar_texto(request.form.get("nome"), "nome da joia", 100)
        custo = parse_moeda(request.form.get("custo_base"), "custo")
        colecao_id = request.form.get("colecao_id", type=int)
        markup = float(request.form.get("markup") or 2.5)
        if not colecao_id or not Colecao.query.filter_by(id=colecao_id, arquivada=False).first():
            raise ValueError("Selecione um lote ativo.")
        if not 0.01 <= markup <= 100:
            raise ValueError("O markup deve estar entre 0,01 e 100.")
        manual_value = request.form.get("preco_venda_manual", "").strip()
        manual = parse_moeda(manual_value, "preço de venda") if manual_value else None
        novo = Produto(
            nome=nome, categoria=(request.form.get("categoria") or "Outros").strip()[:50],
            foto_url=converter_link_drive(request.form.get("foto_url", "").strip())[:500],
            custo_base=custo, colecao_id=colecao_id, markup=markup,
            preco_venda_manual=manual,
        )
        foto_url, foto_path = salvar_foto_produto(request.files.get("foto"))
        if foto_path:
            saved_files.append(foto_path)
            novo.foto_url = foto_url
        db.session.add(novo)
        db.session.flush()
        anexo = request.files.get("anexo")
        if anexo and anexo.filename:
            content, original_name, extension = ler_anexo(anexo)
            saved_files.append(salvar_anexo_conteudo(
                novo, content, original_name, extension, anexo.mimetype
            ))
        db.session.commit()
        flash(f"Joia cadastrada. Preço ideal: R$ {novo.preco_ideal:.2f}.", "success")
    except ValueError as exc:
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        flash(str(exc), "error")
    except SQLAlchemyError:
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        logger.exception("Falha ao cadastrar produto")
        flash("Não foi possível cadastrar a joia. Tente novamente.", "error")
    except OSError:
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        logger.exception("Falha ao armazenar anexo do produto")
        flash("Não foi possível salvar o anexo. Tente novamente.", "error")
    return redirect(url_for("index"))


@app.route("/produto/editar/<int:id>", methods=["POST"])
@login_required
def editar_produto(id):
    produto = db.get_or_404(Produto, id)
    saved_files = []
    try:
        produto.nome = validar_texto(
            request.form.get("nome"), "nome da joia", 100
        )
        produto.categoria = validar_texto(
            request.form.get("categoria"), "categoria", 50
        )
        produto.custo_base = parse_moeda(
            request.form.get("custo_base"), "custo"
        )
        markup = float(request.form.get("markup") or 2.5)
        if not 0.01 <= markup <= 100:
            raise ValueError("O markup deve estar entre 0,01 e 100.")
        produto.markup = markup
        manual_value = request.form.get("preco_venda_manual", "").strip()
        produto.preco_venda_manual = (
            parse_moeda(manual_value, "preço de venda") if manual_value else None
        )
        foto_url, foto_path = salvar_foto_produto(request.files.get("foto"))
        if foto_path:
            saved_files.append(foto_path)
            produto.foto_url = foto_url
        elif request.form.get("remover_foto") == "1":
            produto.foto_url = None
        db.session.commit()
        flash("Joia atualizada.", "success")
    except ValueError as exc:
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        flash(str(exc), "error")
    except SQLAlchemyError:
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        logger.exception("Falha ao atualizar produto")
        flash("Não foi possível atualizar a joia. Tente novamente.", "error")
    except OSError:
        db.session.rollback()
        for path in saved_files:
            path.unlink(missing_ok=True)
        logger.exception("Falha ao armazenar a foto da joia")
        flash("Não foi possível salvar a foto. Tente novamente.", "error")
    return redirect(url_for("index"))


@app.route("/produto/vender/<int:id>", methods=["POST"])
@login_required
def vender_produto(id):
    produto = db.get_or_404(Produto, id)
    try:
        if produto.vendido == "SIM":
            raise ValueError("Esta joia já foi vendida.")
        cliente = validar_texto(request.form.get("cliente_nome"), "nome da cliente", 100)
        desconto = float(request.form.get("desconto_manual") or 0)
        parcelas = int(request.form.get("num_parcelas") or 1)
        if not 0 <= desconto <= 100:
            raise ValueError("O desconto deve estar entre 0% e 100%.")
        if not 1 <= parcelas <= 24:
            raise ValueError("O número de parcelas deve estar entre 1 e 24.")
        produto.cliente_nome = cliente
        produto.cliente_telefone = request.form.get("cliente_telefone", "").strip()[:20]
        forma_pagamento = request.form.get("forma_pagamento", "Pix")
        if forma_pagamento not in {"Pix", "Cartão", "Dinheiro"}:
            raise ValueError("Selecione uma forma de pagamento válida.")
        produto.forma_pagamento = forma_pagamento
        produto.num_parcelas = parcelas
        origem = request.form.get("origem_venda", "").strip()
        if origem and origem not in {
            "Instagram", "WhatsApp", "Loja física", "Indicação", "Outro"
        }:
            raise ValueError("Selecione uma origem de venda válida.")
        produto.origem_venda = origem[:50]
        produto.tag_campanha = request.form.get("tag_campanha", "").strip()[:50]
        produto.desconto_aplicado = desconto
        produto.preco_venda = produto.preco_ideal * (1 - desconto / 100)
        produto.vendido = "SIM"
        produto.data_venda = datetime.now()
        brindes_sem_saldo = []
        itens_brinde = ItemBrinde.query.filter(
            ItemBrinde.ativo.is_(True),
            ItemBrinde.consumo_por_venda > 0,
        ).with_for_update().all()
        for item in itens_brinde:
            if item.quantidade_estoque + 1e-9 < item.consumo_por_venda:
                brindes_sem_saldo.append(item.nome)
                continue
            item.quantidade_estoque = max(
                0, item.quantidade_estoque - item.consumo_por_venda
            )
            db.session.add(ConsumoBrinde(
                produto_id=produto.id,
                item_id=item.id,
                quantidade=item.consumo_por_venda,
                custo_unitario=item.custo_unitario,
            ))
        db.session.commit()
        flash("Venda registrada.", "success")
        if brindes_sem_saldo:
            flash(
                "Estoque insuficiente; não foram descontados: "
                + ", ".join(brindes_sem_saldo) + ".",
                "warning",
            )
    except (ValueError, TypeError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Falha ao registrar venda")
        flash("Não foi possível registrar a venda. Tente novamente.", "error")
    return redirect(url_for("index"))


@app.route("/despesa/add", methods=["POST"])
@login_required
def add_despesa():
    try:
        descricao = validar_texto(request.form.get("descricao"), "descrição", 100)
        valor = parse_moeda(request.form.get("valor"), "valor")
        categoria = validar_texto(
            request.form.get("categoria", "Operacional"), "categoria", 50
        )
        data_raw = request.form.get("data", "").strip()
        data = datetime.strptime(data_raw, "%Y-%m-%d") if data_raw else datetime.now()
        controla_estoque = request.form.get("controla_estoque") == "1"
        item_brinde = None
        quantidade_compra = 0.0
        consumo_por_venda = 0.0
        unidade = ""
        if controla_estoque:
            quantidade_compra = float(request.form.get("quantidade_compra") or 0)
            unidade = request.form.get("unidade_item", "")
            if not 0 < quantidade_compra <= 1_000_000_000:
                raise ValueError("A quantidade comprada deve ser maior que zero.")
            if unidade not in {"ml", "g", "unidade"}:
                raise ValueError("Selecione uma unidade válida para o item.")
            item_brinde = ItemBrinde.query.filter(
                func.lower(ItemBrinde.nome) == descricao.lower()
            ).with_for_update().first()
            if item_brinde and item_brinde.unidade != unidade:
                raise ValueError(
                    f"{descricao} já está cadastrada em {item_brinde.unidade}; "
                    "use a mesma unidade para novas compras."
                )
            if not item_brinde:
                item_brinde = ItemBrinde(
                    nome=descricao, unidade=unidade,
                    quantidade_estoque=0, consumo_por_venda=0,
                    custo_unitario=0,
                )
                db.session.add(item_brinde)
                db.session.flush()
            consumo_raw = request.form.get("consumo_por_venda", "").strip()
            consumo_por_venda = (
                float(consumo_raw)
                if consumo_raw else item_brinde.consumo_por_venda
            )
            if not 0 <= consumo_por_venda <= 1_000_000_000:
                raise ValueError("A quantidade por venda deve ser zero ou maior.")
            estoque_anterior = item_brinde.quantidade_estoque
            valor_estoque = estoque_anterior * item_brinde.custo_unitario
            item_brinde.quantidade_estoque += quantidade_compra
            item_brinde.custo_unitario = (
                (valor_estoque + valor) / item_brinde.quantidade_estoque
            )
            item_brinde.consumo_por_venda = consumo_por_venda
            item_brinde.ativo = consumo_por_venda > 0
        db.session.add(DespesaFixa(
            descricao=descricao, valor=valor, categoria=categoria, data=data
        ))
        db.session.commit()
        flash(
            "Despesa e estoque de brinde registrados."
            if controla_estoque else "Despesa registrada.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Falha ao registrar despesa")
        flash("Não foi possível registrar a despesa. Tente novamente.", "error")
    return redirect(url_for("index"))


@app.route("/colecao/add", methods=["POST"])
@login_required
def add_colecao():
    try:
        nome = validar_texto(request.form.get("nome"), "nome do lote", 100)
        quantidade = int(request.form.get("qtd_pecas") or 0)
        frete = parse_moeda(request.form.get("valor_frete") or "0", "frete")
        mimos = parse_moeda(request.form.get("valor_mimos") or "0", "mimos")
        if not 1 <= quantidade <= 100_000:
            raise ValueError("A quantidade deve estar entre 1 e 100.000 peças.")
        db.session.add(Colecao(
            nome=nome, qtd_pecas=quantidade, valor_frete=frete, valor_mimos=mimos
        ))
        db.session.commit()
        flash("Lote criado.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Falha ao criar lote")
        flash("Não foi possível criar o lote. Tente novamente.", "error")
    return redirect(url_for("index"))


@app.route("/relatorio")
@login_required
def relatorio():
    today = datetime.now().date()
    start_default = today.replace(day=1)
    try:
        start = datetime.strptime(request.args.get("inicio", start_default.isoformat()), "%Y-%m-%d").date()
        end = datetime.strptime(request.args.get("fim", today.isoformat()), "%Y-%m-%d").date()
        if start > end or (end - start).days > 3660:
            raise ValueError
    except ValueError:
        abort(400, description="Informe um período válido de até 10 anos.")
    start_at = datetime.combine(start, time.min)
    end_exclusive = datetime.combine(end + timedelta(days=1), time.min)
    vendas = Produto.query.filter(
        Produto.vendido == "SIM",
        Produto.data_venda >= start_at,
        Produto.data_venda < end_exclusive,
    ).options(joinedload(Produto.colecao)).order_by(Produto.data_venda.desc()).all()
    despesas = DespesaFixa.query.filter(
        DespesaFixa.data >= start_at, DespesaFixa.data < end_exclusive
    ).all()
    faturamento = sum(v.preco_venda or 0 for v in vendas)
    lucro_total = sum(v.lucro_liquido for v in vendas)
    total_despesas = sum(d.valor for d in despesas)
    total_mkt = sum(d.valor for d in despesas if d.categoria.lower() == "marketing")
    origens = {}
    for venda in vendas:
        origem = venda.origem_venda or "Direto/Outros"
        origens[origem] = origens.get(origem, 0) + (venda.preco_venda or 0)
    return render_template(
        "relatorio.html", vendas=vendas, faturamento=faturamento,
        ticket_medio=faturamento / len(vendas) if vendas else 0,
        lucro_final=lucro_total - total_despesas, lucro_total=lucro_total,
        total_despesas=total_despesas, total_mkt=total_mkt, origens=origens,
        loja=LOJA_CONFIG, inicio=start.isoformat(), fim=end.isoformat(),
    )


@app.route("/vendas")
@login_required
def vendas_realizadas():
    vendas = Produto.query.filter_by(vendido="SIM").options(
        joinedload(Produto.colecao)
    ).order_by(Produto.data_venda.desc(), Produto.id.desc()).all()
    total_vendido = sum(venda.preco_venda or 0 for venda in vendas)
    return render_template(
        "vendas.html", vendas=vendas, total_vendido=total_vendido,
    )


@app.route("/produto/excluir-venda/<int:id>", methods=["POST"])
@login_required
def excluir_venda(id):
    produto = db.get_or_404(Produto, id)
    try:
        if produto.vendido != "SIM":
            raise ValueError("Esta joia não possui uma venda para excluir.")

        consumos = ConsumoBrinde.query.filter_by(produto_id=produto.id).all()
        for consumo in consumos:
            item = db.session.get(ItemBrinde, consumo.item_id)
            if item:
                item.quantidade_estoque += consumo.quantidade
            db.session.delete(consumo)

        produto.vendido = "NÃO"
        produto.cliente_nome = None
        produto.cliente_telefone = None
        produto.forma_pagamento = None
        produto.num_parcelas = 1
        produto.origem_venda = None
        produto.tag_campanha = None
        produto.data_venda = None
        produto.desconto_aplicado = 0
        produto.preco_venda = None
        db.session.commit()
        flash(
            "Venda excluída; a joia voltou ao estoque e os brindes foram repostos.",
            "success",
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Falha ao excluir venda")
        flash("Não foi possível excluir a venda. Tente novamente.", "error")
    return redirect(url_for("vendas_realizadas"))


@app.route("/catalogo")
def catalogo():
    estoque = Produto.query.join(Colecao).filter(
        Produto.vendido == "NÃO", Colecao.arquivada.is_(False)
    ).order_by(Produto.nome).all()
    return render_template(
        "Catalogo.html", estoque=estoque, loja=LOJA_CONFIG,
        data=datetime.now().strftime("%d/%m/%Y"),
    )


@app.route("/health")
def health():
    db.session.execute(text("SELECT 1"))
    return jsonify({"status": "ok"}), 200


@app.route("/anexos/<int:anexo_id>")
@login_required
def download_anexo(anexo_id):
    anexo = db.get_or_404(AnexoProduto, anexo_id)
    path = (UPLOAD_DIR / anexo.nome_armazenado).resolve()
    if path.parent != UPLOAD_DIR or not path.is_file():
        abort(404)
    return send_file(path, mimetype=anexo.tipo, as_attachment=True,
                     download_name=anexo.nome_original)


@app.route("/documentos-pedido/<int:documento_id>")
@login_required
def download_documento_pedido(documento_id):
    documento = db.get_or_404(DocumentoPedido, documento_id)
    path = (UPLOAD_DIR / documento.nome_armazenado).resolve()
    if path.parent != UPLOAD_DIR or not path.is_file():
        abort(404)
    return send_file(path, mimetype=documento.tipo, as_attachment=True,
                     download_name=documento.nome_original)


@app.route("/fotos/<string:filename>")
def foto_produto(filename):
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|jpeg|png|webp)", filename):
        abort(404)
    foto_dir = (UPLOAD_DIR / "fotos").resolve()
    path = (foto_dir / filename).resolve()
    if path.parent != foto_dir or not path.is_file():
        abort(404)
    response = send_file(path, mimetype=mimetypes.guess_type(filename)[0])
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.errorhandler(413)
def request_entity_too_large(_error):
    if request.path == "/produto/extrair-anexo":
        return jsonify({"error": "O arquivo excede o limite permitido de 10 MB."}), 413
    flash("O arquivo excede o limite permitido de 10 MB.", "error")
    return redirect(url_for("index"))


with app.app_context():
    db.create_all()
    migrar_esquema_sqlite(db.engine)


if __name__ == "__main__":
    app.run(debug=environment != "production")
