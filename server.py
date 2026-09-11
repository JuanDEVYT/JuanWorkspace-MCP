"""
Servidor MCP para el ecosistema JuanWorkspace.

Da herramientas para:
  - (admin) registrar/quitar apps del ecosistema, ver cuentas registradas
  - (cuenta) iniciar sesión, listar apps, conectar/desconectar apps,
    leer el manifiesto de una app, y leer/crear/editar/borrar los datos
    de esa app que pertenecen a la cuenta logueada

Habla por HTTP con los endpoints /api/ecosystem/* y /api/admin/ecosystem/*
del backend de SmallDB (el mismo app.py de siempre).

Variables de entorno requeridas:
  SMALLDB_BASE_URL   -> ej: https://juansmalldb.pythonanywhere.com
  SMALLDB_ADMIN_KEY  -> el mismo valor que ADMIN_API_KEY en SmallDB
                        (solo se usa para las herramientas de administración
                        de apps; nunca se expone al modelo)

Variable opcional (fuertemente recomendada):
  MCP_SECRET         -> si se define, todas las peticiones a este servidor MCP
                         deben incluir el header  X-MCP-Secret: <valor>

Ejecutar localmente:
  pip install -r requirements.txt
  python server.py
"""

import os
from typing import Any

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
SMALLDB_BASE_URL = os.environ.get("SMALLDB_BASE_URL", "").rstrip("/")
SMALLDB_ADMIN_KEY = os.environ.get("SMALLDB_ADMIN_KEY", "")
MCP_SECRET = os.environ.get("MCP_SECRET", "")
PORT = int(os.environ.get("PORT", "8000"))

PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "juanworkspace-mcp.onrender.com")

if not SMALLDB_BASE_URL or not SMALLDB_ADMIN_KEY:
    raise RuntimeError(
        "Faltan variables de entorno: SMALLDB_BASE_URL y SMALLDB_ADMIN_KEY son obligatorias."
    )

mcp = MCPServer(
    name="juanworkspace",
    instructions=(
        "Herramientas para administrar el ecosistema JuanWorkspace: registrar "
        "apps nuevas, iniciar sesión con una cuenta de JuanWorkspace, listar "
        "las apps conectadas, y leer/crear/editar/borrar los datos de una app "
        "que pertenecen a la cuenta logueada. Las herramientas de cuenta "
        "requieren un access_token (obtenido con login); las de administración "
        "de apps no lo requieren, usan la clave maestra del servidor."
    ),
)


async def _request(method: str, path: str, headers: dict, json: dict | None = None) -> Any:
    url = f"{SMALLDB_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.request(method, url, headers=headers, json=json)
    if res.status_code >= 400:
        try:
            detail = res.json().get("error", res.text)
        except Exception:
            detail = res.text
        raise RuntimeError(f"JuanWorkspace respondió {res.status_code}: {detail}")
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
