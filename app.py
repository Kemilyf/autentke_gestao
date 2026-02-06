from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'autentke_secret_key_2024'
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'autentke.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# --- CONFIGURAÇÃO DA LOJA ---
LOJA_CONFIG = {
    'nome': 'Autentke',
    'instagram': 'autentke.joias',
    'meta_padrao': 5000.0
}

# --- MODELOS DE DADOS ---

class Colecao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    qtd_pecas = db.Column(db.Integer, nullable=False)
    valor_frete = db.Column(db.Float, default=0.0)
    valor_mimos = db.Column(db.Float, default=0.0)
    produtos = db.relationship('Produto', backref='colecao', lazy=True)

    @property
    def rateio_unidade(self):
        if self.qtd_pecas and self.qtd_pecas > 0:
            return (self.valor_frete + self.valor_mimos) / self.qtd_pecas
        return 0.0

class Produto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    foto_url = db.Column(db.String(500))
    custo_base = db.Column(db.Float, nullable=False)
    markup = db.Column(db.Float, default=2.5)
    preco_venda = db.Column(db.Float)
    vendido = db.Column(db.String(3), default='NÃO')
    colecao_id = db.Column(db.Integer, db.ForeignKey('colecao.id'), nullable=False)
    
    # CRM & Marketing
    cliente_nome = db.Column(db.String(100))
    forma_pagamento = db.Column(db.String(50))
    origem_venda = db.Column(db.String(50)) # Novo: Instagram, WhatsApp, Tráfego, etc.
    tag_campanha = db.Column(db.String(50)) # Novo: Ex: #PROMO_VERAO
    data_venda = db.Column(db.DateTime)
    desconto_aplicado = db.Column(db.Float, default=0.0)

    @property
    def preco_ideal(self):
        rateio = self.colecao.rateio_unidade if self.colecao else 0.0
        return (self.custo_base + rateio) * self.markup

    @property
    def lucro_liquido(self):
        if self.vendido == 'SIM':
            rateio = self.colecao.rateio_unidade if self.colecao else 0.0
            return (self.preco_venda or 0.0) - (self.custo_base + rateio)
        return 0.0

class DespesaFixa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(100), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    categoria = db.Column(db.String(50), default='Operacional') # Operacional ou Marketing
    data = db.Column(db.DateTime, default=datetime.now)

class MetaMensal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    valor_meta = db.Column(db.Float, nullable=False)
    mes_ano = db.Column(db.String(7), unique=True)

# --- ROTAS ---

@app.route('/')
def index():
    mes_atual = datetime.now().strftime('%Y-%m')
    colecoes = Colecao.query.all()
    estoque_ativo = Produto.query.filter_by(vendido='NÃO').all()
    vendas = Produto.query.filter_by(vendido='SIM').order_by(Produto.data_venda.desc()).all()
    despesas = DespesaFixa.query.all()
    
    faturamento = sum(p.preco_venda for p in vendas if p.preco_venda) if vendas else 0
    lucro_bruto = sum(p.lucro_liquido for p in vendas) if vendas else 0
    total_despesas = sum(d.valor for d in despesas)
    lucro_liquido_final = lucro_bruto - total_despesas
    
    # Métricas de Marketing
    vendas_insta = len([p for p in vendas if p.origem_venda == 'Instagram'])
    vendas_whats = len([p for p in vendas if p.origem_venda == 'WhatsApp'])
    vendas_trafego = len([p for p in vendas if p.origem_venda == 'Tráfego Pago'])

    meta_obj = MetaMensal.query.filter_by(mes_ano=mes_atual).first()
    meta_valor = meta_obj.valor_meta if meta_obj else LOJA_CONFIG['meta_padrao']
    progresso_meta = (faturamento / meta_valor * 100) if meta_valor > 0 else 0

    ticket_medio = faturamento / len(vendas) if vendas else 0
    return render_template('index.html', 
                           colecoes=colecoes, estoque=estoque_ativo, vendas=vendas,
                           faturamento=faturamento, lucro_real=lucro_bruto, 
                           lucro_final=lucro_liquido_final, ticket_medio=ticket_medio,
                           despesas=despesas, total_despesas=total_despesas,
                           meta_valor=meta_valor, progresso_meta=progresso_meta,
                           vendas_insta=vendas_insta, vendas_whats=vendas_whats, vendas_trafego=vendas_trafego,
                           loja=LOJA_CONFIG)

@app.route('/colecao/liquidar/<int:id>', methods=['POST'])
def liquidar_colecao(id):
    try:
        novo_markup = float(request.form.get('novo_markup'))
        produtos = Produto.query.filter_by(colecao_id=id, vendido='NÃO').all()
        for p in produtos:
            p.markup = novo_markup
            p.preco_venda = p.preco_ideal
        db.session.commit()
        flash(f'Lote atualizado para markup {novo_markup}!')
    except Exception as e:
        flash(f'Erro: {str(e)}')
    return redirect(url_for('index'))

