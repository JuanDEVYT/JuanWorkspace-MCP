#!/usr/bin/env python3
"""
panel.py — Interfaz de terminal para MCP Archivos
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import ctypes
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# ── colores ANSI ──────────────────────────────────────────────────────────────
if os.name == "nt":
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(-11), 7)

G  = "\033[32m"; LG = "\033[92m"; Y = "\033[33m"; R = "\033[31m"
C  = "\033[36m"; W = "\033[97m";  DIM= "\033[2m";  RST= "\033[0m"; BLD= "\033[1m"

HERE        = Path(__file__).parent
CFG_FILE    = HERE / "mcp_config.py"
SECRET_FILE = HERE / ".mcp_secret"
URL_FILE    = HERE / "url_conector.txt"
CF_LOG_FILE = HERE / "cloudflared.log"
JW_SESSION  = HERE / "jw_session.json"   # guarda token de JuanWorkspace

JW_BASE       = "https://juansmalldb.pythonanywhere.com"
JW_TUNNEL_SLUG = "pc-tunnel"

cfg = {"root": None, "port": 8765, "allow_delete": False, "allow_exec": False}


def load_cfg():
    if CFG_FILE.exists():
        g = {}
        exec(CFG_FILE.read_text(encoding="utf-8"), g)
        for k in cfg:
            if k in g:
                cfg[k] = g[k]


def save_cfg():
    CFG_FILE.write_text(
        f'root = {repr(cfg["root"])}\n'
        f'port = {cfg["port"]}\n'
        f'allow_delete = {cfg["allow_delete"]}\n'
        f'allow_exec = {cfg["allow_exec"]}\n',
        encoding="utf-8")


def find_cloudflared():
    for name in ("cloudflared.exe", "cloudflared"):
        p = HERE / name
        if p.exists():
            return str(p)
    return shutil.which("cloudflared")


def get_secret():
    if SECRET_FILE.exists():
        s = SECRET_FILE.read_text().strip()
        if s:
            return s
    s = secrets.token_urlsafe(32)
    SECRET_FILE.write_text(s)
    return s


def now():
    return datetime.now().strftime("%H:%M:%S")


def log(color, tag, msg):
    print(f"{DIM}[{now()}]{RST} {color}{BLD}{tag:5}{RST} {msg}")


def sep(title=""):
    w = 60
    if title:
        pad = (w - len(title) - 2) // 2
        rest = w - pad - len(title) - 2
        print(f"{G}{'─'*pad} {W}{BLD}{title}{RST}{G} {'─'*rest}{RST}")
    else:
        print(f"{G}{'─'*w}{RST}")


def clear():
    os.system("cls" if os.name == "nt" else "clear")


def pause(msg="  Pulsa Enter para volver al menú…"):
    input(f"\n{DIM}{msg}{RST} ")


# ── procesos activos ──────────────────────────────────────────────────────────
procs: dict = {}
tunnel_url = {"value": None}

# Regex — busca en el texto del mensaje JSON o en texto plano
URL_RE = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')


def _read_log_bytes() -> str:
    """
    Lee cloudflared.log usando 'type' de cmd.exe (Windows) o lectura directa.
    Esto evita el PermissionError cuando cloudflared tiene el archivo abierto
    con lock exclusivo (comportamiento habitual en Windows).
    """
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["cmd", "/c", "type", str(CF_LOG_FILE)],
                capture_output=True, timeout=5
            )
            # cmd usa la página de códigos del sistema (cp850/cp1252)
            for enc in ("utf-8", "cp1252", "cp850", "latin-1"):
                try:
                    return result.stdout.decode(enc)
                except UnicodeDecodeError:
                    continue
            return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
    # Linux/Mac o fallback
    try:
        return CF_LOG_FILE.read_bytes().decode("utf-8", errors="replace")
    except Exception:
        return ""


def watch_cloudflared_log(url_event: threading.Event, timeout: int = 60):
    """
    Monitorea CF_LOG_FILE hasta encontrar la URL pública del túnel.
    cloudflared en Windows usa WriteConsole() y no escribe al pipe estándar,
    por eso usamos --logfile. Leemos con 'type' para evitar el file-lock de Windows.
    """
    deadline = time.time() + timeout
    seen_bytes = 0

    while time.time() < deadline:
        if CF_LOG_FILE.exists():
            content = _read_log_bytes()
            if content and len(content) > seen_bytes:
                new_text = content[seen_bytes:]
                seen_bytes = len(content)
                for line in new_text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # Intenta parsear como JSON para mostrar solo el mensaje
                    try:
                        obj = json.loads(line)
                        msg = obj.get("message", line)
                    except Exception:
                        msg = line
                    print(f"  {C}{DIM}[TUN]{RST} {msg}")
                    sys.stdout.flush()
                    # Busca la URL en la línea cruda (puede estar en el mensaje con pipes | url |)
                    m = URL_RE.search(line)
                    if m:
                        tunnel_url["value"] = m.group(0)
                        url_event.set()
                        return
        time.sleep(0.4)


def stream_output(proc, label, color):
    """Lee stdout+stderr de un proceso en hilos daemon (para el servidor MCP)."""
    def read_pipe(pipe):
        try:
            for raw in pipe:
                line = raw.rstrip()
                if not line:
                    continue
                print(f"  {color}{DIM}[{label}]{RST} {line}")
                sys.stdout.flush()
        except Exception:
            pass

    if proc.stderr:
        threading.Thread(target=read_pipe, args=(proc.stderr,), daemon=True).start()
    if proc.stdout:
        threading.Thread(target=read_pipe, args=(proc.stdout,), daemon=True).start()


def kill_all():
    for name, p in list(procs.items()):
        if p.poll() is None:
            log(Y, "STOP", f"Deteniendo {name}…")
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                   capture_output=True, timeout=5)
                else:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
    procs.clear()
    tunnel_url["value"] = None


def make_popen_kwargs():
    """kwargs para el servidor MCP (captura stdout+stderr)."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              text=True, encoding="utf-8", errors="replace", env=env)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return kw


