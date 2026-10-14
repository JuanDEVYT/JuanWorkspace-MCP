@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
title MCP Archivos - Panel

set "CFG=%~dp0mcp_config.cmd"
set "ROOT="
set "PORT=8765"
set "ALLOWDEL=n"
set "ALLOWEXEC=n"
if exist "%CFG%" call "%CFG%"

if not defined ROOT goto config

:menu
cls
set "SHOWROOT=!ROOT!"
if not defined ROOT set "SHOWROOT=[sin configurar]"
set "SHOWDEL=NO"
if /i "!ALLOWDEL!"=="s" set "SHOWDEL=SI"
set "SHOWEXEC=NO"
if /i "!ALLOWEXEC!"=="s" set "SHOWEXEC=SI  ^(control total del PC^)"
echo ==============================================
echo    MCP ARCHIVOS  -  Panel de control  ^(v4^)
echo ==============================================
echo.
echo   Carpeta permitida : !SHOWROOT!
echo   Puerto            : !PORT!
echo   Permitir borrar   : !SHOWDEL!
echo   Permitir comandos : !SHOWEXEC!
echo.
echo   [1] Iniciar servidor + tunel
echo   [2] Configurar carpeta, puerto, borrado y comandos
echo   [3] Ver / copiar la URL del tunel
echo   [4] Detener todo
echo   [5] Regenerar secreto ^(invalida la URL anterior^)
echo   [6] Salir
echo.
choice /c 123456 /n /m "Elige una opcion: "
set "OPT=%errorlevel%"
if "!OPT!"=="1" goto start
if "!OPT!"=="2" goto config
if "!OPT!"=="3" goto showurl
if "!OPT!"=="4" goto stop
if "!OPT!"=="5" goto resecret
exit /b 0

:: ------------------------------------------------------------ CONFIG
:config
cls
echo ----------- CONFIGURACION -----------
echo.
:askroot
set "NEWROOT="
set /p "NEWROOT=Carpeta que Claude podra ver y editar (puedes arrastrarla aqui): "
set "NEWROOT=!NEWROOT:"=!"
if not defined NEWROOT goto askroot
if "!NEWROOT:~-1!"=="\" set "NEWROOT=!NEWROOT:~0,-1!"
if not exist "!NEWROOT!\" (
  echo  Esa carpeta no existe, intenta de nuevo.
  goto askroot
)
if "!NEWROOT:~3!"=="" (
  echo.
  echo  ATENCION: eso es la raiz de un disco completo.
  choice /c SN /m "Seguro que quieres darle acceso a todo el disco"
  if errorlevel 2 goto askroot
)
set "HERE=%~dp0"
call set "CHK=%%HERE:!NEWROOT!\=%%"
if not "!CHK!"=="!HERE!" (
  echo.
  echo  ATENCION: los archivos del servidor ^(este .bat y mcp_archivos.py^)
  echo  estan DENTRO de esa carpeta. Claude podria leer el secreto
  echo  ^(.mcp_secret^) o modificar el propio servidor.
  choice /c SN /m "Quieres elegir otra carpeta"
  if not errorlevel 2 goto askroot
)
set "ROOT=!NEWROOT!"

set "NEWPORT="
set /p "NEWPORT=Puerto [!PORT!]: "
if defined NEWPORT set "PORT=!NEWPORT!"

echo.
choice /c SN /m "Permitir que Claude BORRE archivos y carpetas"
if errorlevel 2 (set "ALLOWDEL=n") else (set "ALLOWDEL=s")

echo.
echo  ----------------------------------------------------------
echo   COMANDOS: si lo activas, Claude podra ejecutar CUALQUIER
echo   comando en tu PC con tus permisos de usuario. Eso NO se
echo   limita a la carpeta compartida: puede leer, cambiar o
echo   borrar todo lo que tu puedas. Activalo solo si confias en
echo   quien tenga la URL del tunel.
echo  ----------------------------------------------------------
choice /c SN /m "Permitir que Claude EJECUTE comandos"
if errorlevel 2 (set "ALLOWEXEC=n") else (set "ALLOWEXEC=s")

> "%CFG%" echo set "ROOT=!ROOT!"
>> "%CFG%" echo set "PORT=!PORT!"
>> "%CFG%" echo set "ALLOWDEL=!ALLOWDEL!"
>> "%CFG%" echo set "ALLOWEXEC=!ALLOWEXEC!"
echo.
echo  Configuracion guardada.
timeout /t 2 >nul
goto menu

:: ------------------------------------------------------------- START
:start
call :deps
if errorlevel 1 goto menu
call :killall

set "DELFLAG="
if /i "!ALLOWDEL!"=="s" set "DELFLAG=--allow-delete"
if /i "!ALLOWEXEC!"=="s" set "DELFLAG=!DELFLAG! --allow-exec"

echo.
echo  Iniciando servidor MCP...
start "MCP Archivos - Servidor" cmd /k python mcp_archivos.py --root "!ROOT!" --port !PORT! !DELFLAG!

echo  Iniciando tunel de Cloudflare...
if exist tunnel.log del tunnel.log >nul 2>&1
start "MCP Archivos - Tunel" /min cmd /c ""!CF!" tunnel --url http://localhost:!PORT! > tunnel.log 2>&1"

