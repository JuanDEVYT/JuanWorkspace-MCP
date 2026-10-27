#!/usr/bin/env python3
"""
panel.py — Interfaz de terminal para MCP Archivos
Reemplaza la lógica del .bat: gestiona servidor + túnel con output en vivo.
"""
import os, sys, re, subprocess, threading, time, secrets, shutil, signal
from pathlib import Path
from datetime import datetime

# ── colores ANSI ──────────────────────────────────────────────────────────────
if os.name == "nt":
    import ctypes
    ctypes.windll.kernel32.SetConsoleMode(
        ctypes.windll.kernel32.GetStdHandle(-11), 7)

G  = "\033[32m"   # verde
LG = "\033[92m"   # verde brillante
Y  = "\033[33m"   # amarillo
R  = "\033[31m"   # rojo
C  = "\033[36m"   # cyan
W  = "\033[97m"   # blanco
DIM= "\033[2m"
RST= "\033[0m"
BLD= "\033[1m"

CFG_FILE   = Path(__file__).with_name("mcp_config.py")
SECRET_FILE= Path(__file__).with_name(".mcp_secret")
URL_FILE   = Path(__file__).with_name("url_conector.txt")

cfg = {"root": None, "port": 8765, "allow_delete": False, "allow_exec": False}

def load_cfg():
    if CFG_FILE.exists():
        g = {}
        exec(CFG_FILE.read_text(encoding="utf-8"), g)
        for k in cfg:
            if k in g:
                cfg[k] = g[k]

def save_cfg():
    lines = [
        f'root = {repr(cfg["root"])}',
        f'port = {cfg["port"]}',
        f'allow_delete = {cfg["allow_delete"]}',
        f'allow_exec = {cfg["allow_exec"]}',
    ]
    CFG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

def find_cloudflared():
    here = Path(__file__).parent
    for name in ("cloudflared.exe", "cloudflared"):
        p = here / name
        if p.exists():
            return str(p)
    found = shutil.which("cloudflared")
    return found

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
    print(f"{DIM}[{now()}]{RST} {color}{BLD}{tag}{RST} {msg}")

def sep(title=""):
    w = 60
    if title:
        pad = (w - len(title) - 2) // 2
        print(f"{G}{'─'*pad} {W}{BLD}{title}{RST}{G} {'─'*(w-pad-len(title)-2)}{RST}")
    else:
        print(f"{G}{'─'*w}{RST}")

# ── procesos activos ──────────────────────────────────────────────────────────
procs: dict[str, subprocess.Popen] = {}
tunnel_url = {"value": None}

def stream_output(proc, label, color, url_event: threading.Event | None = None):
    """Lee líneas del proceso y las imprime en tiempo real. Opcionalmente busca la URL del túnel."""
    url_re_json  = re.compile(r'"url":"(https://[a-z0-9\-]+\.trycloudflare\.com)"')
    url_re_plain = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            print(f"  {color}{DIM}[{label}]{RST} {line}")
            if url_event and not tunnel_url["value"]:
                m = url_re_json.search(line) or url_re_plain.search(line)
                if m:
                    tunnel_url["value"] = m.group(1) if url_re_json.search(line) else m.group(0)
                    url_event.set()
    except Exception:
        pass

def kill_all():
    for name, p in list(procs.items()):
        if p.poll() is None:
            log(Y, "STOP", f"Deteniendo {name}…")
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill","/PID",str(p.pid),"/T","/F"],
                                   capture_output=True, timeout=5)
                else:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try: p.kill()
                except Exception: pass
    procs.clear()
    tunnel_url["value"] = None

