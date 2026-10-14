# Panel de JuanWorkspace: sección «Túnel de mi PC»

El MCP (`server.py`) y el panel comparten **el mismo dato**: un documento en la app
del ecosistema `pc-tunnel`, dentro de los datos de la cuenta:

```json
{ "kind": "tunnel", "tunnel_url": "https://algo.trycloudflare.com/<secreto>/mcp" }
```

- El panel lo escribe con el `access_token` de la cuenta (igual que cualquier app conectada).
- Las tools `pc_*` del MCP lo leen con el token OAuth de esa misma cuenta. Cada cuenta usa
  **solo su** túnel; no hay URL global.
- La app `pc-tunnel` se registra sola la primera vez que alguien usa una tool `pc_*`
  (o con la tool `setup_pc_tunnel_app`). Hazlo una vez antes de probar el panel.
- El MCP acepta la URL tal como la muestra `iniciar_mcp.bat` (con `/mcp` al final) y la normaliza.

## Fragmento (JS sin dependencias)

Ajusta solo las tres primeras constantes a cómo ya guarda el panel su sesión.

```html
<section id="pc-tunnel" class="card">
  <h3>🖥️ Túnel de mi PC</h3>
  <p class="muted">Pega la URL que te muestra <b>iniciar_mcp.bat</b>. Trátala como una contraseña.</p>
  <input id="pc-url" type="password" placeholder="https://…trycloudflare.com/…/mcp" autocomplete="off" />
  <label><input type="checkbox" id="pc-show" /> Mostrar</label>
  <div class="row">
    <button id="pc-save">Guardar y probar</button>
    <button id="pc-test" class="secondary">Probar</button>
    <button id="pc-clear" class="danger">Quitar</button>
  </div>
  <p id="pc-msg" role="status"></p>
  <details>
    <summary>¿Aún no tienes el túnel? Guía rápida</summary>
    <ol>
      <li>Descarga <a href="https://juanworkspace-mcp.onrender.com/pc-server/iniciar_mcp.bat">iniciar_mcp.bat</a>
          y <a href="https://juanworkspace-mcp.onrender.com/pc-server/mcp_archivos.py">mcp_archivos.py</a>
          y guárdalos juntos <b>fuera</b> de la carpeta que vas a compartir.</li>
      <li>Instala Python 3 y pon <code>cloudflared.exe</code> junto al .bat
          (<a href="https://github.com/cloudflare/cloudflared/releases/latest">descarga</a>).</li>
      <li>Abre el .bat, configura carpeta/permisos y elige <b>1. Iniciar</b>.</li>
      <li>Pega aquí la URL que aparece y pulsa <b>Guardar y probar</b>.</li>
    </ol>
  </details>
</section>

<script>
(() => {
  // === AJUSTAR a tu panel =================================================
  const API  = "https://juansmalldb.pythonanywhere.com";   // backend SmallDB
  const SLUG = "pc-tunnel";                                 // app del ecosistema
  const token = () => localStorage.getItem("access_token"); // <- cómo guarda tu panel el token
  // ========================================================================

  const $ = (id) => document.getElementById(id);
  const say = (t, ok) => { const m = $("pc-msg"); m.textContent = t; m.style.color = ok === false ? "#f87171" : ok ? "#4ade80" : ""; };
  const H = () => ({ "Authorization": `Bearer ${token()}`, "Content-Type": "application/json" });

  async function api(method, path, body) {
    const r = await fetch(API + path, { method, headers: H(), body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) { const e = new Error(`HTTP ${r.status}`); e.status = r.status; throw e; }
    return r.status === 204 ? {} : r.json();
  }

  // Lee mi documento de túnel; si la cuenta aún no está conectada a la app, la conecta y reintenta.
  async function loadDoc() {
    for (let i = 0; i < 2; i++) {
      try {
        const { data } = await api("GET", `/api/ecosystem/apps/${SLUG}/data`);
        return (data || []).find((d) => d.kind === "tunnel") || null;
      } catch (e) {
        if (e.status === 401) throw e;
        if (i === 1) throw e;
        await api("POST", `/api/ecosystem/apps/${SLUG}/connect`).catch(() => {});
      }
    }
  }

  function base(raw) {                       // …/<secreto>/mcp  ->  …/<secreto>
    const u = new URL(raw.trim());
    if (u.protocol !== "https:") throw new Error("Debe empezar por https://");
    let p = u.pathname.replace(/\/+$/, "").replace(/\/mcp$/, "").replace(/\/+$/, "");
    if (!p.replace(/\//g, "")) throw new Error("Falta el secreto: copia la URL COMPLETA del .bat");
    return `${u.origin}${p}`;
  }

  async function ping(raw) {                 // el servidor del PC permite CORS en /api/ping
    const r = await fetch(`${base(raw)}/api/ping`, { cache: "no-store" });
    if (!r.ok) throw new Error(`el PC respondió HTTP ${r.status} (¿secreto distinto?)`);
    return r.json();
  }

  async function test(raw) {
    try {
      const i = await ping(raw);
      say(`✅ PC conectado · carpeta «${i.root_name}» · borrar: ${i.allow_delete ? "sí" : "no"} · comandos: ${i.allow_exec ? "sí" : "no"}`, true);
    } catch (e) {
      say(`⚠️ No responde: ${e.message}. ¿Está abierto iniciar_mcp.bat (servidor + túnel)? Si lo reiniciaste, la URL cambió.`, false);
    }
  }

  $("pc-show").onchange = (e) => ($("pc-url").type = e.target.checked ? "text" : "password");

  $("pc-save").onclick = async () => {
    const url = $("pc-url").value.trim();
    try { base(url); } catch (e) { return say(e.message, false); }
    try {
      const payload = { kind: "tunnel", tunnel_url: url, updated_at: new Date().toISOString() };
      const doc = await loadDoc();
      if (doc?._id) await api("PATCH", `/api/ecosystem/apps/${SLUG}/data/${doc._id}`, payload);
      else          await api("POST",  `/api/ecosystem/apps/${SLUG}/data`, payload);
      say("Guardado. Probando…");
      await test(url);
    } catch (e) { say(e.status === 401 ? "Sesión expirada: vuelve a iniciar sesión." : `No se pudo guardar (${e.message}).`, false); }
  };

  $("pc-test").onclick = () => { const u = $("pc-url").value.trim(); u ? test(u) : say("Pega o carga una URL primero.", false); };

  $("pc-clear").onclick = async () => {
    try {
      const doc = await loadDoc();
      if (doc?._id) await api("DELETE", `/api/ecosystem/apps/${SLUG}/data/${doc._id}`);
      $("pc-url").value = ""; say("Túnel quitado. Claude ya no tiene acceso a tu PC.", true);
    } catch (e) { say(`No se pudo quitar (${e.message}).`, false); }
  };

  // Al abrir el panel, carga la URL guardada y comprueba si responde.
  loadDoc().then((d) => { if (d?.tunnel_url) { $("pc-url").value = d.tunnel_url; test(d.tunnel_url); } }).catch(() => {});
})();
</script>
```

## Notas

- Usa los endpoints `/api/ecosystem/apps/<slug>/{connect,data}` que ya usa el MCP, así que no
  hace falta tocar `app.py` de SmallDB.
- La URL del túnel es una credencial: el campo está oculto por defecto y nunca se imprime completa
  en los mensajes del MCP (solo `https://dominio/<secreto oculto>`).
- Si el panel ya muestra apps a partir de un manifiesto, la app `pc-tunnel` aparecerá en la lista; este
  bloque es independiente de eso y puede ir en cualquier página del panel.
