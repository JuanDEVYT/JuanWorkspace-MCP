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

Habla por HTTP con la API administrativa de SmallDB (endpoints /api/admin/...)
y con los endpoints /api/ecosystem/* del mismo backend, usando una clave
maestra (ADMIN_API_KEY) que NUNCA se expone al cliente MCP.

Variables de entorno requeridas:
  SMALLDB_BASE_URL   -> ej: https://juansmalldb.pythonanywhere.com
  SMALLDB_ADMIN_KEY  -> el mismo valor que ADMIN_API_KEY en SmallDB

Variable opcional:
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
        "Herramientas para administrar TODO el ecosistema JuanWorkspace: "
        "tanto la base de datos SmallDB de bajo nivel (proyectos, colecciones, "
        "documentos) como el ecosistema de cuentas y apps (registrar apps, "
        "login, conectar apps, y leer/crear/editar/borrar los datos de una "
        "app que pertenecen a la cuenta logueada). "
        "Las herramientas de cuenta (login, list_apps, get_app_data, etc.) "
        "requieren un access_token obtenido con login(); el resto usa la "
        "clave maestra del servidor, sin necesitar token."
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
