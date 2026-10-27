#!/usr/bin/env python3
"""
panel.py — Interfaz de terminal para MCP Archivos
"""
import os, sys, re, subprocess, threading, time, secrets, shutil, signal, ctypes
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

cfg = {"root": None, "port": 8765, "allow_delete": False, "allow_exec": False}

def load_cfg():
    if CFG_FILE.exists():
        g = {}
        exec(CFG_FILE.read_text(encoding="utf-8"), g)
        for k in cfg:
            if k in g: cfg[k] = g[k]

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
        if p.exists(): return str(p)
    return shutil.which("cloudflared")

def get_secret():
    if SECRET_FILE.exists():
        s = SECRET_FILE.read_text().strip()
        if s: return s
    s = secrets.token_urlsafe(32)
    SECRET_FILE.write_text(s)
    return s

def now(): return datetime.now().strftime("%H:%M:%S")

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

def stream_output(proc, label, color, url_event=None):
    url_re_json  = re.compile(r'"url":"(https://[a-z0-9\-]+\.trycloudflare\.com)"')
    url_re_plain = re.compile(r'https://[a-z0-9\-]+\.trycloudflare\.com')
    try:
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line: continue
            print(f"  {color}{DIM}[{label}]{RST} {line}")
            sys.stdout.flush()
            if url_event and not tunnel_url["value"]:
                m = url_re_json.search(line)
                if m:
                    tunnel_url["value"] = m.group(1)
                    url_event.set()
                else:
                    m = url_re_plain.search(line)
                    if m:
                        tunnel_url["value"] = m.group(0)
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

def make_popen_kwargs(extra_flags=0):
    """kwargs comunes para Popen — sin elevar, sin ventana nueva."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              text=True, encoding="utf-8", errors="replace", env=env)
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP para poder matarlo con taskkill /T
        # NO usamos CREATE_NO_WINDOW aquí porque queremos capturar stdout
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | extra_flags
    else:
        kw["start_new_session"] = True
    return kw

def launch_cloudflared(cf, port):
    """
    Lanza cloudflared con shell=True para evitar WinError 740 en Windows.
    shell=True hace que cmd.exe lo lance y hereda permisos correctamente.
    """
    cmd = f'"{cf}" tunnel --url http://localhost:{port}'
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              text=True, encoding="utf-8", errors="replace",
              env=env, shell=True)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(cmd, **kw)
    except Exception as e:
        log(R, "ERROR", f"No se pudo iniciar cloudflared: {e}")
        log(Y, "INFO ", f"Ruta usada: {cf}")
        log(Y, "INFO ", "Asegúrate de que cloudflared.exe esté junto al panel.py")
        return None

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
    if cfg["allow_delete"]: srv_args.append("--allow-delete")
    if cfg["allow_exec"]:   srv_args.append("--allow-exec")

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
    tunnel_url["value"] = None
    url_event = threading.Event()

    cf_proc = launch_cloudflared(cf, cfg["port"])
    if cf_proc is None:
        return

    procs["tunel"] = cf_proc
    threading.Thread(target=stream_output, args=(cf_proc, "TUN", C, url_event), daemon=True).start()

    log(C, "TUNEL", "Esperando URL pública (hasta 60 s)…")
    got = url_event.wait(timeout=60)

    if not got or not tunnel_url["value"]:
        log(R, "ERROR", "No se capturó la URL en 60 s. Revisa los mensajes [TUN] de arriba.")
        return

    full_url = f"{tunnel_url['value']}/{secret}/mcp"
    URL_FILE.write_text(full_url)
    try:
        subprocess.run(["clip"], input=full_url, text=True, capture_output=True)
    except Exception:
        pass

    sep("LISTO")
    print(f"\n  {W}{BLD}URL del conector:{RST}\n")
    print(f"  {LG}{BLD}{full_url}{RST}\n")
    print(f"  {DIM}Pégala en JuanWorkspace › Túnel de mi PC › Guardar{RST}")
    print(f"  {DIM}Trátala como contraseña.{RST}\n")
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
            if cfg["root"]: break
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
    if val.isdigit(): cfg["port"] = int(val)

    curr = "S" if cfg["allow_delete"] else "N"
    val = input(f"  Permitir borrar archivos? (S/N) [{curr}]: ").strip().upper()
    if val in ("S","N"): cfg["allow_delete"] = (val == "S")

    if not cfg["allow_exec"]:
        print(f"\n  {Y}{BLD}ADVERTENCIA:{RST}{Y} los comandos corren con TUS permisos")
        print(f"  y pueden tocar cualquier archivo del PC.{RST}")
    curr = "S" if cfg["allow_exec"] else "N"
    val = input(f"  Permitir ejecutar comandos? (S/N) [{curr}]: ").strip().upper()
    if val in ("S","N"): cfg["allow_exec"] = (val == "S")

    save_cfg()
    print()
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
        log(Y, "INFO ", "Aún no hay URL guardada. Usa Iniciar primero.")

def do_resecret():
    ans = input(f"  {Y}Esto invalida la URL actual. ¿Continuar? (S/N): {RST}").strip().upper()
    if ans != "S": return
    kill_all()
    for f in (SECRET_FILE, URL_FILE):
        if f.exists(): f.unlink()
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
        print(f"""
  Carpeta   : {W}{root_show}{RST}
  Puerto    : {cfg['port']}
  Borrar    : {del_show}      Comandos : {exec_show}
  Servidor  : {srv_s}      Túnel    : {tun_s}

  {G}[1]{RST} Iniciar servidor + túnel
  {G}[2]{RST} Configurar carpeta / opciones
  {G}[3]{RST} Ver URL del túnel
  {G}[4]{RST} Detener todo
  {G}[5]{RST} Regenerar secreto  (invalida URL actual)
  {G}[6]{RST} Salir
""")
        try:
            opt = input(f"  {W}Elige: {RST}").strip()
        except (KeyboardInterrupt, EOFError):
            kill_all(); sys.exit(0)

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
            kill_all(); sys.exit(0)
        else:
            log(Y, "INFO ", "Opción no válida.")
            time.sleep(0.8)

if __name__ == "__main__":
    try:
        menu()
    except KeyboardInterrupt:
        kill_all()
        sys.exit(0)