@app.route('/produto/vender/<int:id>', methods=['POST'])
def vender_produto(id):
    p = Produto.query.get_or_404(id)
    p.cliente_nome = request.form.get('cliente_nome')
    p.forma_pagamento = request.form.get('forma_pagamento')
    p.origem_venda = request.form.get('origem_venda')
    p.tag_campanha = request.form.get('tag_campanha')
    desc = float(request.form.get('desconto_manual') or 0)
    p.desconto_aplicado = desc
    p.preco_venda = p.preco_ideal * (1 - (desc/100))
    p.vendido = 'SIM'
    p.data_venda = datetime.now()
    db.session.commit()
    flash('Venda registrada!')
    return redirect(url_for('index'))

@app.route('/produto/add', methods=['POST'])
def add_produto():
    try:
        nome = request.form.get('nome')
        foto = request.form.get('foto_url')
        custo = float(request.form.get('custo_base') or 0)
        colecao_id = int(request.form.get('colecao_id'))
        markup = float(request.form.get('markup') or 2.5)
        colecao = Colecao.query.get(colecao_id)
        rateio = colecao.rateio_unidade if colecao else 0
        preco_inicial = (custo + rateio) * markup
        novo = Produto(nome=nome, foto_url=foto, custo_base=custo, colecao_id=colecao_id, markup=markup, preco_venda=preco_inicial)
        db.session.add(novo)
        db.session.commit()
        flash('Joia adicionada!')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro: {str(e)}')
    return redirect(url_for('index'))

@app.route('/despesa/add', methods=['POST'])
def add_despesa():
    desc = request.form.get('descricao')
    valor = float(request.form.get('valor') or 0)
    cat = request.form.get('categoria')
    nova = DespesaFixa(descricao=desc, valor=valor, categoria=cat)
    db.session.add(nova)
    db.session.commit()
    flash('Despesa registrada!')
    return redirect(url_for('index'))

@app.route('/despesa/excluir/<int:id>')
def excluir_despesa(id):
    d = DespesaFixa.query.get(id)
    db.session.delete(d)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/meta/update', methods=['POST'])
def update_meta():
    mes_atual = datetime.now().strftime('%Y-%m')
    valor = float(request.form.get('valor_meta') or 0)
    meta = MetaMensal.query.filter_by(mes_ano=mes_atual).first()
    if meta:
        meta.valor_meta = valor
    else:
        meta = MetaMensal(valor_meta=valor, mes_ano=mes_atual)
        db.session.add(meta)
    db.session.commit()
    flash('Meta atualizada!')
    return redirect(url_for('index'))

@app.route('/produto/editar/<int:id>', methods=['POST'])
def editar_produto(id):
    p = Produto.query.get_or_404(id)
    p.nome = request.form.get('nome')
    p.foto_url = request.form.get('foto_url')
    p.markup = float(request.form.get('markup') or 2.5)
    p.cliente_nome = request.form.get('cliente_nome')
    p.forma_pagamento = request.form.get('forma_pagamento')
    p.origem_venda = request.form.get('origem_venda')
    p.tag_campanha = request.form.get('tag_campanha')
    desc = float(request.form.get('desconto_ajustado') or 0)
    p.desconto_aplicado = desc
    p.preco_venda = p.preco_ideal * (1 - (desc/100))
    db.session.commit()
    flash('Dados atualizados!')
    return redirect(url_for('index'))

@app.route('/produto/excluir/<int:id>')
def excluir_produto(id):
    p = Produto.query.get_or_404(id)
    db.session.delete(p)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/colecao/add', methods=['POST'])
def add_colecao():
    nome = request.form.get('nome')
    qtd = int(request.form.get('qtd_pecas') or 0)
    frete = float(request.form.get('valor_frete') or 0)
    mimos = float(request.form.get('valor_mimos') or 0)
    nova = Colecao(nome=nome, qtd_pecas=qtd, valor_frete=frete, valor_mimos=mimos)
    db.session.add(nova)
    db.session.commit()
    flash('Lote criado!')
    return redirect(url_for('index'))

@app.route('/relatorio')
def relatorio():
    vendas = Produto.query.filter_by(vendido='SIM').all()
    despesas = DespesaFixa.query.all()
    faturamento = sum(p.preco_venda for p in vendas if p.preco_venda) if vendas else 0
    lucro_bruto = sum(p.lucro_liquido for p in vendas) if vendas else 0
    total_despesas = sum(d.valor for d in despesas)
    total_mkt = sum(d.valor for d in despesas if d.categoria == 'Marketing')
    lucro_final = lucro_bruto - total_despesas
    
    # Agrupamento por origem
    origens = {}
    for v in vendas:
        origens[v.origem_venda] = origens.get(v.origem_venda, 0) + (v.preco_venda or 0)

    ticket_medio = faturamento / len(vendas) if vendas else 0
    return render_template('relatorio.html', 
                           vendas=vendas, faturamento=faturamento, lucro_total=lucro_bruto, 
                           lucro_final=lucro_final, total_despesas=total_despesas, total_mkt=total_mkt,
                           ticket_medio=ticket_medio,
                           origens=origens, data_geracao=datetime.now().strftime('%d/%m/%Y %H:%M'), loja=LOJA_CONFIG)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
