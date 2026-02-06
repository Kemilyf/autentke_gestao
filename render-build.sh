#!/usr/bin/env bash
# Sair imediatamente se um comando falhar
set -o errexit

# Instalar as bibliotecas do requirements.txt
pip install -r requirements.txt

# Comando opcional: Caso queira rodar migrações de banco futuramente
# python manage.py db upgrade