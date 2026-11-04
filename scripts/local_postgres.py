"""Optional local PostgreSQL bootstrap. Never installs outside this project's venv.

Production/deployment should use compose.yaml or a managed PostgreSQL service.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VECTOR_VERSION = "0.8.0"
VECTOR_SHA256 = "867a2c328d4928a5a9d6f052cd3bc78c7d60228a9b914ad32aa3db88e9de27b0"


def local_defaults(config):
    """Reuse an existing installation's database/socket across project renames."""
    database, socket, port = "shsf", "/tmp/shsf-pg-socket", 55439
    if config.exists():
        saved = urllib.parse.urlparse(config.read_text().strip())
        if saved.scheme in ("postgresql", "postgres"):
            database = urllib.parse.unquote(saved.path.lstrip("/")) or database
            query = urllib.parse.parse_qs(saved.query)
            socket = query.get("host", [socket])[0]
            port = int(query.get("port", [port])[0])
    return database, socket, port


def run(command, **kwargs):
    subprocess.run([str(x) for x in command], check=True, **kwargs)


def build_vector(install):
    control = install / "share/postgresql/extension/vector.control"
    if control.exists():
        return
    tools = ROOT / ".data/tools"
    tools.mkdir(parents=True, exist_ok=True)
    archive = tools / f"pgvector-{VECTOR_VERSION}.tar.gz"
    if not archive.exists():
        request = urllib.request.Request(f"https://github.com/pgvector/pgvector/archive/refs/tags/v{VECTOR_VERSION}.tar.gz",
                                         headers={"User-Agent": "shsf-local-setup/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read(4 * 1024 * 1024)
        archive.write_bytes(body)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != VECTOR_SHA256:
        raise RuntimeError("pgvector source checksum mismatch")
    with tarfile.open(archive) as tar:
        # Archive is pinned and verified; also reject paths outside tools.
        for member in tar.getmembers():
            if not (tools / member.name).resolve().is_relative_to(tools.resolve()) or member.issym() or member.islnk():
                raise RuntimeError("Unsafe archive member")
        if sys.version_info >= (3, 12):
            tar.extractall(tools, filter="data")
        else:
            tar.extractall(tools)
    command = ["make", "-C", tools / f"pgvector-{VECTOR_VERSION}",
               f"PG_CONFIG={install / 'bin/pg_config'}", "OPTFLAGS="]
    if sys.platform == "darwin":
        sdk = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
        command.append(f"PG_SYSROOT={sdk}")
    run(command)
    run(command + ["install"])


def main():
    config = ROOT / ".data/pipeline/database-url"
    database, socket_dir, port = local_defaults(config)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["setup", "start", "stop", "status"])
    parser.add_argument("--socket-dir", default=socket_dir)
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    spec = importlib.util.find_spec("pgserver")
    if spec is None:
        parser.error("Install local tools first: uv pip install -e '.[local-postgres]'")
    install = Path(spec.origin).parent / "pginstall"
    data = ROOT / ".data/postgres"
    bindir = install / "bin"
    if args.action == "setup":
        build_vector(install)
        data.parent.mkdir(parents=True, exist_ok=True)
        if not (data / "PG_VERSION").exists():
            run([bindir / "initdb", "-D", data, "--auth-local=trust", "--auth-host=scram-sha-256", "--encoding=UTF8", "--no-locale"])
    if args.action in ("setup", "start"):
        if not (data / "PG_VERSION").exists():
            parser.error("Run setup first")
        socket = Path(args.socket_dir).resolve()
        if "'" in str(socket) or len(str(socket)) > 80:
            parser.error("Socket path must be short and contain no single quote")
        socket.mkdir(parents=True, exist_ok=True, mode=0o700)
        status = subprocess.run([str(bindir / "pg_ctl"), "-D", str(data), "status"], stdout=subprocess.DEVNULL)
        if status.returncode:
            run([bindir / "pg_ctl", "-D", data, "-l", data / "server.log", "-o",
                 f"-h '' -k '{socket}' -p {args.port}", "start"])
        import psycopg
        url = f"postgresql:///postgres?{urllib.parse.urlencode({'host': str(socket), 'port': args.port})}"
        with psycopg.connect(url, autocommit=True) as conn:
            for name in (database, database + "_test"):
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                    conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
        project_url = url.replace("///postgres?", "///" + urllib.parse.quote(database, safe="") + "?")
        test_url = url.replace("///postgres?", "///" + urllib.parse.quote(database + "_test", safe="") + "?")
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(project_url + "\n")
        config.chmod(0o600)
        print(json.dumps({"database_config": str(config), "database_url": project_url,
                          "test_database_url": test_url, "access": "local Unix socket only; no TCP listener"}, indent=2))
    elif args.action == "stop":
        run([bindir / "pg_ctl", "-D", data, "-m", "fast", "stop"])
    else:
        run([bindir / "pg_ctl", "-D", data, "status"])


if __name__ == "__main__":
    main()
