"""
MCP de archivos y comandos para tu PC (Python).

Deja que Claude lea/edite una carpeta (y sus subcarpetas) y, si lo permites,
ejecute comandos. Se usa de dos formas a la vez:

  1) Como conector MCP directo:   https://<tunel>/<SECRETO>/mcp
  2) Detrás de JuanWorkspace-MCP: éste llama a la API JSON
                                  https://<tunel>/<SECRETO>/api/<herramienta>

Instalación:
    pip install -U mcp

Uso (normalmente lo lanza iniciar_mcp.bat):
    python mcp_archivos.py --root "C:/Users/TuUsuario/MiCarpeta"
    opcional:  --port 8765  --allow-delete  --allow-exec

Seguridad:
  * Las herramientas de archivos NUNCA salen de --root (rutas, '..' y symlinks).
  * --allow-exec es DISTINTO: los comandos corren con TUS permisos de usuario y
    pueden tocar cualquier cosa de tu PC. Solo se limita la carpeta inicial.
    Está apagado por defecto.
  * El secreto de la URL se guarda en .mcp_secret junto a este archivo.
"""

import argparse
import fnmatch
import locale
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

try:
    import anyio
    import uvicorn
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        f"Falta una dependencia ({exc.name}). Ejecuta:  python -m pip install -U mcp\n"
        "(este script necesita mcp 2.x)"
    )

VERSION = "2.0"

# ----------------------------------------------------------------- config
parser = argparse.ArgumentParser()
parser.add_argument("--root", required=True, help="Carpeta que Claude podrá ver/editar")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--allow-delete", action="store_true",
                    help="Habilita borrar archivos/carpetas")
parser.add_argument("--allow-exec", action="store_true",
                    help="Habilita ejecutar comandos (PELIGROSO: usa tus permisos de usuario)")
args = parser.parse_args()

ROOT = Path(args.root).expanduser().resolve()
if not ROOT.is_dir():
    raise SystemExit(f"La carpeta no existe: {ROOT}")

# Secreto persistente: va dentro de la URL, así solo quien la tenga puede conectarse
SECRET_FILE = Path(__file__).with_name(".mcp_secret")
if SECRET_FILE.exists():
    SECRET = SECRET_FILE.read_text().strip()
else:
    SECRET = secrets.token_urlsafe(32)
    SECRET_FILE.write_text(SECRET)

MAX_READ_CHARS = 200_000
MAX_CMD_OUTPUT = 60_000
# Cloudflare corta las peticiones a ~100 s, por eso el tope en primer plano.
MAX_FOREGROUND_TIMEOUT = 90
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
IS_WINDOWS = os.name == "nt"

# Necesario para que funcione detrás de un túnel (el Host no es localhost)
SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)
HTTP_PATH = f"/{SECRET}/mcp"

mcp = MCPServer(
    "archivos-pc",
    instructions=(
        "Acceso a una carpeta del PC del usuario. Todas las rutas son relativas "
        "a esa carpeta raíz. Prefiere edit_file a write_file para cambios pequeños."
    ),
)

# Registro de herramientas: sirve tanto para MCP como para la API JSON
TOOLS: dict = {}
DISABLED: dict[str, str] = {}   # nombre -> mensaje de por qué está desactivado


def tool(fn):
    TOOLS[fn.__name__] = fn
    mcp.tool()(fn)
    return fn


def disabled_tool(name: str, flag: str):
    DISABLED[name] = (
        f"La herramienta '{name}' está desactivada en el servidor del PC. "
        f"Hay que iniciarlo con la opción {flag} (en iniciar_mcp.bat: opción 2 → responder S)."
    )


# ---------------------------------------------------------------- helpers
def _safe(rel: str) -> Path:
    """Resuelve una ruta relativa y garantiza que quede dentro de ROOT."""
    p = (ROOT / rel).resolve()
    if p != ROOT and not p.is_relative_to(ROOT):
        raise ValueError("Ruta fuera de la carpeta permitida.")
    return p


def _rel(p: Path) -> str:
    r = p.relative_to(ROOT).as_posix()
    return r if r != "." else "."


