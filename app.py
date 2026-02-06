from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from sqlalchemy import func
import os

app = Flask(__name__)

# Configuração do Banco de Dados
base_dir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(base_dir, 'autentke.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Modelo do Banco de Dados conforme a Arquitetura definida
class Produto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    custo_base = db.Column(db.Float, nullable=False)
    custo_extra = db.Column(db.Float, default=0.0) # Rateio de frete/mimos
    markup = db.Column(db.Float, default=2.5) 
    preco_venda = db.Column(db.Float)
    preco_promo = db.Column(db.Float)
    vendido = db.Column(db.String(3), default='NÃO')
    cliente_nome = db.Column(db.String(100))
    cliente_id = db.Column(db.String(50))
    data_venda = db.Column(db.String(20))
    forma_pagamento = db.Column(db.String(50))
    tipo_venda = db.Column(db.String(20))
    arquivado = db.Column(db.Boolean, default=False)

with app.app_context():
    db.create_all()

@app.route('/')
def index():
    # Itens ativos para a gestão diária
    produtos = Produto.query.filter_by(arquivado=False).all()
    vendas = Produto.query.filter_by(vendido='SIM').all()
    
    # Cálculos Financeiros em tempo real
    fat_total = sum(v.preco_venda for v in vendas)
    lucro_total = sum(v.preco_venda - (v.custo_base + v.custo_extra) for v in vendas)
    
    # CRM: Ranking e Ticket Médio
    ranking = db.session.query(Produto.cliente_nome, func.count(Produto.id))\
        .filter(Produto.vendido == 'SIM')\
        .group_by(Produto.cliente_nome)\
        .order_by(func.count(Produto.id).desc()).limit(5).all()
    
    ticket_medio = fat_total / len(vendas) if vendas else 0
    
    return render_template('index.html', produtos=produtos, fat=fat_total, 
                           lucro=lucro_total, ranking=ranking, ticket=ticket_medio)

@app.route('/add', methods=['POST'])
def add():
    nome = request.form.get('nome').upper()
    custo = float(request.form.get('custo'))
    qntd = int(request.form.get('qntd'))
    extras_total = float(request.form.get('extras', 0))
    
    # Automação de Rateio e Preços
    extras_un = extras_total / qntd if qntd > 0 else 0
    venda_ideal = round((custo + extras_un) * 2.5, 2)
    venda_promo = round(venda_ideal * 0.9, 2)
    
    for _ in range(qntd):
        novo = Produto(nome=nome, custo_base=custo, custo_extra=extras_un, 
                       preco_venda=venda_ideal, preco_promo=venda_promo)
        db.session.add(novo)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/edit/<int:id>', methods=['POST'])
def edit(id):
    p = Produto.query.get(id)
    if p:
        p.nome = request.form.get('nome').upper()
        p.custo_base = float(request.form.get('custo'))
        p.markup = float(request.form.get('markup', 2.5))
        # Recalcula com base na modificação manual
        p.preco_venda = float(request.form.get('valor_venda'))
        p.preco_promo = round(p.preco_venda * 0.9, 2)
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/vender/<int:id>', methods=['POST'])
def vender(id):
    p = Produto.query.get(id)
    if p:
        p.cliente_nome = request.form.get('cliente_nome').title()
        p.cliente_id = request.form.get('cliente_id')
        p.forma_pagamento = request.form.get('forma_pagamento')
        p.data_venda = datetime.now().strftime('%d/%m/%Y %H:%M')
        
        if request.form.get('usar_promo') == 'sim':
            p.preco_venda = p.preco_promo
            p.tipo_venda = 'PROMOÇÃO'
        else:
            p.tipo_venda = 'NORMAL'
            
        p.vendido = 'SIM'
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/delete/<int:id>', methods=['POST'])
def delete(id):
    p = Produto.query.get(id)
    if p:
        db.session.delete(p)
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/arquivar', methods=['POST'])
def arquivar():
    Produto.query.filter_by(vendido='SIM', arquivado=False).update({Produto.arquivado: True})
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/relatorio')
def relatorio():
    vendas_hist = Produto.query.filter_by(vendido='SIM').order_by(Produto.data_venda.desc()).all()
    # Soma do lucro acumulado para o relatório
    lucro_acumulado = sum(v.preco_venda - (v.custo_base + v.custo_extra) for v in vendas_hist)
    return render_template('relatorio.html', vendas=vendas_hist, fat_total_lucro=lucro_acumulado)

if __name__ == '__main__':
    app.run(debug=True)