"""Check the Git candidate set, including untracked files, without printing secret values."""
import re
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PATTERNS=[re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),re.compile(rb"\bAIza[A-Za-z0-9_-]{25,}\b"),
          re.compile(rb"\bAQ\.[A-Za-z0-9_-]{30,}\b"),re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
          re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]


def main():
    raw=subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"],cwd=ROOT)
    paths=sorted(set(p.decode() for p in raw.split(b"\x00") if p))
    failed=[]
    for name in paths:
        p=ROOT/name
        if not p.is_file(): continue
        if any(part in {".studygenius",".venv"} for part in p.parts) or (p.name.startswith(".env") and p.name!=".env.example"):
            failed.append(name+": file privato incluso in Git")
            continue
        data=p.read_bytes()
        if any(pattern.search(data) for pattern in PATTERNS):
            failed.append(name+": possibile credenziale")
    if failed:
        print("Controllo credenziali fallito:\n"+"\n".join(failed))
        return 1
    print(f"Controllati {len(paths)} file: nessuna credenziale rilevata dai pattern configurati.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
