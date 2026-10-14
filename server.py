"""
Servidor MCP JuanWorkspace (fusionado).

Da herramientas para:
  - (bajo nivel) listar/crear/renombrar/borrar proyectos, regenerar su API key,
    listar/crear/borrar colecciones, y CRUD de documentos en cualquier
    colección de cualquier proyecto de SmallDB
  - (admin ecosistema) registrar/quitar apps del ecosistema, ver cuentas
    registradas
  - (cuenta) iniciar sesión, listar apps, conectar/desconectar apps, leer el
    manifiesto de una app, y leer/crear/editar/borrar los datos de esa app
    que pertenecen a la cuenta logueada

  - (PC del usuario) pc_* : leer/editar archivos y ejecutar comandos en el PC de
    CADA usuario a través de SU túnel. La URL del túnel se guarda en los datos de
    la cuenta (app "pc-tunnel": la escribe el panel de JuanWorkspace o la tool
    set_tunnel_url) y se lee con el token OAuth de esa cuenta. Sin túnel, las tools
    devuelven una guía para que el agente de IA ayude al usuario a encenderlo.
    El servidor del PC vive en ./pc_server (se descarga desde /pc-server).
    Ver PANEL_TUNEL_PC.md para el panel.

Habla por HTTP con la API administrativa de SmallDB (endpoints /api/admin/...)
y con los endpoints /api/ecosystem/* del mismo backend, usando una clave
maestra (ADMIN_API_KEY) que NUNCA se expone al cliente MCP.

Variables de entorno requeridas:
  SMALLDB_BASE_URL   -> ej: https://juansmalldb.pythonanywhere.com
  SMALLDB_ADMIN_KEY  -> el mismo valor que ADMIN_API_KEY en SmallDB

Variables opcionales:
  TUNNEL_APP_SLUG / TUNNEL_PROJECT_NAME -> app y proyecto donde se guarda la URL del túnel
                         (por defecto "pc-tunnel" / "JuanWorkspace PC")
  PC_TUNNEL_ALLOW_PRIVATE=1 -> SOLO pruebas locales: permite túneles a localhost/http.
                         NUNCA lo definas en producción (es la protección anti-SSRF).
  MCP_SECRET         -> si se define, todas las peticiones a este servidor MCP
                         deben incluir el header  X-MCP-Secret: <valor>
                         NOTA: este mecanismo es EXCLUYENTE con el flujo OAuth
                         descrito abajo. Si vas a usar OAuth (recomendado),
                         deja MCP_SECRET sin definir: el middleware compara el
                         Bearer entrante contra MCP_SECRET literal, y con OAuth
                         ese Bearer es un access_token real emitido por SmallDB,
                         nunca coincidirá con MCP_SECRET.

--------------------------------------------------------------------------
OAuth para el conector de Claude
--------------------------------------------------------------------------
Este servidor NO implementa su propio Authorization Server: delega en el que
ya existe en el backend SmallDB (endpoints /oauth/authorize y /oauth/token,
ver app.py). Para que Claude (o cualquier cliente MCP compatible) descubra
automáticamente ese Authorization Server, este servidor publica el documento
de "OAuth Protected Resource Metadata" (RFC 9728) en:

  GET /.well-known/oauth-protected-resource

Pasos para dejarlo funcionando:
  1. Registra "Claude" como app del ecosistema en SmallDB (una sola vez),
     con redirect_uri = https://claude.ai/api/mcp/auth_callback.
     Esto te da un client_id y un client_secret.
  2. Despliega este server.py y el app.py parcheado (con soporte PKCE +
     metadata) en SmallDB.
  3. En Claude: Settings > Connectors > Add connector > Remote.
       URL: https://juanworkspace-mcp.onrender.com
       Advanced settings > OAuth Client ID / Secret: los del paso 1.

Ejecutar localmente:
  pip install -r requirements.txt
  python server.py
"""

import asyncio
import ipaddress
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
SMALLDB_BASE_URL = os.environ.get("SMALLDB_BASE_URL", "").rstrip("/")
SMALLDB_ADMIN_KEY = os.environ.get("SMALLDB_ADMIN_KEY", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
PORT = int(os.environ.get("PORT", "8000"))

PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "juanworkspace-mcp.onrender.com")
PUBLIC_BASE = f"https://{PUBLIC_HOST}"

# Túnel al PC del usuario (ver sección "Herramientas del PC" más abajo)
TUNNEL_APP_SLUG = os.environ.get("TUNNEL_APP_SLUG", "pc-tunnel")
TUNNEL_PROJECT_NAME = os.environ.get("TUNNEL_PROJECT_NAME", "JuanWorkspace PC")
# SOLO para pruebas locales: permite túneles hacia IPs privadas/localhost (anti-SSRF apagado)
ALLOW_PRIVATE_TUNNELS = os.environ.get("PC_TUNNEL_ALLOW_PRIVATE", "") == "1"

if not SMALLDB_BASE_URL or not SMALLDB_ADMIN_KEY:
    raise RuntimeError(
        "Faltan variables de entorno: SMALLDB_BASE_URL y SMALLDB_ADMIN_KEY son obligatorias."
    )

mcp = MCPServer(
    name="juanworkspace",
    instructions=(
        "Herramientas para administrar TODO el ecosistema JuanWorkspace: "
        "tanto la base de datos SmallDB de bajo nivel (proyectos, colecciones, "
        "documentos) como el ecosistema de cuentas y apps (registrar apps, "
        "login, conectar apps, y leer/crear/editar/borrar los datos de una "
        "app que pertenecen a la cuenta logueada). "
        "Las herramientas de cuenta (login, list_apps, get_app_data, etc.) "
        "requieren un access_token obtenido con login(); el resto usa la "
        "clave maestra del servidor, sin necesitar token. "
        "Además hay herramientas pc_* para leer/editar archivos y ejecutar comandos "
        "en el PC del usuario a través de SU túnel (cada cuenta guarda su propia URL). "
        "Si una herramienta pc_* responde con '[TÚNEL DEL PC NO DISPONIBLE]' o "
        "'[SESIÓN NECESARIA]', sigue las instrucciones que trae: guía al usuario con "
        "esos pasos y no inventes resultados."
    ),
)