def do_start():
    if not cfg["root"] or not Path(cfg["root"]).is_dir():
        log(R, "ERROR", "No hay carpeta configurada. Ve a Configurar primero.")
        return

    cf = find_cloudflared()
    if not cf:
        log(R, "ERROR", "No se encontró cloudflared.exe junto al panel.")
        log(Y, "INFO ", "Descárgalo de: https://github.com/cloudflare/cloudflared/releases/latest")
        log(Y, "INFO ", f"Y ponlo aquí: {Path(__file__).parent}")
        return

    kill_all()
    secret = get_secret()

    sep("INICIANDO")

    # ── servidor Python ───────────────────────────────────────────────────────
    log(G, "SRV  ", "Arrancando mcp_archivos.py…")
    srv_args = [
        sys.executable, str(Path(__file__).with_name("mcp_archivos.py")),
        "--root", cfg["root"],
        "--port", str(cfg["port"]),
    ]
    if cfg["allow_delete"]: srv_args.append("--allow-delete")
    if cfg["allow_exec"]:   srv_args.append("--allow-exec")

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    kw  = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
               text=True, encoding="utf-8", errors="replace", env=env)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True

    srv = subprocess.Popen(srv_args, **kw)
    procs["servidor"] = srv
    threading.Thread(target=stream_output, args=(srv, "SRV", G), daemon=True).start()
    time.sleep(1.5)

    if srv.poll() is not None:
        log(R, "ERROR", "El servidor terminó inesperadamente. Revisa los mensajes de arriba.")
        return

    log(G, "SRV  ", f"Servidor en http://127.0.0.1:{cfg['port']}/{secret}/mcp")

    # ── cloudflared ───────────────────────────────────────────────────────────
    log(C, "TUNEL", f"Arrancando cloudflared → puerto {cfg['port']}…")
    url_event = threading.Event()
    cf_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 text=True, encoding="utf-8", errors="replace", env=env)
    if os.name == "nt":
        cf_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        cf_kw["start_new_session"] = True

    cf_proc = subprocess.Popen([cf, "tunnel", "--url", f"http://localhost:{cfg['port']}"], **cf_kw)
    procs["tunel"] = cf_proc
    threading.Thread(target=stream_output, args=(cf_proc, "TUN", C, url_event), daemon=True).start()

    log(C, "TUNEL", "Esperando URL pública (hasta 60 s)…")
    got = url_event.wait(timeout=60)

    if not got or not tunnel_url["value"]:
        log(R, "ERROR", "No se capturó la URL del túnel en 60 s.")
        log(Y, "INFO ", "Revisa los mensajes [TUN] de arriba para ver qué falló.")
        return

    base_url = tunnel_url["value"]
    full_url = f"{base_url}/{secret}/mcp"
    URL_FILE.write_text(full_url)

    # copiar al portapapeles
    try:
        import subprocess as _sp
        _sp.run(["clip"], input=full_url, text=True, capture_output=True)
        clipboard_ok = True
    except Exception:
        clipboard_ok = False

    sep("LISTO")
    print(f"\n  {W}{BLD}URL del conector (copiada al portapapeles):{RST}\n")
    print(f"  {LG}{BLD}{full_url}{RST}\n")
    print(f"  {DIM}Pégala en JuanWorkspace › Túnel de mi PC › Guardar{RST}")
    print(f"  {DIM}Trátala como contraseña: quien la tenga accede a tu carpeta{RST}\n")
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
        log(Y, "STOP", "Ctrl+C recibido — deteniendo todo…")
        kill_all()
        log(G, "OK  ", "Detenido.")

