@echo off
chcp 65001 >nul
setlocal
rem ===========================================================================
rem  arrancar_todo.bat · Arranca el estimador: la API y la interfaz Streamlit
rem ===========================================================================
rem  Uso: doble clic en este fichero, o desde una terminal:
rem
rem      arrancar_todo.bat                    (modelo por defecto: Haiku 5.5)
rem      arrancar_todo.bat claude-opus-5-5    (otro modelo, solo esta vez)
rem
rem  Qué hace, en orden:
rem   1. Se coloca en la carpeta de este fichero (estimador-cag). Así da igual
rem      desde dónde se lance: sin esto, uv no encuentra el proyecto.
rem   2. Comprueba que están uv y el fichero .env (con las claves).
rem   3. Abre una ventana con la API (FastAPI, puerto 8001) y espera a que
rem      conteste en /health. Si ya estaba en marcha, la reutiliza.
rem   4. Abre otra ventana con Streamlit (puerto 8501, solo desde este equipo),
rem      espera a que esté lista y abre el navegador.
rem
rem  Para pararlo todo: cerrar las dos ventanas (o Ctrl+C dentro de cada una).
rem  Al parar la API se pierden las sesiones: viven en su memoria, no en disco.
rem ===========================================================================

cd /d "%~dp0"

rem El modelo de las sesiones. Haiku 5.5 por defecto: es el de la batería de
rem la sesión 6 y cada turno cuesta muy poco. Se puede pasar otro como argumento.
set "SESSION_LLM_MODEL=claude-haiku-5-5"
if not "%~1"=="" set "SESSION_LLM_MODEL=%~1"

echo.
echo  Estimador CAG · arrancando la plataforma
echo  Carpeta: %CD%
echo.

rem --- 1. Comprobaciones previas ---------------------------------------------
where uv >nul 2>nul
if errorlevel 1 (
    echo  [ERROR] No se encuentra uv. Instálalo o abre una terminal nueva.
    goto :fin_con_error
)
if not exist ".env" (
    echo  [ERROR] Falta el fichero .env en esta carpeta. Copia .env.example como
    echo          .env y rellena OPENAI_API_KEY y ANTHROPIC_API_KEY.
    goto :fin_con_error
)

rem --- 2. La API (puerto 8001) ---------------------------------------------
curl -sf -o nul -m 2 http://127.0.0.1:8001/health
if not errorlevel 1 (
    echo  [OK] La API ya estaba en marcha en el puerto 8001: la reutilizo.
    echo       Ojo: usa el modelo con el que se arrancó, no necesariamente %SESSION_LLM_MODEL%.
    goto :streamlit
)
netstat -ano -p tcp | findstr "LISTENING" | findstr ":8001 " >nul
if not errorlevel 1 (
    echo  [ERROR] El puerto 8001 está ocupado por otro programa que no es la API.
    echo          Ciérralo, o mira cuál es con: netstat -ano ^| findstr :8001
    goto :fin_con_error
)

echo  [..] Arrancando la API con el modelo %SESSION_LLM_MODEL% ...
rem "start" abre una ventana nueva que hereda SESSION_LLM_MODEL. Sin --reload
rem a propósito: en Windows deja procesos huérfanos (ver automejora.md).
start "Estimador · API (8001)" /D "%CD%" cmd /k "uv run uvicorn app.main:app --host 127.0.0.1 --port 8001"

rem Esperamos a que conteste, como mucho 60 segundos. "ping" hace de pausa de
rem un segundo (timeout no funciona si la consola no es interactiva).
set /a intentos=0
:esperar_api
set /a intentos+=1
curl -sf -o nul -m 2 http://127.0.0.1:8001/health
if not errorlevel 1 goto :api_lista
if %intentos% geq 60 (
    echo  [ERROR] La API no contesta tras 60 segundos. Mira el error en su ventana.
    goto :fin_con_error
)
ping -n 2 127.0.0.1 >nul
goto :esperar_api

:api_lista
echo  [OK] API lista: http://127.0.0.1:8001  (documentación en /docs)

rem --- 3. Streamlit (puerto 8501) ------------------------------------------
:streamlit
curl -sf -o nul -m 2 http://localhost:8501/_stcore/health
if not errorlevel 1 (
    echo  [OK] Streamlit ya estaba en marcha en el puerto 8501.
    goto :abrir_navegador
)

echo  [..] Arrancando Streamlit ...
rem --server.address localhost: solo se puede abrir desde este equipo.
rem --server.headless true: Streamlit no abre el navegador ni pregunta nada al
rem arrancar; lo abrimos nosotros cuando esté listo.
start "Estimador · Streamlit (8501)" /D "%CD%" cmd /k "uv run streamlit run streamlit_app.py --server.address localhost --server.port 8501 --server.headless true"

set /a intentos=0
:esperar_streamlit
set /a intentos+=1
curl -sf -o nul -m 2 http://localhost:8501/_stcore/health
if not errorlevel 1 goto :abrir_navegador
if %intentos% geq 60 (
    echo  [ERROR] Streamlit no contesta tras 60 segundos. Mira el error en su ventana.
    goto :fin_con_error
)
ping -n 2 127.0.0.1 >nul
goto :esperar_streamlit

:abrir_navegador
echo  [OK] Streamlit listo: http://localhost:8501
start "" http://localhost:8501
echo.
echo  Todo en marcha. Para pararlo, cierra las ventanas de la API y de Streamlit.
echo.
endlocal
exit /b 0

:fin_con_error
echo.
pause
endlocal
exit /b 1
