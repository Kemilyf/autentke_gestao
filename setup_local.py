from getpass import getpass
from pathlib import Path
import re
import secrets
import sys

from werkzeug.security import generate_password_hash


ENV_FILE = Path(__file__).resolve().parent / ".env"


def main():
    if ENV_FILE.exists():
        answer = input("Já existe .env. Substituir credenciais? [s/N] ").strip().lower()
        if answer != "s":
            print("Configuração mantida.")
            return

    username = input("Usuário administrador [admin]: ").strip() or "admin"
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", username):
        sys.exit("Use de 1 a 80 letras, números, ponto, hífen ou sublinhado no usuário.")

    while True:
        password = getpass("Crie uma senha (mínimo 12 caracteres): ")
        if len(password) < 12:
            print("Use pelo menos 12 caracteres.")
            continue
        confirmation = getpass("Confirme a senha: ")
        if password != confirmation:
            print("As senhas não correspondem.")
            continue
        break

    contents = "\n".join((
        "APP_ENV=development",
        f"SECRET_KEY={secrets.token_hex(32)}",
        f"ADMIN_USERNAME={username}",
        f"ADMIN_PASSWORD_HASH={generate_password_hash(password)}",
        "",
    ))
    ENV_FILE.write_text(contents, encoding="utf-8")
    ENV_FILE.chmod(0o600)
    print(f"Configuração salva em {ENV_FILE}.")
    print("Inicie o sistema com: python app.py")


if __name__ == "__main__":
    main()