def _walk(base: Path):
    """Itera archivos y carpetas recursivamente ignorando carpetas pesadas."""
    for p in sorted(base.iterdir()):
        if p.is_dir():
            if p.name in IGNORE_DIRS:
                continue
            yield p
            yield from _walk(p)
        else:
            yield p


def _read_text(p: Path) -> str:
    if not p.is_file():
        raise FileNotFoundError(f"No es un archivo: {_rel(p)}")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError("El archivo no es texto UTF-8 (¿binario?).")


# ------------------------------------------------------- tools: archivos
@tool
def list_directory(path: str = ".", recursive: bool = False, max_entries: int = 500) -> str:
    """Lista archivos y carpetas. Las carpetas terminan en '/'. Rutas relativas a la carpeta raíz."""
    base = _safe(path)
    if not base.is_dir():
        raise NotADirectoryError(f"No es una carpeta: {path}")
    items = _walk(base) if recursive else sorted(base.iterdir())
    out = []
    for p in items:
        if len(out) >= max_entries:
            out.append(f"... (cortado en {max_entries} entradas)")
            break
        if p.is_dir():
            out.append(_rel(p) + "/")
        else:
            out.append(f"{_rel(p)}  ({p.stat().st_size} bytes)")
    return "\n".join(out) or "(vacía)"


@tool
def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """Lee un archivo de texto. Opcionalmente un rango de líneas (1-indexado, inclusivo)."""
    lines = _read_text(_safe(path)).splitlines()
    total = len(lines)
    s = max(start_line, 1) - 1
    e = end_line if end_line else total
    text = "\n".join(f"{i + 1}\t{l}" for i, l in enumerate(lines[s:e], start=s))
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n... (cortado; el archivo tiene {total} líneas, usa start_line/end_line)"
    return f"[{path} — {total} líneas]\n{text}"


@tool
def write_file(path: str, content: str) -> str:
    """Crea o sobrescribe un archivo de texto completo (crea las carpetas necesarias)."""
    p = _safe(path)
    if p.is_dir():
        raise IsADirectoryError(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="")
    return f"Escrito {_rel(p)} ({len(content)} caracteres)"


@tool
def edit_file(path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
    """Reemplaza texto exacto en un archivo. old_str debe aparecer exactamente una vez,
    salvo que replace_all=True. Preferible a write_file para cambios pequeños."""
    p = _safe(path)
    text = _read_text(p)
    count = text.count(old_str)
    if count == 0:
        raise ValueError("old_str no se encontró en el archivo.")
    if count > 1 and not replace_all:
        raise ValueError(f"old_str aparece {count} veces; amplía el contexto o usa replace_all=True.")
    p.write_text(text.replace(old_str, new_str), encoding="utf-8", newline="")
    return f"Editado {_rel(p)} ({count} reemplazo{'s' if count != 1 else ''})"


@tool
def append_file(path: str, content: str) -> str:
    """Agrega texto al final de un archivo (lo crea si no existe)."""
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="") as f:
        f.write(content)
    return f"Agregado a {_rel(p)}"


@tool
def create_directory(path: str) -> str:
    """Crea una carpeta (y las intermedias)."""
    p = _safe(path)
    p.mkdir(parents=True, exist_ok=True)
    return f"Carpeta creada: {_rel(p)}/"


