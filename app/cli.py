from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import Branch, User
from app.security import hash_password
from app.services.legacy_import import import_legacy_sqlite
from app.services.seed import seed_database


def init_database(args) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_database(db, include_demo=args.demo)
    finally:
        db.close()
    print("Banco inicializado com sucesso.")


def create_user(args) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.scalar(select(User).where(User.email == args.email.lower())):
            raise SystemExit("Já existe um usuário com esse e-mail.")
        branch = db.scalar(select(Branch).where(Branch.code == args.branch)) if args.branch else None
        password = args.password or getpass.getpass("Senha: ")
        user = User(
            name=args.name,
            email=args.email.lower(),
            password_hash=hash_password(password),
            role=args.role,
            team=args.team,
            branch_id=branch.id if branch else None,
            must_change_password=True,
        )
        db.add(user)
        db.commit()
        print(f"Usuário criado: {user.email}")
    finally:
        db.close()


def import_legacy(args) -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == args.user.lower()))
        if not user:
            raise SystemExit("Usuário responsável não encontrado.")
        branch = db.scalar(select(Branch).where(Branch.code == args.branch)) if args.branch else None
        result = import_legacy_sqlite(
            db,
            path=Path(args.path),
            user=user,
            branch_id=branch.id if branch else None,
        )
        print(
            "Importação concluída: "
            f"{result['imported']} importados, {result['duplicates']} duplicados, "
            f"{result['skipped']} ignorados de {result['total']} registros."
        )
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sulnet-prospecta", description="Administração do Sulnet Prospecta")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-db", help="Cria tabelas, unidades, metas e administrador")
    init.add_argument("--demo", action="store_true", help="Inclui dados de demonstração")
    init.set_defaults(func=init_database)

    user = sub.add_parser("create-user", help="Cria um usuário interno")
    user.add_argument("--name", required=True)
    user.add_argument("--email", required=True)
    user.add_argument("--role", choices=["ADMIN", "GESTOR", "SUPERVISOR", "CONSULTOR"], default="CONSULTOR")
    user.add_argument("--team", choices=["LOJA", "DIGITAL", "PAP", "B2B"], default="LOJA")
    user.add_argument("--branch", help="Código da unidade, ex.: SRO")
    user.add_argument("--password", help="Evite informar em histórico de terminal; omita para entrada segura")
    user.set_defaults(func=create_user)

    legacy = sub.add_parser("import-legacy", help="Importa prospects.db do projeto original")
    legacy.add_argument("--path", required=True, help="Caminho para prospects.db")
    legacy.add_argument("--user", required=True, help="E-mail do responsável pelos leads importados")
    legacy.add_argument("--branch", help="Código da unidade padrão")
    legacy.set_defaults(func=import_legacy)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
