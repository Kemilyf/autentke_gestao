# Autentke Gestão

Aplicação local para gerenciar estoque, vendas, despesas, relatórios e catálogo.
Esta configuração não cria serviço de hospedagem nem recursos pagos: o app roda
no seu computador, e a URL fica disponível apenas enquanto ele estiver ligado.

## Configuração local

Requisitos: Python 3.10 ou superior. Para ler fotos e PDFs escaneados, instale
também Tesseract com idioma português e Poppler. No Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install tesseract-ocr tesseract-ocr-por poppler-utils
```

No terminal, a partir da pasta do projeto:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python setup_local.py
python app.py
```

O assistente `setup_local.py` pede um nome de usuário e senha, gera a chave da
sessão e guarda apenas o hash da senha em `.env`, com permissão restrita. Esse
arquivo é ignorado pelo Git; não o compartilhe nem o adicione ao repositório.
Se já houver um `.env`, o assistente pede confirmação antes de substituí-lo.
No Windows, ative o ambiente com `.venv\Scripts\activate` e execute os mesmos
comandos Python; Tesseract e Poppler precisam estar instalados e disponíveis no
`PATH`.

Depois de iniciar o servidor, abra <http://127.0.0.1:5000> e entre com a conta
criada. Para encerrar, use `Ctrl+C` no terminal. SQLite grava o banco em
`autentke.db` na pasta do projeto; anexos ficam em `instance/uploads/`.
Esses arquivos permanecem no computador entre reinícios. Faça cópias de
segurança periódicas de `autentke.db` e `instance/uploads/`.
Se o projeto já tinha um `autentke.db` na pasta principal, a configuração local
continua usando esse mesmo banco; não o apague ao atualizar o código.

O catálogo em `/catalogo` é público para quem já consegue acessar o seu
computador; dashboard, relatórios, anexos e ações de gestão exigem login. O
servidor inicia ligado apenas a `127.0.0.1`, não o exponha à internet.

## Anexos de joias

Em **Adicionar Joia**, selecione foto ou PDF. A leitura local tenta sugerir nome
e custo automaticamente. Em notas de pedido tabulares, o sistema mostra uma
lista de itens para revisar, deixa desmarcadas linhas reconhecidas como
embalagem e cadastra cada linha selecionada como um produto. Confira nomes,
categorias e custos antes de confirmar. O documento original é guardado uma
vez e compartilhado pelos produtos importados. PDFs de até 10 páginas são
processados página por página. PDFs com texto selecionável e PDFs escaneados
passam pela leitura local; imagens e PDFs escaneados precisam de Tesseract,
idioma português e, para PDFs, Poppler.

Para a foto que será mostrada no catálogo, use o campo **Foto da joia** (JPG,
PNG ou WEBP, até 10 MB), separado da nota do pedido. Na importação em lote, há
um campo de foto opcional por joia. Use **Editar** no estoque para alterar nome,
categoria, custo, markup e preço manual, substituir a foto ou removê-la. As
imagens ficam armazenadas localmente em `instance/uploads/fotos/`.

## Estoque de brindes

Ao registrar uma compra na tela **Despesas**, marque **Adicionar também ao
estoque de brindes** e informe a quantidade, a unidade e quanto será usado por
venda. O valor informado continua sendo registrado como despesa e o sistema
calcula o custo unitário do item. Por exemplo, registre 300 ml de limpa-pratas
por R$ 50 e configure 5 ml por venda; registre os 50 potes em outra compra,
com consumo de 1 unidade por venda. Cada venda baixa automaticamente as
quantidades configuradas e o painel exibe o saldo. Se faltar algum item, a
venda é registrada, o item sem saldo não é descontado e o painel avisa. Para
repor um item, use o mesmo nome e unidade; deixe o campo de consumo em branco
para manter a configuração anterior.

## Vendas realizadas

A opção **Vendas realizadas** no painel lista o histórico completo. **Excluir
venda** cancela o registro, devolve a joia ao estoque e repõe os brindes
associados à venda; essa ação também remove os dados da venda dos relatórios.

## Testes

```bash
python -m unittest discover -s tests -v
```