def launch_cloudflared(cf, port):
    """
    Lanza cloudflared con --logfile para capturar su salida en disco.
    cloudflared en Windows usa WriteConsole() y NO escribe al pipe estándar.
    watch_cloudflared_log() lee el archivo via 'cmd /c type' para evitar el
    file-lock exclusivo que cloudflared mantiene en Windows.
    """
    if CF_LOG_FILE.exists():
        try:
            CF_LOG_FILE.unlink()
        except Exception:
            pass

    cmd = [cf, "tunnel",
           "--loglevel", "info",
           "--logfile", str(CF_LOG_FILE),
           "--url", f"http://localhost:{port}"]
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kw)
    except OSError as e:
        if getattr(e, "winerror", None) == 740:
            log(R, "ERROR", "cloudflared requiere elevación (WinError 740).")
            log(Y, "INFO ", "Ejecuta iniciar_mcp.bat como Administrador.")
        else:
            log(R, "ERROR", f"No se pudo iniciar cloudflared: {e}")
            log(Y, "INFO ", f"Ruta usada: {cf}")
        return None


# ── JuanWorkspace: push automático de la URL del túnel ───────────────────────

def _jw_request(path, method="GET", body=None, token=None):
    """Llamada HTTP mínima sin dependencias externas."""
    url = JW_BASE + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", str(e))
        except Exception:
            msg = str(e)
        raise RuntimeError(msg)