class UpstreamError(RuntimeError):
    """Error HTTP devuelto por SmallDB (conserva el código de estado)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def _request(method: str, path: str, headers: dict, json: dict | None = None) -> Any:
    url = f"{SMALLDB_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.request(method, url, headers=headers, json=json)
    if res.status_code >= 400:
        try:
            detail = res.json().get("error", res.text)
        except Exception:
            detail = res.text
        raise UpstreamError(res.status_code, f"JuanWorkspace respondió {res.status_code}: {detail}")
    if res.status_code == 204 or not res.content:
        return {}
    return res.json()


def _admin_headers() -> dict:
    return {"X-Admin-Key": SMALLDB_ADMIN_KEY, "Content-Type": "application/json"}


def _account_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}


async def _admin_request(method: str, path: str, json: dict | None = None) -> Any:
    return await _request(method, path, _admin_headers(), json)


async def _account_request(method: str, path: str, access_token: str, json: dict | None = None) -> Any:
    return await _request(method, path, _account_headers(access_token), json)


# ---------------------------------------------------------------------------
# Herramientas de SmallDB de bajo nivel (proyectos, colecciones, documentos)
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_projects() -> list[dict]:
    """Lista todos los proyectos (apps) que existen en SmallDB, con su id, nombre y api_key."""
    data = await _admin_request("GET", "/api/admin/projects")
    return data["projects"]


@mcp.tool()
async def create_project(name: str) -> dict:
    """Crea un nuevo proyecto (app) en SmallDB y devuelve su id y su api_key recién generada.

    Args:
        name: Nombre del proyecto, por ejemplo 'Cronómetro Pro Max' o 'mi-app-flutter'.
    """
    return await _admin_request("POST", "/api/admin/projects", json={"name": name})


@mcp.tool()
async def get_project(project_id: int) -> dict:
    """Obtiene el detalle de un proyecto (nombre, api_key, fecha) junto con sus colecciones.

    Args:
        project_id: El id numérico del proyecto (lo devuelve list_projects / create_project).
    """
    return await _admin_request("GET", f"/api/admin/projects/{project_id}")


@mcp.tool()
async def rename_project(project_id: int, new_name: str) -> dict:
    """Cambia el nombre de un proyecto existente.

    Args:
        project_id: id del proyecto a renombrar.
        new_name: nuevo nombre para el proyecto.
    """
    return await _admin_request("PATCH", f"/api/admin/projects/{project_id}", json={"name": new_name})


@mcp.tool()
async def delete_project(project_id: int) -> dict:
    """Elimina un proyecto por completo, incluyendo todas sus colecciones y documentos.
    Esta acción no se puede deshacer, úsala solo si el usuario confirma explícitamente.

    Args:
        project_id: id del proyecto a eliminar.
    """
    return await _admin_request("DELETE", f"/api/admin/projects/{project_id}")


@mcp.tool()
async def regenerate_api_key(project_id: int) -> dict:
    """Invalida la API key actual de un proyecto y genera una nueva.
    Cualquier app que use la key anterior dejará de poder conectarse hasta
    que se actualice con la nueva key devuelta aquí.

    Args:
        project_id: id del proyecto.
    """
    return await _admin_request("POST", f"/api/admin/projects/{project_id}/regenerate_key")


@mcp.tool()
async def list_collections(project_id: int) -> list[dict]:
    """Lista las colecciones ('tablas') de un proyecto, con cuántos documentos tiene cada una.

    Args:
        project_id: id del proyecto.
    """
    data = await _admin_request("GET", f"/api/admin/projects/{project_id}/collections")
    return data["collections"]


@mcp.tool()
async def create_collection(project_id: int, name: str) -> dict:
    """Crea una colección nueva y vacía dentro de un proyecto.
    (No es obligatorio: las colecciones también se crean solas al guardar el
    primer documento vía la API pública. Usa esto solo si el usuario quiere
    dejarla preparada de antemano.)

    Args:
        project_id: id del proyecto.
        name: nombre de la colección, por ejemplo 'usuarios' o 'historial'.
    """
    return await _admin_request("POST", f"/api/admin/projects/{project_id}/collections", json={"name": name})


@mcp.tool()
async def delete_collection(project_id: int, collection_id: int) -> dict:
    """Elimina una colección y todos sus documentos. No se puede deshacer.

    Args:
        project_id: id del proyecto dueño de la colección.
        collection_id: id de la colección a eliminar.
    """
    return await _admin_request("DELETE", f"/api/admin/projects/{project_id}/collections/{collection_id}")


@mcp.tool()
async def list_documents(project_id: int, collection_id: int) -> list[dict]:
    """Lista todos los documentos de una colección, con su _id, contenido y fechas.

    Args:
        project_id: id del proyecto.
        collection_id: id de la colección.
    """
    data = await _admin_request(
        "GET", f"/api/admin/projects/{project_id}/collections/{collection_id}/documents"
    )
    return data["documents"]


@mcp.tool()
async def get_document(project_id: int, collection_id: int, doc_id: str) -> dict:
    """Obtiene un documento individual por su _id.

    Args:
        project_id: id del proyecto.
        collection_id: id de la colección.
        doc_id: el _id del documento (lo devuelve list_documents / create_document).
    """
    return await _admin_request(
        "GET", f"/api/admin/projects/{project_id}/collections/{collection_id}/documents/{doc_id}"
    )


@mcp.tool()
async def create_document(project_id: int, collection_id: int, data: dict) -> dict:
    """Crea un documento nuevo dentro de una colección.

    Args:
        project_id: id del proyecto.
        collection_id: id de la colección.
        data: diccionario JSON con los campos del documento. Puedes incluir
            la clave "_id" para forzar un identificador específico; si no,
            se genera uno automáticamente.
    """
    return await _admin_request(
        "POST", f"/api/admin/projects/{project_id}/collections/{collection_id}/documents", json=data
    )


@mcp.tool()
async def update_document(
    project_id: int, collection_id: int, doc_id: str, data: dict, replace: bool = False
) -> dict:
    """Edita un documento existente dentro de una colección.

    Args:
        project_id: id del proyecto.
        collection_id: id de la colección.
        doc_id: el _id del documento a editar.
        data: campos a actualizar. Por defecto solo se combinan (patch parcial)
            con los campos existentes del documento.
        replace: si es True, reemplaza el documento completo con `data` en vez
            de solo actualizar los campos dados.
    """
    method = "PUT" if replace else "PATCH"
    return await _admin_request(
        method,
        f"/api/admin/projects/{project_id}/collections/{collection_id}/documents/{doc_id}",
        json=data,
    )


@mcp.tool()
async def delete_document(project_id: int, collection_id: int, doc_id: str) -> dict:
    """Elimina un documento individual de una colección. No se puede deshacer.

    Args:
        project_id: id del proyecto.
        collection_id: id de la colección.
        doc_id: el _id del documento a eliminar.
    """
    return await _admin_request(
        "DELETE", f"/api/admin/projects/{project_id}/collections/{collection_id}/documents/{doc_id}"
    )


# ---------------------------------------------------------------------------
# Herramientas de ADMINISTRACIÓN (registrar apps, ver cuentas)
# ---------------------------------------------------------------------------
@mcp.tool()
async def register_app(
    slug: str,
    name: str,
    project_id: int,
    data_collection: str = "user_data",
    account_field: str = "account_id",
    icon: str = "🧩",
    color: str = "#3B82F6",
    description: str = "",
    manifest_collection: str = "_ecosystem_manifest",
) -> dict:
    """Registra una app nueva en el ecosistema JuanWorkspace. Devuelve el
    client_id y un client_secret (solo se muestra una vez: úsalo para OAuth2
    si esa app va a permitir "iniciar sesión con JuanWorkspace").

    La app ya debe tener su proyecto creado en SmallDB (project_id) y —
    idealmente — ya debe haber publicado su documento de manifiesto en la
    colección `manifest_collection` de ese proyecto.

    Args:
        slug: identificador corto único para la app, ej. 'cronometro-pro-max'.
        name: nombre visible de la app, ej. 'Cronómetro Pro Max'.
        project_id: id del proyecto SmallDB que contiene los datos de esa app.
        data_collection: colección dentro de ese proyecto donde viven los
            documentos de usuario (uno o más por cuenta). Por defecto 'user_data'.
        account_field: campo dentro de cada documento que guarda el id de la
            cuenta JuanWorkspace dueña de ese documento. Por defecto 'account_id'.
        icon: emoji o símbolo corto para mostrar en la interfaz.
        color: color de acento en hex, ej. '#3B82F6'.
        description: descripción corta para mostrar en el listado de apps.
        manifest_collection: colección donde esa app publica su documento de
            manifiesto (botones/controles). Por defecto '_ecosystem_manifest'.
    """
    return await _admin_request("POST", "/api/admin/ecosystem/apps", json={
        "slug": slug, "name": name, "project_id": project_id,
        "data_collection": data_collection, "account_field": account_field,
        "icon": icon, "color": color, "description": description,
        "manifest_collection": manifest_collection,
    })


@mcp.tool()
async def list_registered_apps() -> list[dict]:
    """Lista todas las apps registradas en el ecosistema JuanWorkspace (vista admin,
    sin exponer client secrets)."""
    data = await _admin_request("GET", "/api/admin/ecosystem/apps")
    return data["apps"]


@mcp.tool()
async def unregister_app(slug: str) -> dict:
    """Quita una app del ecosistema JuanWorkspace. No borra sus datos en SmallDB,
    solo deja de mostrarla/permitir conexión desde JuanWorkspace.

    Args:
        slug: identificador de la app a quitar.
    """
    return await _admin_request("DELETE", f"/api/admin/ecosystem/apps/{slug}")


@mcp.tool()
async def list_ecosystem_accounts() -> list[dict]:
    """Lista las cuentas registradas en JuanWorkspace (sin contraseñas)."""
    data = await _admin_request("GET", "/api/admin/ecosystem/accounts")
    return data["accounts"]


# ---------------------------------------------------------------------------
# Herramientas de CUENTA (requieren access_token, obtenido con login)
# ---------------------------------------------------------------------------
@mcp.tool()
async def login(username: str, password: str) -> dict:
    """Inicia sesión con una cuenta de JuanWorkspace y devuelve un access_token.
    Usa ese access_token en el resto de herramientas de esta sección.

    Args:
        username: nombre de usuario de la cuenta JuanWorkspace.
        password: contraseña de la cuenta.
    """
    return await _request(
        "POST", "/api/ecosystem/login", {"Content-Type": "application/json"},
        json={"username": username, "password": password},
    )


@mcp.tool()
async def register_account(username: str, password: str, display_name: str | None = None) -> dict:
    """Crea una cuenta nueva de JuanWorkspace.

    Args:
        username: nombre de usuario deseado (único).
        password: contraseña (mínimo 6 caracteres).
        display_name: nombre para mostrar; si no se da, se usa el username.
    """
    payload = {"username": username, "password": password}
    if display_name:
        payload["display_name"] = display_name
    return await _request(
        "POST", "/api/ecosystem/register", {"Content-Type": "application/json"}, json=payload
    )


@mcp.tool()
async def list_apps(access_token: str) -> list[dict]:
    """Lista todas las apps del ecosistema y si la cuenta logueada está
    conectada a cada una.

    Args:
        access_token: token obtenido con login().
    """
    data = await _account_request("GET", "/api/ecosystem/apps", access_token)
    return data["apps"]


@mcp.tool()
async def connect_app(access_token: str, slug: str) -> dict:
    """Conecta la cuenta logueada a una app del ecosistema (necesario antes
    de poder leer o editar sus datos).

    Args:
        access_token: token obtenido con login().
        slug: identificador de la app (lo devuelve list_apps).
    """
    return await _account_request("POST", f"/api/ecosystem/apps/{slug}/connect", access_token)


@mcp.tool()
async def disconnect_app(access_token: str, slug: str) -> dict:
    """Desconecta la cuenta logueada de una app del ecosistema.

    Args:
        access_token: token obtenido con login().
        slug: identificador de la app.
    """
    return await _account_request("DELETE", f"/api/ecosystem/apps/{slug}/connect", access_token)


@mcp.tool()
async def get_app_manifest(access_token: str, slug: str) -> dict:
    """Obtiene el manifiesto (botones y controles) que esa app publicó para
    que JuanWorkspace lo reconozca. Requiere estar conectado a la app.

    Args:
        access_token: token obtenido con login().
        slug: identificador de la app.
    """
    return await _account_request("GET", f"/api/ecosystem/apps/{slug}/manifest", access_token)


@mcp.tool()
async def get_app_data(access_token: str, slug: str) -> list[dict]:
    """Lista los documentos de datos de una app que pertenecen a la cuenta
    logueada (nunca datos de otras cuentas). Requiere estar conectado a la app.

    Args:
        access_token: token obtenido con login().
        slug: identificador de la app.
    """
    data = await _account_request("GET", f"/api/ecosystem/apps/{slug}/data", access_token)
    return data["data"]


@mcp.tool()
async def create_app_data(access_token: str, slug: str, data: dict) -> dict:
    """Crea un documento de datos nuevo para esa app, asociado automáticamente
    a la cuenta logueada (no se puede crear a nombre de otra cuenta).

    Args:
        access_token: token obtenido con login().
        slug: identificador de la app.
        data: campos del documento a crear.
    """
    return await _account_request("POST", f"/api/ecosystem/apps/{slug}/data", access_token, json=data)


@mcp.tool()
async def edit_app_data(access_token: str, slug: str, doc_id: str, data: dict) -> dict:
    """Edita (patch parcial) un documento de datos de esa app. Solo funciona
    si el documento pertenece a la cuenta logueada; si pertenece a otra
    cuenta, se rechaza.

    Args:
        access_token: token obtenido con login().
        slug: identificador de la app.
        doc_id: _id del documento (lo devuelve get_app_data / create_app_data).
        data: campos a actualizar.
    """
    return await _account_request(
        "PATCH", f"/api/ecosystem/apps/{slug}/data/{doc_id}", access_token, json=data
    )


@mcp.tool()
async def delete_app_data(access_token: str, slug: str, doc_id: str) -> dict:
    """Elimina un documento de datos de esa app. Solo funciona si pertenece
    a la cuenta logueada.

    Args:
        access_token: token obtenido con login().
        slug: identificador de la app.
        doc_id: _id del documento a eliminar.
    """
    return await _account_request("DELETE", f"/api/ecosystem/apps/{slug}/data/{doc_id}", access_token)


# ---------------------------------------------------------------------------
# Herramientas del PC: archivos y comandos a través del túnel de CADA usuario
# ---------------------------------------------------------------------------
# Arquitectura:
#   Claude --OAuth--> este MCP --HTTPS--> túnel del usuario --> mcp_archivos.py (su PC)
#
# La URL del túnel NO vive en este servidor ni en variables de entorno: se guarda
# en los datos de la propia cuenta del usuario, dentro de una app del ecosistema
# (slug TUNNEL_APP_SLUG, por defecto "pc-tunnel"), en un documento
#   {"kind": "tunnel", "tunnel_url": "https://....trycloudflare.com/<secreto>/mcp"}
# El panel de JuanWorkspace y la herramienta set_tunnel_url escriben ese mismo
# documento. Así cada cuenta usa SU túnel y nunca el de otra.
#
# Si no hay URL o el túnel no responde, las herramientas NO fallan con un error
# críptico: devuelven una guía escrita para el agente de IA, que se la explica al
# usuario paso a paso.
class _Guide(Exception):
    """No es un fallo: es una respuesta guiada que se devuelve tal cual al agente."""

    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


_tunnel_app_lock = asyncio.Lock()
_tunnel_app_ready = False


def _setup_steps() -> str:
    return (
        "PASOS PARA EL USUARIO (Windows; Mac/Linux al final):\n"
        "1. Descargar estos dos archivos y guardarlos JUNTOS en una carpeta propia "
        "(NO dentro de la carpeta que va a compartir):\n"
        f"   - {PUBLIC_BASE}/pc-server/iniciar_mcp.bat\n"
        f"   - {PUBLIC_BASE}/pc-server/mcp_archivos.py\n"
        "2. Tener Python 3 instalado (python.org, marcando 'Add to PATH') y cloudflared: bajar "
        "cloudflared-windows-amd64.exe de https://github.com/cloudflare/cloudflared/releases/latest, "
        "renombrarlo EXACTAMENTE a cloudflared.exe y ponerlo junto a iniciar_mcp.bat.\n"
        "3. Doble clic en iniciar_mcp.bat. La primera vez pregunta: carpeta a compartir, puerto (Enter), "
        "si Claude puede BORRAR archivos y si puede EJECUTAR COMANDOS (S/N). Después elegir la opción 1.\n"
        "4. El .bat muestra (y copia al portapapeles) una URL como "
        "https://algo.trycloudflare.com/<secreto>/mcp . El usuario la pega en el panel de JuanWorkspace "
        "(sección 'Túnel de mi PC') y guarda; o te la da a ti para que llames set_tunnel_url. "
        "Debe tratarla como una contraseña: quien la tenga controla esa carpeta.\n"
        "5. Dejar abiertas las ventanas del servidor y del túnel. El túnel gratuito cambia de URL cada vez "
        "que se reinicia: si pasa, repetir el paso 4.\n"
        "Mac/Linux: ejecutar `python mcp_archivos.py --root <carpeta> [--allow-delete] [--allow-exec]` y, en otra "
        "terminal, `cloudflared tunnel --url http://localhost:8765`; la URL es la del túnel + /<secreto>/mcp "
        "(el script imprime el secreto).\n"
        f"Guía completa en texto: {PUBLIC_BASE}/pc-server\n"
    )


def _agent_guide(headline: str, detail: str = "") -> str:
    return (
        f"[TÚNEL DEL PC NO DISPONIBLE] {headline}\n"
        + (f"(detalle técnico: {detail})\n" if detail else "")
        + "\nINSTRUCCIONES PARA TI (agente de IA): no pudiste acceder al PC del usuario, así que NO inventes "
        "contenido de archivos ni des por hecha la operación. Explícale al usuario, en su idioma y con tus "
        "palabras, qué está pasando y guíalo paso a paso con los pasos de abajo (salta los que ya tenga "
        "hechos). Cuando confirme que el túnel está encendido y que guardó la URL, reintenta la operación o "
        "llama pc_tunnel_status. No reintentes en bucle mientras no haya túnel.\n\n"
        + _setup_steps()
    )


def _guide_feature_disabled(message: str) -> str:
    return (
        f"[FUNCIÓN DESACTIVADA EN EL PC] {message}\n\n"
        "INSTRUCCIONES PARA TI (agente de IA): el túnel funciona, pero el usuario no activó esta función al "
        "iniciar el servidor. Díselo y, si quiere usarla, guíalo: en iniciar_mcp.bat elegir opción 4 (detener), "
        "opción 2 (configurar) y responder S a la pregunta correspondiente (borrar / ejecutar comandos), luego "
        "opción 1 (iniciar). Como el túnel gratuito se reinicia, la URL cambiará: debe guardar la nueva en el panel "
        "de JuanWorkspace o dártela para set_tunnel_url. Avísale que ejecutar comandos da control total sobre su PC "
        "(con sus permisos de usuario), no solo sobre la carpeta compartida."
    )


_NEED_LOGIN = (
    "[SESIÓN NECESARIA] No se pudo identificar la cuenta de JuanWorkspace.\n\n"
    "INSTRUCCIONES PARA TI (agente de IA): con el conector OAuth el token llega solo; si no llegó (o es "
    "inválido/expirado), pídele al usuario su usuario y contraseña de JuanWorkspace, llama login() y pasa el "
    "access_token que devuelve en el parámetro access_token de esta herramienta. Nunca guardes ni repitas la "
    "contraseña."
)


def _bearer(ctx: Context | None) -> str:
    headers = getattr(ctx, "headers", None) or {}
    auth = headers.get("authorization", "") if hasattr(headers, "get") else ""
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _token_for(ctx: Context | None, access_token: str | None) -> str:
    token = (access_token or "").strip() or _bearer(ctx)
    if not token:
        raise _Guide(_NEED_LOGIN)
    return token


async def _ensure_tunnel_app() -> None:
    """Registra (una sola vez, idempotente) la app del ecosistema que guarda las URLs de túnel."""
    global _tunnel_app_ready
    if _tunnel_app_ready:
        return
    async with _tunnel_app_lock:
        if _tunnel_app_ready:
            return
        apps = (await _admin_request("GET", "/api/admin/ecosystem/apps")).get("apps", [])
        if not any(a.get("slug") == TUNNEL_APP_SLUG for a in apps):
            projects = (await _admin_request("GET", "/api/admin/projects")).get("projects", [])
            project = next((p for p in projects if p.get("name") == TUNNEL_PROJECT_NAME), None)
            if project is None:
                project = await _admin_request("POST", "/api/admin/projects", json={"name": TUNNEL_PROJECT_NAME})
            project_id = (
                project.get("id") or project.get("project_id") or (project.get("project") or {}).get("id")
            )
            if project_id is None:
                raise RuntimeError("No se pudo obtener el id del proyecto del túnel en SmallDB.")
            await _admin_request("POST", "/api/admin/ecosystem/apps", json={
                "slug": TUNNEL_APP_SLUG,
                "name": "Túnel de mi PC",
                "project_id": project_id,
                "data_collection": "user_data",
                "account_field": "account_id",
                "icon": "🖥️",
                "color": "#3B82F6",
                "description": "URL del túnel hacia tu PC, para que Claude lea/edite archivos y ejecute comandos.",
                "manifest_collection": "_ecosystem_manifest",
            })
        _tunnel_app_ready = True


async def _load_tunnel_doc(token: str) -> dict | None:
    """Devuelve el documento del túnel de ESTA cuenta (o None). Se conecta a la app si hace falta."""
    await _ensure_tunnel_app()
    path = f"/api/ecosystem/apps/{TUNNEL_APP_SLUG}/data"
    data = None
    for attempt in (1, 2):
        try:
            data = await _account_request("GET", path, token)
            break
        except UpstreamError as exc:
            if exc.status == 401:
                raise _Guide(_NEED_LOGIN)
            if attempt == 2:
                raise
            # probablemente la cuenta aún no está conectada a la app del túnel
            try:
                await _account_request("POST", f"/api/ecosystem/apps/{TUNNEL_APP_SLUG}/connect", token)
            except UpstreamError as exc2:
                if exc2.status == 401:
                    raise _Guide(_NEED_LOGIN)
    for doc in (data or {}).get("data", []):
        if doc.get("kind") == "tunnel":
            return doc
    return None


async def _save_tunnel_doc(token: str, url: str) -> None:
    doc = await _load_tunnel_doc(token)
    payload = {"kind": "tunnel", "tunnel_url": url, "updated_at": datetime.now(timezone.utc).isoformat()}
    base = f"/api/ecosystem/apps/{TUNNEL_APP_SLUG}/data"
    if doc and doc.get("_id"):
        await _account_request("PATCH", f"{base}/{doc['_id']}", token, json=payload)
    else:
        await _account_request("POST", base, token, json=payload)


def _normalize_tunnel_url(raw: str) -> str:
    """Acepta la URL tal como la muestra el .bat (…/<secreto>/mcp) y devuelve la base (…/<secreto>)."""
    raw = (raw or "").strip().strip("\"'<>")
    if not raw:
        raise ValueError("la URL está vacía")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme != "https" and not (ALLOW_PRIVATE_TUNNELS and parts.scheme == "http"):
        raise ValueError("debe empezar por https:// (el túnel de Cloudflare ya es https)")
    if parts.username or parts.password:
        raise ValueError("no debe llevar usuario/contraseña")
    if not parts.hostname:
        raise ValueError("falta el dominio")
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/")
    if path.endswith("/mcp"):
        path = path[:-4]
    path = path.rstrip("/")
    if not path.strip("/"):
        raise ValueError(
            "falta el secreto al final de la URL; copia la URL COMPLETA que muestra iniciar_mcp.bat "
            "(https://…trycloudflare.com/<secreto>/mcp)"
        )
    return f"{parts.scheme}://{parts.hostname}{port}{path}"


def _mask(base: str) -> str:
    parts = urlsplit(base)
    return f"{parts.scheme}://{parts.netloc}/<secreto oculto>"


async def _assert_public_host(host: str) -> None:
    """Anti-SSRF: este servidor solo habla con direcciones públicas, nunca con la red interna."""
    if ALLOW_PRIVATE_TUNNELS:
        return
    try:
        literal = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        literal = None
    if literal is not None:
        ips = [literal]
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ConnectionError(f"el dominio {host} no existe o no resuelve (¿túnel cerrado?)") from exc
        ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if not ips or not all(ip.is_global for ip in ips):
        raise PermissionError("la URL apunta a una dirección privada/local; solo se permiten túneles públicos")


async def _stored_base(token: str) -> str:
    doc = await _load_tunnel_doc(token)
    raw = (doc or {}).get("tunnel_url", "")
    if not raw:
        raise _Guide(_agent_guide("El usuario aún no ha guardado la URL de su túnel."))
    try:
        return _normalize_tunnel_url(raw)
    except ValueError as exc:
        raise _Guide(_agent_guide(f"La URL guardada no es válida: {exc}."))


async def _call_tunnel(base: str, method: str, tool: str, args: dict | None, timeout: float) -> Any:
    """Llama a la API del PC. Devuelve el 'result' o lanza _Guide / RuntimeError."""
    host = urlsplit(base).hostname or ""
    try:
        await _assert_public_host(host)
    except PermissionError as exc:
        raise _Guide(_agent_guide(f"La URL guardada fue rechazada por seguridad: {exc}."))
    except ConnectionError as exc:
        raise _Guide(_agent_guide(f"Hay una URL guardada ({_mask(base)}) pero no responde: {exc}."))

    url = f"{base}/api/{tool}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10), follow_redirects=False) as client:
            res = await client.request(method, url, json=args if method == "POST" else None)
    except httpx.ReadTimeout:
        raise RuntimeError(
            "El PC no respondió a tiempo. Puede estar ocupado o la operación tardó demasiado; "
            "si era un comando largo, usa pc_run_command con background=true."
        )
    except httpx.HTTPError as exc:
        raise _Guide(_agent_guide(
            f"Hay una URL guardada ({_mask(base)}) pero no responde: el túnel o el servidor del PC están "
            "apagados, el PC está suspendido o sin internet, o la URL cambió (los túneles gratuitos cambian "
            "de URL cada vez que se reinician).",
            type(exc).__name__,
        ))

    try:
        body = res.json()
        is_ours = isinstance(body, dict) and "ok" in body
    except ValueError:
        body, is_ours = None, False

    if not is_ours:
        if res.status_code == 404:
            raise _Guide(_agent_guide(
                f"La URL guardada ({_mask(base)}) responde, pero el secreto ya no corresponde: probablemente "
                "el usuario regeneró el secreto o la URL es de otro túnel/servidor.",
                "HTTP 404",
            ))
        raise _Guide(_agent_guide(
            f"Hay una URL guardada ({_mask(base)}) pero el túnel no está entregando el servidor del PC: está "
            "apagado, se reinició con otra URL, o el servidor local (iniciar_mcp.bat) no está corriendo.",
            f"HTTP {res.status_code}",
        ))

    if body.get("ok"):
        return body.get("result", body)
    if body.get("error") == "tool_disabled":
        raise _Guide(_guide_feature_disabled(body.get("message", "función desactivada")))
    raise RuntimeError(f"{body.get('error', 'error')}: {body.get('message', '')}".strip(": "))


async def _pc(ctx: Context | None, access_token: str | None, tool: str, args: dict, timeout: float = 45.0) -> str:
    try:
        token = _token_for(ctx, access_token)
        base = await _stored_base(token)
        result = await _call_tunnel(base, "POST", tool, args, timeout)
    except _Guide as guide:
        return guide.text
    except (RuntimeError, UpstreamError) as exc:
        # Error "normal" de la operación (ruta inválida, archivo inexistente, old_str no encontrado,
        # tiempo agotado...). Se devuelve como texto: el SDK oculta el mensaje de las excepciones
        # genéricas y el agente necesita leerlo para corregir su siguiente intento.
        return f"[ERROR DE LA OPERACIÓN EN EL PC] {exc}"
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)


# --- configuración del túnel ------------------------------------------------
@mcp.tool()
async def setup_pc_tunnel_app() -> dict:
    """(Admin, una sola vez) Registra en el ecosistema la app 'pc-tunnel' donde cada cuenta
    guarda la URL de su túnel, para que aparezca en el panel de JuanWorkspace. Es idempotente
    y las herramientas pc_* ya lo hacen solas la primera vez; úsala si quieres que la app
    exista antes de que nadie use el túnel."""
    await _ensure_tunnel_app()
    return {"ok": True, "slug": TUNNEL_APP_SLUG, "project": TUNNEL_PROJECT_NAME}


@mcp.tool()
async def set_tunnel_url(url: str, ctx: Context, access_token: str | None = None) -> str:
    """Guarda la URL del túnel del PC para la cuenta actual (es lo mismo que hace el panel de
    JuanWorkspace). Pega la URL COMPLETA que muestra iniciar_mcp.bat, p. ej.
    https://algo.trycloudflare.com/<secreto>/mcp . Después comprueba si responde.

    Args:
        url: URL completa del túnel (incluye el secreto). Trátala como una contraseña.
        access_token: opcional; con el conector OAuth se toma solo.
    """
    try:
        token = _token_for(ctx, access_token)
        try:
            base = _normalize_tunnel_url(url)
        except ValueError as exc:
            return f"URL no válida: {exc}."
        try:
            await _assert_public_host(urlsplit(base).hostname or "")
        except PermissionError as exc:
            return f"URL rechazada por seguridad: {exc}."
        except ConnectionError:
            pass  # túnel apagado ahora mismo: se guarda igual y se avisa abajo
        await _save_tunnel_doc(token, url.strip())
        try:
            info = await _call_tunnel(base, "GET", "ping", None, 15)
        except _Guide:
            return (
                f"URL guardada ({_mask(base)}), pero ahora mismo NO responde. Verifica que iniciar_mcp.bat "
                "esté corriendo (servidor + túnel abiertos) y que sea la URL actual; cuando esté listo, "
                "llama pc_tunnel_status."
            )
        return f"URL guardada y el PC responde. {_describe_ping(info)}"
    except _Guide as guide:
        return guide.text


@mcp.tool()
async def clear_tunnel_url(ctx: Context, access_token: str | None = None) -> str:
    """Borra la URL del túnel guardada para la cuenta actual (deja de haber acceso al PC).

    Args:
        access_token: opcional; con el conector OAuth se toma solo.
    """
    try:
        token = _token_for(ctx, access_token)
        doc = await _load_tunnel_doc(token)
        if not doc or not doc.get("_id"):
            return "No había ninguna URL de túnel guardada."
        await _account_request("DELETE", f"/api/ecosystem/apps/{TUNNEL_APP_SLUG}/data/{doc['_id']}", token)
        return "URL del túnel borrada."
    except _Guide as guide:
        return guide.text


def _describe_ping(info: Any) -> str:
    if not isinstance(info, dict):
        return ""
    return (
        f"Carpeta compartida: '{info.get('root_name', '?')}' · versión {info.get('version', '?')} · "
        f"borrar: {'sí' if info.get('allow_delete') else 'no'} · "
        f"ejecutar comandos: {'sí' if info.get('allow_exec') else 'no'}."
    )


@mcp.tool()
async def pc_tunnel_status(ctx: Context, access_token: str | None = None) -> str:
    """Comprueba si el túnel del PC del usuario está encendido y qué permite (carpeta compartida,
    si puede borrar, si puede ejecutar comandos). Úsala antes de trabajar con archivos del PC o
    cuando una herramienta pc_* falle. Si no hay túnel, devuelve la guía para ayudar al usuario.

    Args:
        access_token: opcional; con el conector OAuth se toma solo.
    """
    try:
        token = _token_for(ctx, access_token)
        base = await _stored_base(token)
        info = await _call_tunnel(base, "GET", "ping", None, 15)
    except _Guide as guide:
        return guide.text
    return f"Túnel activo ({_mask(base)}). {_describe_ping(info)}"


# --- archivos del PC --------------------------------------------------------
_PC_NOTE = "Opera en la carpeta compartida del PC del usuario (vía su túnel); rutas relativas a esa carpeta."


@mcp.tool()
async def pc_list_directory(ctx: Context, path: str = ".", recursive: bool = False,
                            max_entries: int = 500, access_token: str | None = None) -> str:
    """Lista archivos y carpetas del PC del usuario (las carpetas terminan en '/').
    Opera en la carpeta compartida del PC del usuario (vía su túnel); rutas relativas a esa carpeta.
    Si no hay túnel, devuelve una guía para el usuario."""
    return await _pc(ctx, access_token, "list_directory",
                     {"path": path, "recursive": recursive, "max_entries": max_entries})


@mcp.tool()
async def pc_read_file(path: str, ctx: Context, start_line: int = 1, end_line: int | None = None,
                       access_token: str | None = None) -> str:
    """Lee un archivo de texto del PC del usuario, con números de línea; admite rango de líneas.
    Opera en la carpeta compartida del PC del usuario (vía su túnel); rutas relativas a esa carpeta."""
    return await _pc(ctx, access_token, "read_file",
                     {"path": path, "start_line": start_line, "end_line": end_line})


@mcp.tool()
async def pc_write_file(path: str, content: str, ctx: Context, access_token: str | None = None) -> str:
    """Crea o sobrescribe un archivo de texto completo en el PC del usuario (crea las carpetas).
    Para cambios pequeños prefiere pc_edit_file. Opera en la carpeta compartida del PC (vía su túnel)."""
    return await _pc(ctx, access_token, "write_file", {"path": path, "content": content})


@mcp.tool()
async def pc_edit_file(path: str, old_str: str, new_str: str, ctx: Context, replace_all: bool = False,
                       access_token: str | None = None) -> str:
    """Reemplaza texto exacto en un archivo del PC del usuario. old_str debe aparecer una sola vez
    salvo replace_all=True. Opera en la carpeta compartida del PC (vía su túnel)."""
    return await _pc(ctx, access_token, "edit_file",
                     {"path": path, "old_str": old_str, "new_str": new_str, "replace_all": replace_all})


@mcp.tool()
async def pc_append_file(path: str, content: str, ctx: Context, access_token: str | None = None) -> str:
    """Agrega texto al final de un archivo del PC del usuario (lo crea si no existe)."""
    return await _pc(ctx, access_token, "append_file", {"path": path, "content": content})


@mcp.tool()
async def pc_create_directory(path: str, ctx: Context, access_token: str | None = None) -> str:
    """Crea una carpeta (y las intermedias) en el PC del usuario."""
    return await _pc(ctx, access_token, "create_directory", {"path": path})


@mcp.tool()
async def pc_move_path(source: str, destination: str, ctx: Context, access_token: str | None = None) -> str:
    """Mueve o renombra un archivo/carpeta dentro de la carpeta compartida del PC del usuario."""
    return await _pc(ctx, access_token, "move_path", {"source": source, "destination": destination})


@mcp.tool()
async def pc_search_files(pattern: str, ctx: Context, path: str = ".", access_token: str | None = None) -> str:
    """Busca archivos por nombre con patrón glob (ej. '*.py') en el PC del usuario."""
    return await _pc(ctx, access_token, "search_files", {"pattern": pattern, "path": path})


@mcp.tool()
async def pc_grep(query: str, ctx: Context, path: str = ".", file_glob: str = "*", regex: bool = False,
                  ignore_case: bool = True, max_results: int = 100, access_token: str | None = None) -> str:
    """Busca texto dentro de los archivos del PC del usuario. Devuelve 'ruta:línea: contenido'."""
    return await _pc(ctx, access_token, "grep", {
        "query": query, "path": path, "file_glob": file_glob, "regex": regex,
        "ignore_case": ignore_case, "max_results": max_results})


@mcp.tool()
async def pc_delete_path(path: str, ctx: Context, access_token: str | None = None) -> str:
    """Borra un archivo o carpeta (con todo su contenido) del PC del usuario. IRREVERSIBLE: pide
    confirmación explícita al usuario antes. Solo funciona si el usuario activó el borrado al iniciar
    el servidor; si no, devuelve cómo activarlo."""
    return await _pc(ctx, access_token, "delete_path", {"path": path})


# --- comandos en el PC ------------------------------------------------------
@mcp.tool()
async def pc_run_command(command: str, ctx: Context, cwd: str = ".", timeout: int = 60,
                         background: bool = False, access_token: str | None = None) -> str:
    """Ejecuta un comando de shell en el PC del usuario (cmd.exe en Windows) y devuelve código de
    salida + salida. No es interactivo. 'cwd' es relativo a la carpeta compartida. En primer plano
    espera hasta 'timeout' s (máx. 90); para tareas largas usa background=true y luego pc_job_status.

    CUIDADO: corre con los permisos del usuario y puede afectar CUALQUIER parte de su PC, no solo la
    carpeta compartida. Explica qué vas a ejecutar y pide confirmación antes de comandos destructivos
    (borrar, formatear, instalar, tocar el registro, etc.). Solo funciona si el usuario activó los
    comandos al iniciar el servidor; si no, devuelve cómo activarlos."""
    timeout = max(1, min(int(timeout), 90))
    return await _pc(ctx, access_token, "run_command",
                     {"command": command, "cwd": cwd, "timeout": timeout, "background": background},
                     timeout=timeout + 20)


@mcp.tool()
async def pc_job_status(job_id: str, ctx: Context, tail_lines: int = 100, access_token: str | None = None) -> str:
    """Estado y últimas líneas de salida de un comando lanzado con pc_run_command(background=true)."""
    return await _pc(ctx, access_token, "job_status", {"job_id": job_id, "tail_lines": tail_lines})


@mcp.tool()
async def pc_stop_job(job_id: str, ctx: Context, access_token: str | None = None) -> str:
    """Detiene (con todo su árbol de procesos) un comando en segundo plano del PC del usuario."""
    return await _pc(ctx, access_token, "stop_job", {"job_id": job_id})


# ---------------------------------------------------------------------------
# App ASGI (Starlette) + protección de transporte + protección opcional con MCP_SECRET
# ---------------------------------------------------------------------------
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[PUBLIC_HOST, f"{PUBLIC_HOST}:*", "localhost:*", "127.0.0.1:*"],
    allowed_origins=[f"https://{PUBLIC_HOST}"],
)

app = mcp.streamable_http_app(transport_security=_transport_security)


class SecretHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if MCP_SECRET:
            auth_header = request.headers.get("authorization", "")
            token_from_header = (
                auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
            )
            token_from_query = request.query_params.get("secret", "")
            if MCP_SECRET not in (token_from_header, token_from_query):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if MCP_SECRET:
    app.add_middleware(SecretHeaderMiddleware)


# ---------------------------------------------------------------------------
# OAuth Protected Resource Metadata (RFC 9728)
# ---------------------------------------------------------------------------
# Este documento le dice a un cliente MCP (Claude, etc.) qué Authorization
# Server usar para obtener un token antes de conectarse a ESTE servidor MCP.
# El Authorization Server real (authorize + token endpoints, login de
# usuario, PKCE) vive en el backend SmallDB, no aquí — ver SMALLDB_BASE_URL
# y el archivo app.py.
async def oauth_protected_resource_metadata(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource": f"https://{PUBLIC_HOST}",
        "authorization_servers": [SMALLDB_BASE_URL],
        "bearer_methods_supported": ["header"],
    })


app.add_route(
    "/.well-known/oauth-protected-resource",
    oauth_protected_resource_metadata,
    methods=["GET"],
)


# ---------------------------------------------------------------------------
# Descarga del servidor del PC (los archivos viven en ./pc_server de este repo)
# ---------------------------------------------------------------------------
# Públicos a propósito: no contienen secretos; el secreto lo genera cada usuario
# en su PC. El agente de IA da estos enlaces cuando alguien aún no tiene túnel.
PC_SERVER_DIR = Path(__file__).resolve().parent / "pc_server"
PC_SERVER_FILES = {
    "iniciar_mcp.bat": "application/x-msdos-program",
    "mcp_archivos.py": "text/x-python; charset=utf-8",
}


async def pc_server_download(request: Request) -> Response:
    name = request.path_params["name"]
    path = PC_SERVER_DIR / name
    if name not in PC_SERVER_FILES or not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return Response(
        path.read_bytes(),
        media_type=PC_SERVER_FILES[name],
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


async def pc_server_guide(request: Request) -> Response:
    return PlainTextResponse(
        "Conectar tu PC a JuanWorkspace (archivos + comandos para Claude)\n"
        "================================================================\n\n"
        + _setup_steps(),
        media_type="text/plain; charset=utf-8",
    )


app.add_route("/pc-server", pc_server_guide, methods=["GET"])
app.add_route("/pc-server/{name}", pc_server_download, methods=["GET"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