def do_config():
    sep("CONFIGURAR")
    print()

    # carpeta
    while True:
        current = cfg["root"] or "(sin configurar)"
        print(f"  Carpeta actual: {Y}{current}{RST}")
        val = input(f"  Nueva carpeta (Enter para mantener): ").strip().strip('"')
        if not val:
            if cfg["root"]:
                break
            log(R, "ERROR", "Debes configurar una carpeta.")
            continue
        if val.endswith("\\") or val.endswith("/"):
            val = val[:-1]
        if not Path(val).is_dir():
            log(R, "ERROR", "Esa carpeta no existe.")
            continue
        cfg["root"] = val
        break

    # puerto
    val = input(f"  Puerto [{cfg['port']}]: ").strip()
    if val.isdigit():
        cfg["port"] = int(val)

    # borrado
    curr = "S" if cfg["allow_delete"] else "N"
    val = input(f"  Permitir borrar archivos? (S/N) [{curr}]: ").strip().upper()
    if val in ("S","N"):
        cfg["allow_delete"] = (val == "S")

    # comandos
    if not cfg["allow_exec"]:
        print(f"\n  {Y}{BLD}ADVERTENCIA:{RST}{Y} comandos corren con TUS permisos y pueden")
        print(f"  tocar cualquier archivo del PC, no solo la carpeta.{RST}")
    curr = "S" if cfg["allow_exec"] else "N"
    val = input(f"  Permitir ejecutar comandos? (S/N) [{curr}]: ").strip().upper()
    if val in ("S","N"):
        cfg["allow_exec"] = (val == "S")

    save_cfg()
    log(G, "OK  ", "Configuración guardada.")

def do_show_url():
    if URL_FILE.exists():
        url = URL_FILE.read_text().strip()
        print(f"\n  {LG}{BLD}{url}{RST}")
        try:
            subprocess.run(["clip"], input=url, text=True, capture_output=True)
            print(f"  {DIM}(copiada al portapapeles){RST}\n")
        except Exception:
            pass
    else:
        log(Y, "INFO ", "Aún no hay URL. Usa Iniciar primero.")

def do_resecret():
    ans = input(f"  {Y}Esto invalida la URL actual. ¿Continuar? (S/N): {RST}").strip().upper()
    if ans != "S":
        return
    kill_all()
    if SECRET_FILE.exists(): SECRET_FILE.unlink()
    if URL_FILE.exists():    URL_FILE.unlink()
    log(G, "OK  ", "Secreto eliminado. Se generará uno nuevo al iniciar.")

def menu():
    load_cfg()
    while True:
        sep("MCP ARCHIVOS  v4")
        root_show  = cfg["root"] or f"{R}[sin configurar]{RST}"
        del_show   = f"{G}SI{RST}" if cfg["allow_delete"] else f"{DIM}no{RST}"
        exec_show  = f"{Y}SI  ← control total del PC{RST}" if cfg["allow_exec"] else f"{DIM}no{RST}"
        active_srv = f"{LG}activo{RST}" if "servidor" in procs and procs["servidor"].poll() is None else f"{DIM}detenido{RST}"
        active_tun = f"{LG}activo{RST}" if "tunel"    in procs and procs["tunel"].poll()    is None else f"{DIM}detenido{RST}"
        print(f"""
  Carpeta   : {W}{root_show}{RST}
  Puerto    : {cfg['port']}
  Borrar    : {del_show}      Comandos: {exec_show}
  Servidor  : {active_srv}    Túnel: {active_tun}

  {G}[1]{RST} Iniciar servidor + túnel
  {G}[2]{RST} Configurar carpeta / opciones
  {G}[3]{RST} Ver URL del túnel
  {G}[4]{RST} Detener todo
  {G}[5]{RST} Regenerar secreto (invalida URL actual)
  {G}[6]{RST} Salir
""")
        try:
            opt = input(f"  {W}Elige: {RST}").strip()
        except (KeyboardInterrupt, EOFError):
            kill_all()
            sys.exit(0)

        if opt == "1": do_start()
        elif opt == "2": do_config()
        elif opt == "3": do_show_url()
        elif opt == "4": kill_all(); log(G, "OK  ", "Detenido.")
        elif opt == "5": do_resecret()
        elif opt == "6": kill_all(); sys.exit(0)
        else: log(Y, "INFO ", "Opción no válida.")

        if opt not in ("1",):  # después de iniciar el loop interno lo maneja
            input(f"\n  {DIM}Pulsa Enter para continuar…{RST}")

if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        kill_all()
        sys.exit(0)