@tool
def move_path(source: str, destination: str) -> str:
    """Mueve o renombra un archivo/carpeta dentro de la carpeta raíz."""
    src, dst = _safe(source), _safe(destination)
    if not src.exists():
        raise FileNotFoundError(source)
    if dst.exists():
        raise FileExistsError(f"Ya existe: {destination}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"Movido {_rel(src)} -> {_rel(dst)}"


@tool
def search_files(pattern: str, path: str = ".") -> str:
    """Busca archivos por nombre con patrón glob (ej. '*.py', 'config*')."""
    base = _safe(path)
    hits = [_rel(p) for p in _walk(base) if p.is_file() and fnmatch.fnmatch(p.name, pattern)]
    return "\n".join(hits[:500]) or "Sin resultados."


@tool
def grep(query: str, path: str = ".", file_glob: str = "*", regex: bool = False,
         ignore_case: bool = True, max_results: int = 100) -> str:
    """Busca texto dentro de los archivos. Devuelve 'ruta:línea: contenido'."""
    base = _safe(path)
    flags = re.IGNORECASE if ignore_case else 0
    pat = re.compile(query if regex else re.escape(query), flags)
    files = [base] if base.is_file() else [p for p in _walk(base) if p.is_file()]
    out = []
    for p in files:
        if not fnmatch.fnmatch(p.name, file_glob):
            continue
        try:
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    out.append(f"{_rel(p)}:{n}: {line.strip()[:200]}")
                    if len(out) >= max_results:
                        return "\n".join(out) + "\n... (límite alcanzado)"
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(out) or "Sin resultados."


if args.allow_delete:
    @tool
    def delete_path(path: str) -> str:
        """Borra un archivo o carpeta (con todo su contenido). Irreversible."""
        p = _safe(path)
        if p == ROOT:
            raise ValueError("No se puede borrar la carpeta raíz.")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"Borrado: {path}"
else:
    disabled_tool("delete_path", "--allow-delete")


# ------------------------------------------------------ tools: comandos
JOBS_DIR = Path(tempfile.gettempdir()) / "mcp_archivos_jobs"
JOBS: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _decode(data: bytes) -> str:
    """cmd.exe responde en la página de códigos OEM (cp850 en español), no en UTF-8."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        enc = "oem" if IS_WINDOWS else locale.getpreferredencoding(False)
        try:
            return data.decode(enc, errors="replace")
        except LookupError:
            return data.decode("utf-8", errors="replace")


def _trim(text: str) -> str:
    if len(text) <= MAX_CMD_OUTPUT:
        return text
    half = MAX_CMD_OUTPUT // 2
    return (text[:half] + f"\n... [{len(text) - MAX_CMD_OUTPUT} caracteres omitidos] ...\n" + text[-half:])


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn(command: str, cwd: Path, stdout) -> subprocess.Popen:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1"}
    kwargs: dict = dict(shell=True, cwd=str(cwd), stdin=subprocess.DEVNULL,
                        stdout=stdout, stderr=subprocess.STDOUT, env=env)
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _tail(path: Path, lines: int) -> str:
    try:
        text = _decode(path.read_bytes())
    except OSError:
        return ""
    return _trim("\n".join(text.splitlines()[-lines:]))


if args.allow_exec:
    @tool
    def run_command(command: str, cwd: str = ".", timeout: int = 60, background: bool = False) -> str:
        """Ejecuta un comando de shell en el PC del usuario (cmd.exe en Windows) y devuelve
        código de salida y salida combinada (stdout+stderr). 'cwd' es relativo a la carpeta
        raíz. El comando NO es interactivo (no puede pedir input).

        Primer plano: espera hasta 'timeout' segundos (máx. 90). Para tareas largas
        (instalaciones, builds, servidores) usa background=True: devuelve un job_id y
        luego consulta job_status / stop_job.

        CUIDADO: el comando corre con los permisos del usuario y puede tocar archivos
        fuera de la carpeta raíz. Pide confirmación al usuario antes de comandos
        destructivos (borrar, formatear, instalar, tocar el registro, etc.).
        """
        workdir = _safe(cwd)
        if not workdir.is_dir():
            raise NotADirectoryError(f"cwd no es una carpeta: {cwd}")

        if background:
            JOBS_DIR.mkdir(parents=True, exist_ok=True)
            job_id = uuid.uuid4().hex[:8]
            log = JOBS_DIR / f"{job_id}.log"
            logf = open(log, "wb")
            proc = _spawn(command, workdir, logf)
            with _jobs_lock:
                JOBS[job_id] = {"proc": proc, "log": log, "logf": logf, "command": command,
                                "cwd": _rel(workdir), "started": time.time()}
            return (f"Trabajo en segundo plano iniciado: job_id={job_id}\n"
                    f"Consulta la salida con job_status('{job_id}') y detenlo con stop_job('{job_id}').")

        timeout = max(1, min(int(timeout), MAX_FOREGROUND_TIMEOUT))
        proc = _spawn(command, workdir, subprocess.PIPE)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                out, _ = proc.communicate(timeout=5)
            except Exception:
                out = b""
        text = _trim(_decode(out or b""))
        if timed_out:
            return (f"TIEMPO AGOTADO tras {timeout}s: el comando fue detenido.\n"
                    f"Si necesita más tiempo, vuelve a lanzarlo con background=True.\n"
                    f"--- salida parcial ---\n{text}")
        return f"código de salida: {proc.returncode}\n--- salida ---\n{text}"

    @tool
    def job_status(job_id: str, tail_lines: int = 100) -> str:
        """Estado de un trabajo lanzado con run_command(background=True) y las últimas líneas de su salida."""
        with _jobs_lock:
            job = JOBS.get(job_id)
        if not job:
            raise KeyError(f"No existe el trabajo {job_id}. Trabajos conocidos: {', '.join(JOBS) or 'ninguno'}")
        code = job["proc"].poll()
        state = "en ejecución" if code is None else f"terminado (código {code})"
        secs = int(time.time() - job["started"])
        return (f"job {job_id}: {state} — hace {secs}s — cmd: {job['command']}\n"
                f"--- últimas {tail_lines} líneas ---\n{_tail(job['log'], tail_lines)}")

    @tool
    def stop_job(job_id: str) -> str:
        """Detiene (con todo su árbol de procesos) un trabajo en segundo plano."""
        with _jobs_lock:
            job = JOBS.get(job_id)
        if not job:
            raise KeyError(f"No existe el trabajo {job_id}.")
        if job["proc"].poll() is None:
            _kill_tree(job["proc"])
            return f"Trabajo {job_id} detenido."
        return f"El trabajo {job_id} ya había terminado (código {job['proc'].returncode})."
else:
    for _n in ("run_command", "job_status", "stop_job"):
        disabled_tool(_n, "--allow-exec")


# ------------------------------------------------------------- API JSON
# Usada por JuanWorkspace-MCP. El secreto va en la ruta, igual que en /mcp.
@mcp.custom_route(f"/{SECRET}/api/ping", methods=["GET"])
async def api_ping(request: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": "mcp-archivos",
            "version": VERSION,
            "root_name": ROOT.name,
            "allow_delete": args.allow_delete,
            "allow_exec": args.allow_exec,
            "tools": sorted(TOOLS),
        },
        # el panel de JuanWorkspace prueba el túnel desde el navegador
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
    )


@mcp.custom_route(f"/{SECRET}/api/{{tool_name}}", methods=["POST"])
async def api_call(request: Request) -> Response:
    name = request.path_params["tool_name"]
    if name in DISABLED:
        return JSONResponse({"ok": False, "error": "tool_disabled", "message": DISABLED[name]}, status_code=403)
    fn = TOOLS.get(name)
    if fn is None:
        return JSONResponse({"ok": False, "error": "unknown_tool", "message": f"No existe la herramienta '{name}'."},
                            status_code=404)
    try:
        raw = await request.body()
        payload = (await request.json()) if raw else {}
        if not isinstance(payload, dict):
            raise ValueError("el cuerpo debe ser un objeto JSON")
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": "bad_request", "message": str(exc)}, status_code=400)
    try:
        result = await anyio.to_thread.run_sync(lambda: fn(**payload))
    except TypeError as exc:
        return JSONResponse({"ok": False, "error": "bad_arguments", "message": str(exc)}, status_code=400)
    except Exception as exc:  # error de la herramienta: se informa, no se oculta
        return JSONResponse({"ok": False, "error": type(exc).__name__, "message": str(exc)})
    return JSONResponse({"ok": True, "result": result})


# ------------------------------------------------------------------- main
if __name__ == "__main__":
    print(f"\n  Versión           : {VERSION}")
    print(f"  Carpeta permitida : {ROOT}")
    print(f"  Borrado habilitado: {'SI' if args.allow_delete else 'no (--allow-delete)'}")
    print(f"  Comandos          : {'SI  <-- corren con tus permisos de usuario' if args.allow_exec else 'no (--allow-exec)'}")
    print(f"  URL local         : http://{args.host}:{args.port}/{SECRET}/mcp")
    print(f"  Con el túnel      : https://<tu-túnel>/{SECRET}/mcp\n")
    asgi_app = mcp.streamable_http_app(
        streamable_http_path=HTTP_PATH,
        transport_security=SECURITY,
        host=args.host,
    )
    uvicorn.run(asgi_app, host=args.host, port=args.port, log_level="warning")
