import csv
from app import app, db, User

CSV_PATH = "participants.csv"


def import_users():
    with app.app_context():
        db.create_all()

        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for row in reader:
                serial = row["sosci_serial"].strip()
                username = row["username"].strip()

                if not serial or not username:
                    print(f"Übersprungen: unvollständige Zeile {row}")
                    continue

                existing_by_serial = User.query.filter_by(sosci_serial=serial).first()
                existing_by_username = User.query.filter_by(username=username).first()

                if existing_by_serial:
                    print(f"SERIAL existiert schon: {serial}")
                    continue

                if existing_by_username:
                    print(f"Username existiert schon: {username}")
                    continue

                user = User(
                    sosci_serial=serial,
                    username=username,
                    password_hash=None
                )
                db.session.add(user)

            db.session.commit()
            print("Import abgeschlossen.")


if __name__ == "__main__":
    import_users()