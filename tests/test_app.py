import os
from datetime import datetime
from io import BytesIO
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash

os.environ["APP_ENV"] = "testing"
os.environ["ADMIN_USERNAME"] = "admin-test"
os.environ["ADMIN_PASSWORD_HASH"] = generate_password_hash("test-password")
os.environ["SECRET_KEY"] = "test-key-not-for-production"
os.environ["DATABASE_URL"] = "sqlite://"

import app as application


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upload_dir = tempfile.TemporaryDirectory()
        application.UPLOAD_DIR = application.Path(cls.upload_dir.name).resolve()
        application.app.config.update(
            TESTING=True, WTF_CSRF_ENABLED=False, SESSION_COOKIE_SECURE=False
        )

    @classmethod
    def tearDownClass(cls):
        with application.app.app_context():
            application.db.session.remove()
            application.db.engine.dispose()
        cls.upload_dir.cleanup()

    def setUp(self):
        with application.app.app_context():
            application.db.drop_all()
            application.db.create_all()
        self.client = application.app.test_client()

    def login(self):
        return self.client.post(
            "/login",
            data={"username": "admin-test", "password": "test-password"},
            follow_redirects=False,
        )

    def create_product(self, archived=False):
        with application.app.app_context():
            collection = application.Colecao(
                nome="Lote de teste", qtd_pecas=2, valor_frete=20,
                arquivada=archived,
            )
            application.db.session.add(collection)
            application.db.session.flush()
            product = application.Produto(
                nome="Anel teste", custo_base=100, markup=2,
                colecao_id=collection.id,
            )
            application.db.session.add(product)
            application.db.session.commit()
            return product.id

    def test_dashboard_and_reports_require_login_but_catalog_is_public(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/relatorio").status_code, 302)
        self.assertEqual(self.client.get("/catalogo").status_code, 200)
        self.assertEqual(self.client.get("/health").get_json(), {"status": "ok"})
        self.login()
        self.assertEqual(self.client.get("/relatorio").status_code, 200)

    def test_login_accepts_configured_admin_and_rejects_bad_password(self):
        bad = self.client.post(
            "/login", data={"username": "admin-test", "password": "wrong"}
        )
        self.assertEqual(bad.status_code, 200)
        self.assertIn(b"Usu\xc3\xa1rio ou senha inv\xc3\xa1lidos", bad.data)
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_csrf_protects_state_changing_requests(self):
        self.login()
        application.app.config["WTF_CSRF_ENABLED"] = True
        try:
            response = self.client.post("/despesa/add", data={
                "descricao": "Frete", "valor": "10", "categoria": "Operacional",
            })
            self.assertEqual(response.status_code, 400)
        finally:
            application.app.config["WTF_CSRF_ENABLED"] = False

    def test_product_and_expense_inputs_are_validated(self):
        self.login()
        response = self.client.post("/colecao/add", data={
            "nome": "Lote inválido", "qtd_pecas": "0",
        }, follow_redirects=True)
        self.assertIn("quantidade deve estar", response.get_data(as_text=True).lower())
        response = self.client.post("/despesa/add", data={
            "descricao": "Anúncio", "valor": "-2", "categoria": "Marketing",
        }, follow_redirects=True)
        self.assertIn("maior ou igual a zero", response.get_data(as_text=True).lower())

    def test_sale_discount_is_validated_and_metrics_match_profit(self):
        product_id = self.create_product()
        self.login()
        bad_sale = self.client.post(
            f"/produto/vender/{product_id}",
            data={"cliente_nome": "Cliente", "desconto_manual": "101"},
            follow_redirects=True,
        )
        self.assertIn("desconto deve estar entre", bad_sale.get_data(as_text=True).lower())
        with application.app.app_context():
            product = application.db.get_or_404(application.Produto, product_id)
            self.assertEqual(product.vendido, "NÃO")
        sale = self.client.post(
            f"/produto/vender/{product_id}",
            data={
                "cliente_nome": "Cliente", "desconto_manual": "0",
                "num_parcelas": "1", "origem_venda": "Instagram",
            },
        )
        self.assertEqual(sale.status_code, 302)
        self.client.post("/despesa/add", data={
            "descricao": "Anúncio", "valor": "10", "categoria": "Marketing",
        })
        metrics = self.client.get("/api/metricas/total").get_json()
        self.assertEqual(metrics["total_vendas"], 1)
        self.assertAlmostEqual(metrics["faturamento"], 220)
        self.assertAlmostEqual(metrics["lucro_liquido"], 100)

    def test_brinde_purchase_tracks_quantity_cost_and_deducts_per_sale(self):
        product_id = self.create_product()
        self.login()
        cleaner = self.client.post(
            "/despesa/add",
            data={
                "descricao": "Limpa-pratas",
                "valor": "50",
                "categoria": "Brinde",
                "controla_estoque": "1",
                "quantidade_compra": "300",
                "unidade_item": "ml",
                "consumo_por_venda": "5",
            },
            follow_redirects=True,
        )
        self.assertIn("estoque de brinde registrados", cleaner.get_data(as_text=True))
        pots = self.client.post(
            "/despesa/add",
            data={
                "descricao": "Pote",
                "valor": "25",
                "categoria": "Embalagem",
                "controla_estoque": "1",
                "quantidade_compra": "50",
                "unidade_item": "unidade",
                "consumo_por_venda": "1",
            },
        )
        self.assertEqual(pots.status_code, 302)
        with application.app.app_context():
            cleaner_item = application.ItemBrinde.query.filter_by(
                nome="Limpa-pratas"
            ).one()
            pot_item = application.ItemBrinde.query.filter_by(nome="Pote").one()
            self.assertEqual(cleaner_item.quantidade_estoque, 300)
            self.assertAlmostEqual(cleaner_item.custo_unitario, 50 / 300)
            self.assertEqual(cleaner_item.consumo_por_venda, 5)
            self.assertEqual(pot_item.quantidade_estoque, 50)

        sale = self.client.post(
            f"/produto/vender/{product_id}",
            data={
                "cliente_nome": "Cliente",
                "desconto_manual": "0",
                "num_parcelas": "1",
                "origem_venda": "Instagram",
            },
            follow_redirects=True,
        )
        self.assertIn("Venda registrada.", sale.get_data(as_text=True))
        with application.app.app_context():
            cleaner_item = application.ItemBrinde.query.filter_by(
                nome="Limpa-pratas"
            ).one()
            pot_item = application.ItemBrinde.query.filter_by(nome="Pote").one()
            self.assertEqual(cleaner_item.quantidade_estoque, 295)
            self.assertEqual(pot_item.quantidade_estoque, 49)
            consumed = application.ConsumoBrinde.query.all()
            self.assertEqual(len(consumed), 2)
            self.assertEqual(
                {row.quantidade for row in consumed}, {5, 1}
            )

    def test_low_brinde_stock_warns_and_skips_only_that_item(self):
        product_id = self.create_product()
        self.login()
        for name, quantity, unit, consumption in (
            ("Limpa-pratas", 4, "ml", 5),
            ("Pote", 2, "unidade", 1),
        ):
            self.client.post(
                "/despesa/add",
                data={
                    "descricao": name, "valor": "10", "categoria": "Brinde",
                    "controla_estoque": "1",
                    "quantidade_compra": str(quantity),
                    "unidade_item": unit,
                    "consumo_por_venda": str(consumption),
                },
            )
        response = self.client.post(
            f"/produto/vender/{product_id}",
            data={
                "cliente_nome": "Cliente", "desconto_manual": "0",
                "num_parcelas": "1", "origem_venda": "Instagram",
            },
            follow_redirects=True,
        )
        self.assertIn("não foram descontados: Limpa-pratas", response.get_data(as_text=True))
        with application.app.app_context():
            cleaner_item = application.ItemBrinde.query.filter_by(
                nome="Limpa-pratas"
            ).one()
            pot_item = application.ItemBrinde.query.filter_by(nome="Pote").one()
            product = application.db.get_or_404(application.Produto, product_id)
            self.assertEqual(product.vendido, "SIM")
            self.assertEqual(cleaner_item.quantidade_estoque, 4)
            self.assertEqual(pot_item.quantidade_estoque, 1)
            self.assertEqual(application.ConsumoBrinde.query.count(), 1)

    def test_monthly_metrics_exclude_archived_collection_sales(self):
        product_id = self.create_product(archived=True)
        with application.app.app_context():
            product = application.db.get_or_404(application.Produto, product_id)
            product.vendido = "SIM"
            product.preco_venda = 220
            product.data_venda = datetime.now()
            application.db.session.commit()
            monthly = application.calcular_metricas(apenas_atual=True)
            historical = application.calcular_metricas(apenas_atual=False)
        self.assertEqual(monthly["total_vendas"], 0)
        self.assertEqual(historical["total_vendas"], 1)

    def test_sqlite_schema_migration_preserves_existing_collections(self):
        engine = create_engine("sqlite://")
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE colecao "
                    "(id INTEGER PRIMARY KEY, nome VARCHAR(100) NOT NULL)"
                ))
                connection.execute(text(
                    "INSERT INTO colecao (id, nome) VALUES (1, 'Lote existente')"
                ))
            application.migrar_esquema_sqlite(engine)
            with engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT nome, arquivada FROM colecao WHERE id=1"
                )).one()
            self.assertEqual(row, ("Lote existente", 0))
            application.migrar_esquema_sqlite(engine)
        finally:
            engine.dispose()

    def test_ocr_suggestions_parse_portuguese_name_and_currency(self):
        result = application.sugerir_dados(
            "Nome: Anel Dourado\nCusto: R$ 1.234,56\n"
        )
        self.assertEqual(result["nome"], "Anel Dourado")
        self.assertEqual(result["custo_base"], 1234.56)
        self.assertEqual(
            application.sugerir_dados("Colar de prata\nPreço unitário 89,90")[
                "custo_base"
            ],
            89.9,
        )
        self.assertIsNone(
            application.sugerir_dados("CY PRATAS - JOIAS EM PRATA 925")[
                "custo_base"
            ]
        )

    def test_invoice_table_parser_extracts_rows_and_unit_prices(self):
        words = [
            (320, 300, "ANEL", 90), (360, 300, "PRATA", 90),
            (810, 300, "R$", 90), (840, 300, "38,57", 90),
            (320, 400, "BRINCO", 90), (360, 400, "CRISTAL", 90),
            (810, 400, "R$", 90), (840, 400, "18,43", 90),
            (320, 500, "PULSEIRA", 90), (810, 500, "R$", 90),
            (840, 500, "41,33", 90),
        ]
        rows = application.parse_tabela_ocr(words, 1000, 1000)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["nome"], "ANEL PRATA")
        self.assertEqual(rows[0]["custo_base"], 38.57)
        self.assertEqual(rows[0]["categoria"], "Anéis")
        self.assertEqual(rows[1]["custo_base"], 18.43)
        self.assertEqual(rows[1]["categoria"], "Brincos")

    def test_attachment_extraction_endpoint_returns_reviewable_suggestions(self):
        self.login()
        image_buffer = BytesIO()
        Image.new("RGB", (4, 4), "white").save(image_buffer, format="PNG")
        with patch.object(
            application.pytesseract, "image_to_string",
            return_value="Nome: Colar delicado\nCusto: R$ 85,90",
        ):
            response = self.client.post(
                "/produto/extrair-anexo",
                data={"anexo": (BytesIO(image_buffer.getvalue()), "nota.png")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "nome": "Colar delicado", "custo_base": 85.9,
            "nome_encontrado": True, "custo_encontrado": True,
        })

    def test_invoice_endpoint_returns_multiple_products(self):
        self.login()
        rows = [
            {
                "nome": "Anel de prata", "custo_base": 38.57,
                "categoria": "Anéis", "incluido": True, "revisar": False,
            },
            {
                "nome": "Brinco cristal", "custo_base": 18.43,
                "categoria": "Brincos", "incluido": True, "revisar": False,
            },
        ]
        with patch.object(application, "extrair_itens_anexo", return_value=rows):
            response = self.client.post(
                "/produto/extrair-anexo",
                data={"anexo": (BytesIO(b"%PDF-1.4"), "pedido.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"], rows)

    def test_batch_product_registration_saves_selected_rows_and_invoice(self):
        self.login()
        with application.app.app_context():
            collection = application.Colecao(nome="Lote de pedido", qtd_pecas=2)
            application.db.session.add(collection)
            application.db.session.commit()
            collection_id = collection.id
        image_buffer = BytesIO()
        Image.new("RGB", (4, 4), "white").save(image_buffer, format="PNG")
        response = self.client.post(
            "/produto/add-lote",
            data={
                "colecao_id": str(collection_id),
                "markup": "2.5",
                "produto_nome[]": ["Anel de prata", "Brinco cristal", "Embalagem"],
                "produto_custo[]": ["38.57", "18.43", "0.01"],
                "produto_categoria[]": ["Anéis", "Brincos", "Outros"],
                "produto_manual[]": ["", "", ""],
                "produto_incluir[]": ["0", "1"],
                "produto_foto[]": [
                    (BytesIO(image_buffer.getvalue()), "anel.png"),
                    (BytesIO(image_buffer.getvalue()), "brinco.png"),
                    (BytesIO(image_buffer.getvalue()), "embalagem.png"),
                ],
                "anexo": (BytesIO(image_buffer.getvalue()), "pedido.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("2 joia(s) cadastrada(s)", response.get_data(as_text=True))
        with application.app.app_context():
            products = application.Produto.query.order_by(application.Produto.id).all()
            self.assertEqual(len(products), 2)
            self.assertEqual([product.nome for product in products],
                             ["Anel de prata", "Brinco cristal"])
            self.assertTrue(all(product.documento_pedido is not None for product in products))
            self.assertTrue(all(product.foto_url.startswith("/fotos/") for product in products))
            self.assertEqual(
                products[0].documento_pedido.id, products[1].documento_pedido.id
            )
            stored_path = (
                application.UPLOAD_DIR
                / products[0].documento_pedido.nome_armazenado
            )
            self.assertTrue(stored_path.is_file())
            photo_url = products[0].foto_url
        photo = self.client.get(photo_url)
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.data, image_buffer.getvalue())
        photo.close()

    def test_product_document_is_saved_as_private_attachment(self):
        self.login()
        with application.app.app_context():
            collection = application.Colecao(nome="Lote foto", qtd_pecas=1)
            application.db.session.add(collection)
            application.db.session.commit()
            collection_id = collection.id
        image_buffer = BytesIO()
        Image.new("RGB", (4, 4), "white").save(image_buffer, format="PNG")
        response = self.client.post("/produto/add", data={
            "nome": "Brinco enviado", "custo_base": "40,50",
            "colecao_id": str(collection_id), "markup": "2.5",
            "anexo": (BytesIO(image_buffer.getvalue()), "nota.png"),
            "foto": (BytesIO(image_buffer.getvalue()), "brinco.png"),
        }, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("Preço ideal: R$ 101.25", response.get_data(as_text=True))
        with application.app.app_context():
            product = application.Produto.query.filter_by(nome="Brinco enviado").one()
            attachment_id = product.anexo.id
            stored_path = application.UPLOAD_DIR / product.anexo.nome_armazenado
            photo_url = product.foto_url
            self.assertTrue(stored_path.is_file())
            self.assertTrue(photo_url.startswith("/fotos/"))
        download = self.client.get(f"/anexos/{attachment_id}")
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.data, image_buffer.getvalue())
        download.close()
        image = self.client.get(photo_url)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.data, image_buffer.getvalue())
        image.close()
        self.assertEqual(self.client.get("/catalogo").status_code, 200)

    def test_product_can_be_edited_and_photo_removed(self):
        product_id = self.create_product()
        self.login()
        image_buffer = BytesIO()
        Image.new("RGB", (4, 4), "white").save(image_buffer, format="PNG")
        response = self.client.post(
            f"/produto/editar/{product_id}",
            data={
                "nome": "Anel atualizado", "categoria": "Anéis",
                "custo_base": "50", "markup": "3",
                "preco_venda_manual": "",
                "foto": (BytesIO(image_buffer.getvalue()), "anel.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("Joia atualizada.", response.get_data(as_text=True))
        with application.app.app_context():
            product = application.db.get_or_404(application.Produto, product_id)
            self.assertEqual(product.nome, "Anel atualizado")
            self.assertEqual(product.categoria, "Anéis")
            self.assertEqual(product.preco_ideal, 180)
            photo_url = product.foto_url
        photo = self.client.get(photo_url)
        self.assertEqual(photo.status_code, 200)
        photo.close()

        self.client.post(
            f"/produto/editar/{product_id}",
            data={
                "nome": "Anel atualizado", "categoria": "Anéis",
                "custo_base": "50", "markup": "3",
                "preco_venda_manual": "", "remover_foto": "1",
            },
            follow_redirects=True,
        )
        with application.app.app_context():
            product = application.db.get_or_404(application.Produto, product_id)
            self.assertIsNone(product.foto_url)

    def test_photo_upload_rejects_pdf_and_does_not_create_product(self):
        self.login()
        with application.app.app_context():
            collection = application.Colecao(nome="Lote foto inválida", qtd_pecas=1)
            application.db.session.add(collection)
            application.db.session.commit()
            collection_id = collection.id
        response = self.client.post(
            "/produto/add",
            data={
                "nome": "Joia sem foto", "categoria": "Outros",
                "custo_base": "20", "markup": "2",
                "colecao_id": str(collection_id),
                "foto": (BytesIO(b"%PDF-1.4"), "imagem.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn("formato JPG, PNG ou WEBP", response.get_data(as_text=True))
        with application.app.app_context():
            self.assertIsNone(
                application.Produto.query.filter_by(nome="Joia sem foto").first()
            )

    def test_attachment_download_is_private(self):
        product_id = self.create_product()
        with application.app.app_context():
            attachment = application.AnexoProduto(
                produto_id=product_id, nome_original="nota.pdf",
                nome_armazenado="missing.pdf", tipo="application/pdf",
            )
            application.db.session.add(attachment)
            application.db.session.commit()
            attachment_id = attachment.id
        self.assertEqual(self.client.get(f"/anexos/{attachment_id}").status_code, 302)
        self.login()
        self.assertEqual(self.client.get(f"/anexos/{attachment_id}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