echo  Esperando la URL publica ^(hasta 40 s^)...
set "TURL="
for /l %%i in (1,1,40) do (
  if not defined TURL (
    timeout /t 1 /nobreak >nul
    for /f "usebackq delims=" %%u in (`powershell -NoProfile -Command "$m=Select-String -Path 'tunnel.log' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue; if($m){$m[0].Matches[0].Value}"`) do set "TURL=%%u"
  )
)

if not defined TURL (
  echo.
  echo  No se pudo leer la URL del tunel. Revisa la ventana del tunel o tunnel.log
  pause
  goto menu
)

set "SECRET="
if exist ".mcp_secret" set /p SECRET=<".mcp_secret"
if not defined SECRET (
  echo.
  echo  No se encontro el secreto. Revisa la ventana del servidor por errores.
  pause
  goto menu
)

set "FULL=!TURL!/!SECRET!/mcp"
> url_conector.txt echo !FULL!
<nul set /p "=!FULL!" | clip

echo.
echo ==============================================
echo   LISTO. URL de tu tunel ^(ya copiada al portapapeles^):
echo.
echo   !FULL!
echo.
echo   Pegala en el panel de JuanWorkspace:
echo   "Tunel de mi PC" ^> Guardar.
echo   ^(Con eso Claude usa tu PC a traves de JuanWorkspace.^)
echo.
echo   Trata esta URL como una contrasena: quien la tenga
echo   controla tu carpeta compartida.
echo ==============================================
echo.
echo  Deja abiertas las ventanas del servidor y del tunel.
echo  Si las cierras o reinicias, la URL cambia: repite este paso.
pause
goto menu

:: ---------------------------------------------------------- SHOW URL
:showurl
echo.
if not exist url_conector.txt (
  echo  Aun no hay URL. Usa la opcion 1 primero.
  pause
  goto menu
)
set /p SAVED=<url_conector.txt
echo  !SAVED!
<nul set /p "=!SAVED!" | clip
echo.
echo  ^(copiada al portapapeles; solo sirve mientras el tunel siga activo^)
pause
goto menu

:: --------------------------------------------------------------- STOP
:stop
call :killall
echo.
echo  Servidor y tunel detenidos.
timeout /t 2 >nul
goto menu

:: ---------------------------------------------------------- RESECRET
:resecret
echo.
choice /c SN /m "Esto detiene todo y cambia la URL. Continuar"
if errorlevel 2 goto menu
call :killall
if exist ".mcp_secret" del ".mcp_secret" >nul 2>&1
if exist url_conector.txt del url_conector.txt >nul 2>&1
echo  Secreto eliminado. Se generara uno nuevo al iniciar de nuevo.
pause
goto menu

:: ------------------------------------------------------------ HELPERS
:killall
taskkill /fi "WINDOWTITLE eq MCP Archivos - Servidor*" /f /t >nul 2>&1
taskkill /fi "WINDOWTITLE eq MCP Archivos - Tunel*" /f /t >nul 2>&1
taskkill /im cloudflared.exe /f >nul 2>&1
exit /b 0

:deps
where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo  No se encontro Python. Instalalo desde python.org marcando "Add to PATH".
  pause
  exit /b 1
)
python -c "import mcp" >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Instalando la libreria mcp...
  python -m pip install -U "mcp[cli]"
  if errorlevel 1 (
    echo  Fallo la instalacion de mcp.
    pause
    exit /b 1
  )
)
if not exist "mcp_archivos.py" (
  echo.
  echo  No encuentro mcp_archivos.py junto a este archivo.
  pause
  exit /b 1
)
set "CF="
if exist "%~dp0cloudflared.exe" set "CF=%~dp0cloudflared.exe"
if not defined CF for /f "delims=" %%c in ('where cloudflared 2^>nul') do if not defined CF set "CF=%%c"
if defined CF exit /b 0
echo.
echo  No se encontro cloudflared.
where winget >nul 2>&1
if errorlevel 1 goto nowinget
choice /c SN /m "Instalarlo ahora con winget"
if errorlevel 2 exit /b 1
winget install -e --id Cloudflare.cloudflared
echo.
echo  Cierra y vuelve a abrir este archivo para que reconozca cloudflared.
pause
exit /b 1

:nowinget
echo.
echo  Busque aqui: %~dp0cloudflared.exe
echo  Archivos con "cloudflared" en esa carpeta:
dir /b "%~dp0*cloudflared*" 2>nul
echo.
echo  Tu Windows no tiene winget. Instalacion manual ^(1 minuto^):
echo    1. Descarga cloudflared-windows-amd64.exe desde:
echo       https://github.com/cloudflare/cloudflared/releases/latest
echo    2. Renombralo a cloudflared.exe
echo    3. Ponlo en esta misma carpeta: %~dp0
echo    4. Vuelve a ejecutar este archivo
echo.
choice /c SN /m "Abrir la pagina de descarga ahora"
if errorlevel 2 exit /b 1
start https://github.com/cloudflare/cloudflared/releases/latest
pause
exit /b 1
