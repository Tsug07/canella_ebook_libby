"""Emergência: alguém ficou trancado para fora do painel.

Roda DENTRO do container, onde já estão o DATABASE_URL e o código:

    docker exec canella_ebooks python resetar_admin.py --listar
    docker exec canella_ebooks python resetar_admin.py fulano
    docker exec canella_ebooks python resetar_admin.py fulano --sem-2fa
    docker exec canella_ebooks python resetar_admin.py --criar "Nome Sobrenome" fulano

Redefinir: gera senha temporária (a pessoa troca no 1º acesso), reativa a
conta, destrava as tentativas e derruba as sessões abertas. --sem-2fa também
remove a verificação em duas etapas (celular perdido). --criar cria um
administrador novo (ex.: o único admin saiu da empresa).
"""
from __future__ import annotations
import argparse
import sys

import app  # noqa: F401  (carrega o .env em dev)
from app import database as db
from app import equipe


def main() -> int:
    ap = argparse.ArgumentParser(description="Redefine o acesso de um usuário do painel.")
    ap.add_argument("usuario", nargs="?", help="usuário (ou e-mail) da pessoa")
    ap.add_argument("--sem-2fa", action="store_true", help="remove também a verificação em duas etapas")
    ap.add_argument("--criar", metavar="NOME", help="cria um ADMINISTRADOR novo com este nome")
    ap.add_argument("--listar", action="store_true", help="lista os usuários")
    a = ap.parse_args()
    db.init_db()

    if a.listar:
        for u in db.q("SELECT * FROM admin_usuario ORDER BY id"):
            print(f"{u['id']:>3}  {u['usuario']:<24} {(u['email'] or ''):<36} {u['papel']:<7} {'ativo' if u['ativo'] else 'DESATIVADO':<10} "
                  f"{'2 etapas' if u['totp_segredo'] else ''}")
        return 0
    if not a.usuario:
        ap.print_help()
        return 1

    temp = equipe.senha_temporaria()
    if a.criar:
        usuario = a.usuario.strip().lower()
        problema = equipe.problema_no_usuario(usuario)
        if problema:
            print(problema)
            return 1
        if equipe.por_usuario(usuario):
            print("Já existe esse usuário. Rode sem --criar para redefinir a senha.")
            return 1
        db.ex("INSERT INTO admin_usuario(nome, usuario, senha_hash, papel, trocar_senha) VALUES (%s,%s,%s,'admin',TRUE)",
              (a.criar.strip(), usuario, equipe.hash_senha(temp)))
        equipe.registrar(None, "criou administrador pelo servidor", f"@{usuario}", "resetar_admin.py")
        print(f"Administrador criado. Usuário: {usuario}")
    else:
        u = equipe.por_login(a.usuario)
        if not u:
            print("Usuário não encontrado. Use --listar para ver os usuários, ou --criar.")
            return 1
        db.ex(f"""UPDATE admin_usuario SET senha_hash = %s, trocar_senha = TRUE, ativo = TRUE,
                  sessao_versao = sessao_versao + 1{', totp_segredo = NULL' if a.sem_2fa else ''} WHERE id = %s""",
              (equipe.hash_senha(temp), u["id"]))
        equipe.zerar(f"login-conta:{u['usuario'].lower()}")
        equipe.registrar(None, "redefiniu acesso pelo servidor", f"@{u['usuario']}" + (" (sem 2 etapas)" if a.sem_2fa else ""),
                         "resetar_admin.py")
        print(f"Acesso redefinido. Usuário: {u['usuario']}" + (" (verificação em duas etapas removida)" if a.sem_2fa else ""))
    print(f"Senha temporária: {temp}")
    print("No primeiro acesso a pessoa cria a senha dela.")
    return 0


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        db.close_db()
    sys.exit(codigo)
