"""Create an isolated Python environment and launch the localhost application."""
import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path


def main():
    if sys.version_info < (3,11):
        print("Serve Python 3.11 o successivo. Versione consigliata: Python 3.12.")
        return 1
    root=Path(__file__).resolve().parents[1]
    os.chdir(root)
    environment=root/".venv"
    python=environment/("Scripts/python.exe" if os.name=="nt" else "bin/python")
    if not python.exists():
        print("Primo avvio: preparo l'ambiente Python isolato...",flush=True)
        venv.create(environment,with_pip=True)
    digest=hashlib.sha256((root/"pyproject.toml").read_bytes()+(root/"requirements.txt").read_bytes()).hexdigest()
    stamp=environment/"studygenius-installed.txt"
    if not stamp.exists() or stamp.read_text()!=digest:
        print("Installo le dipendenze. Il primo avvio richiede una connessione Internet...",flush=True)
        subprocess.run([str(python),"-m","pip","install","--disable-pip-version-check","-r","requirements.txt","-e","."],check=True)
        stamp.write_text(digest)
    print("StudyGenius sta per aprirsi nel browser. Tieni aperta questa finestra.",flush=True)
    return subprocess.call([str(python),"-m","studygenius",*sys.argv[1:]])


if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (OSError,subprocess.CalledProcessError) as exc:
        print(f"Preparazione non riuscita ({type(exc).__name__}). Controlla connessione, Python e permessi della cartella.")
        raise SystemExit(1)