def _jw_load_session():
    """Devuelve el token guardado o None."""
    if JW_SESSION.exists():
        try:
            return json.loads(JW_SESSION.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _jw_save_session(data):
    JW_SESSION.write_text(json.dumps(data), encoding="utf-8")


def _jw_login_interactive():
    """Pide credenciales al usuario, hace login y guarda el token."""
    sep("LOGIN JUANWORKSPACE")
    print(f"\n  {DIM}Solo se pregunta una vez. El token se guarda en {JW_SESSION.name}.{RST}\n")
    username = input(f"  {W}Usuario JuanWorkspace: {RST}").strip()
    import getpass
    password = getpass.getpass(f"  {W}Contraseña: {RST}")
    try:
        data = _jw_request("/api/ecosystem/login", "POST",
                            {"username": username, "password": password})
        _jw_save_session(data)
        log(G, "JW   ", f"Sesión guardada como {data.get('username', username)}")
        return data
    except RuntimeError as e:
        log(R, "ERROR", f"Login fallido: {e}")
        return None


def _jw_valid_token(session):
    """Comprueba que el token sigue activo."""
    if not session or not session.get("access_token"):
        return False
    try:
        _jw_request("/api/ecosystem/me", token=session["access_token"])
        return True
    except RuntimeError:
        return False


def _jw_get_session():
    """Devuelve una sesión válida (cargada o pidiendo login)."""
    sess = _jw_load_session()
    if _jw_valid_token(sess):
        return sess
    # Token expirado o inexistente → login interactivo
    log(Y, "JW   ", "Token expirado o no encontrado. Inicia sesión en JuanWorkspace.")
    return _jw_login_interactive()


def _jw_ensure_connected(token):
    """Conecta la app pc-tunnel si no estaba conectada."""
    try:
        _jw_request(f"/api/ecosystem/apps/{JW_TUNNEL_SLUG}/connect",
                    "POST", {}, token=token)
    except RuntimeError:
        pass  # ya estaba conectada, ignorar


def _jw_get_tunnel_doc(token):
    """Devuelve el documento del túnel o None."""
    try:
        res = _jw_request(f"/api/ecosystem/apps/{JW_TUNNEL_SLUG}/data",
                          token=token)
        docs = res.get("data", [])
        return next((d for d in docs if d.get("kind") == "tunnel"), None)
    except RuntimeError:
        return None


def push_tunnel_url(full_url):
    """
    Sube automáticamente la URL del túnel a JuanWorkspace.
    Se llama justo después de que cloudflared emite la URL.
    """
    sep("JUANWORKSPACE")
    log(C, "JW   ", "Subiendo URL del túnel a JuanWorkspace…")

    sess = _jw_get_session()
    if not sess:
        log(R, "JW   ", "No se pudo autenticar. Sube la URL manualmente desde la web.")
        return

    token = sess["access_token"]
    _jw_ensure_connected(token)

    payload = {
        "kind":       "tunnel",
        "tunnel_url": full_url,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    doc = _jw_get_tunnel_doc(token)
    try:
        if doc and doc.get("_id"):
            _jw_request(f"/api/ecosystem/apps/{JW_TUNNEL_SLUG}/data/{doc['_id']}",
                        "PATCH", payload, token=token)
            log(G, "JW   ", "URL actualizada en JuanWorkspace ✓")
        else:
            _jw_request(f"/api/ecosystem/apps/{JW_TUNNEL_SLUG}/data",
                        "POST", payload, token=token)
            log(G, "JW   ", "URL guardada en JuanWorkspace ✓")
        log(G, "JW   ", "Claude ya puede conectarse a tu PC automáticamente.")
    except RuntimeError as e:
        log(R, "JW   ", f"No se pudo subir la URL: {e}")
        log(Y, "JW   ", "Súbela manualmente desde juanworkspace.wasmer.app")


def do_logout_jw():
    """Elimina la sesión guardada de JuanWorkspace."""
    if JW_SESSION.exists():
        JW_SESSION.unlink()
        log(G, "OK  ", "Sesión de JuanWorkspace eliminada.")
    else:
        log(Y, "INFO ", "No había sesión guardada.")


def do_start():
    if not cfg["root"] or not Path(cfg["root"]).is_dir():
        log(R, "ERROR", "No hay carpeta configurada. Ve a Configurar primero (opción 2).")
        return

    cf = find_cloudflared()
    if not cf:
        log(R, "ERROR", "No se encontró cloudflared.exe junto al panel.")
        log(Y, "INFO ", "Descárgalo: https://github.com/cloudflare/cloudflared/releases/latest")
        log(Y, "INFO ", f"Ponlo aquí: {HERE}")
        return

    kill_all()
    secret = get_secret()
    sep("INICIANDO")

    # ── servidor Python ───────────────────────────────────────────────────────
    log(G, "SRV  ", "Arrancando mcp_archivos.py…")
    srv_args = [sys.executable, str(HERE / "mcp_archivos.py"),
                "--root", cfg["root"], "--port", str(cfg["port"])]
    if cfg["allow_delete"]:
        srv_args.append("--allow-delete")
    if cfg["allow_exec"]:
        srv_args.append("--allow-exec")

    try:
        srv = subprocess.Popen(srv_args, **make_popen_kwargs())
    except Exception as e:
        log(R, "ERROR", f"No se pudo iniciar el servidor: {e}")
        return

    procs["servidor"] = srv
    threading.Thread(target=stream_output, args=(srv, "SRV", G), daemon=True).start()
    time.sleep(1.5)

    if srv.poll() is not None:
        log(R, "ERROR", "El servidor terminó inesperadamente. Revisa los mensajes [SRV] de arriba.")
        return

    log(G, "SRV  ", f"OK · http://127.0.0.1:{cfg['port']}/{secret}/mcp")

    # ── cloudflared ───────────────────────────────────────────────────────────
    log(C, "TUNEL", f"Arrancando cloudflared → puerto {cfg['port']}…")
    log(C, "TUNEL", f"Ejecutable: {cf}")
    log(C, "TUNEL", f"Log: {CF_LOG_FILE}")
    tunnel_url["value"] = None
    url_event = threading.Event()

    cf_proc = launch_cloudflared(cf, cfg["port"])
    if cf_proc is None:
        return

    procs["tunel"] = cf_proc
    threading.Thread(target=watch_cloudflared_log, args=(url_event, 60), daemon=True).start()

    log(C, "TUNEL", "Esperando URL pública (hasta 60 s)…")
    got = url_event.wait(timeout=60)

    if not got or not tunnel_url["value"]:
        log(R, "ERROR", "No se capturó la URL en 60 s.")
        if CF_LOG_FILE.exists():
            log(Y, "INFO ", f"Contenido del log ({CF_LOG_FILE}):")
            content = _read_log_bytes()
            if content:
                for line in content.splitlines()[:20]:
                    print(f"    {line}")
            else:
                log(Y, "INFO ", "El log está vacío o no se pudo leer.")
        else:
            log(Y, "INFO ", "cloudflared no creó el archivo de log.")
            log(Y, "INFO ", "Puede requerir elevación (clic derecho → Ejecutar como admin).")
        return

    full_url = f"{tunnel_url['value']}/{secret}/mcp"
    URL_FILE.write_text(full_url, encoding="utf-8")
    try:
        subprocess.run(["clip"], input=full_url, text=True, capture_output=True)
    except Exception:
        pass

    # ── Push automático a JuanWorkspace ──────────────────────────────────────
    try:
        push_tunnel_url(full_url)
    except Exception as e:
        log(Y, "JW   ", f"Push automático falló ({e}). Puedes subirla manualmente.")

    sep("LISTO")
    print(f"\n  {W}{BLD}URL del conector:{RST}\n")
    print(f"  {LG}{BLD}{full_url}{RST}\n")
    print(f"  {DIM}URL subida automáticamente a JuanWorkspace.{RST}")
    print(f"  {DIM}Si algo falló, pégala manualmente en juanworkspace.wasmer.app{RST}\n")
    sep()
    print(f"\n  {Y}Deja esta ventana abierta.{RST} Pulsa {W}Ctrl+C{RST} para detener todo.\n")

    try:
        while True:
            time.sleep(2)
            dead = [n for n, p in procs.items() if p.poll() is not None]
            for n in dead:
                log(R, "WARN ", f"{n} se cerró inesperadamente.")
                del procs[n]
            if not procs:
                log(R, "WARN ", "Todos los procesos terminaron.")
                break
    except KeyboardInterrupt:
        print()
        log(Y, "STOP", "Ctrl+C — deteniendo todo…")
        kill_all()
        log(G, "OK  ", "Detenido.")


def do_config():
    clear()
    sep("CONFIGURAR")
    print()

    while True:
        current = cfg["root"] or f"{R}(sin configurar){RST}"
        print(f"  Carpeta actual : {Y}{current}{RST}")
        val = input("  Nueva carpeta  (Enter para mantener, puedes arrastrarla): ").strip().strip('"').strip("'")
        if not val:
            if cfg["root"]:
                break
            log(R, "ERROR", "Debes configurar una carpeta.")
            continue
        if val.endswith("\\") or val.endswith("/"):
            val = val[:-1]
        if not Path(val).is_dir():
            log(R, "ERROR", "Esa carpeta no existe, inténtalo de nuevo.")
            continue
        cfg["root"] = val
        break

    val = input(f"  Puerto         [{cfg['port']}]: ").strip()
    if val.isdigit():
        cfg["port"] = int(val)

    curr = "S" if cfg["allow_delete"] else "N"
    val = input(f"  Permitir borrar archivos? (S/N) [{curr}]: ").strip().upper()
    if val in ("S", "N"):
        cfg["allow_delete"] = (val == "S")

    if not cfg["allow_exec"]:
        print(f"\n  {Y}{BLD}ADVERTENCIA:{RST}{Y} los comandos corren con TUS permisos")
        print(f"  y pueden tocar cualquier archivo del PC.{RST}")
    curr = "S" if cfg["allow_exec"] else "N"
    val = input(f"  Permitir ejecutar comandos? (S/N) [{curr}]: ").strip().upper()
    if val in ("S", "N"):
        cfg["allow_exec"] = (val == "S")

    save_cfg()
    print()
    log(G, "OK  ", "Configuración guardada.")


def do_show_url():
    if URL_FILE.exists():
        url = URL_FILE.read_text(encoding="utf-8").strip()
        print(f"\n  {LG}{BLD}{url}{RST}")
        try:
            subprocess.run(["clip"], input=url, text=True, capture_output=True)
            print(f"  {DIM}(copiada al portapapeles){RST}\n")
        except Exception:
            pass
    else:
        log(Y, "INFO ", "Aún no hay URL guardada. Usa Iniciar primero.")


def do_resecret():
    ans = input(f"  {Y}Esto invalida la URL actual. ¿Continuar? (S/N): {RST}").strip().upper()
    if ans != "S":
        return
    kill_all()
    for f in (SECRET_FILE, URL_FILE):
        if f.exists():
            f.unlink()
    log(G, "OK  ", "Secreto eliminado. Se generará uno nuevo al iniciar.")


def menu():
    load_cfg()
    while True:
        clear()
        sep("MCP ARCHIVOS  v4")
        root_show = cfg["root"] or f"{R}[sin configurar — elige opción 2]{RST}"
        del_show  = f"{G}SI{RST}" if cfg["allow_delete"] else f"{DIM}no{RST}"
        exec_show = f"{Y}SI  ← control total del PC{RST}" if cfg["allow_exec"] else f"{DIM}no{RST}"
        srv_ok    = "servidor" in procs and procs["servidor"].poll() is None
        tun_ok    = "tunel"    in procs and procs["tunel"].poll()    is None
        srv_s     = f"{LG}activo{RST}"   if srv_ok else f"{DIM}detenido{RST}"
        tun_s     = f"{LG}activo{RST}"   if tun_ok else f"{DIM}detenido{RST}"
        # Estado sesión JuanWorkspace
        jw_sess = _jw_load_session()
        jw_who  = jw_sess.get("username", "?") if jw_sess else None
        jw_s    = f"{LG}{jw_who}{RST}" if jw_who else f"{DIM}no autenticado{RST}"

        print(f"""
  Carpeta   : {W}{root_show}{RST}
  Puerto    : {cfg['port']}
  Borrar    : {del_show}      Comandos : {exec_show}
  Servidor  : {srv_s}      Túnel    : {tun_s}
  JuanWS    : {jw_s}

  {G}[1]{RST} Iniciar servidor + túnel  {DIM}(sube URL a JuanWorkspace automáticamente){RST}
  {G}[2]{RST} Configurar carpeta / opciones
  {G}[3]{RST} Ver URL del túnel
  {G}[4]{RST} Detener todo
  {G}[5]{RST} Regenerar secreto  (invalida URL actual)
  {G}[6]{RST} Cerrar sesión de JuanWorkspace
  {G}[7]{RST} Salir
""")
        try:
            opt = input(f"  {W}Elige: {RST}").strip()
        except (KeyboardInterrupt, EOFError):
            kill_all()
            sys.exit(0)

        if opt == "1":
            do_start()
            pause("Pulsa Enter para volver al menú…")
        elif opt == "2":
            do_config()
            pause()
        elif opt == "3":
            do_show_url()
            pause()
        elif opt == "4":
            kill_all()
            log(G, "OK  ", "Detenido.")
            pause()
        elif opt == "5":
            do_resecret()
            pause()
        elif opt == "6":
            do_logout_jw()
            pause()
        elif opt == "7":
            kill_all()
            sys.exit(0)
        else:
            log(Y, "INFO ", "Opción no válida.")
            time.sleep(0.8)


if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        kill_all()
        sys.exit(0)
