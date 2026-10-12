# Estimador CAG · de la reunión a una estimación

Aplicación web para generar propuestas a clientes para proyectos, incorporando requerimientos de reunión y
generando una estimación de equipos y dedicaciones en horas y euros con OpenAI. 
Cada idea conserva sus notas, su estado y el historial de estimaciones, para poder revisar cómo evoluciona.

El objetivo de esta primera Version 0.1.0 es comprobar el flujo completo:

**Transcripción → validación → instrucciones y ejemplos CAG → OpenAI → estimación.**

No se busca exactitud. Se busca flujo OK. 
Una respuesta correcta de la API demuestra que funciona el circuito;
no demuestra que las horas propuestas sean adecuadas para un proyecto real.

## Arranque en tu equipo

Python 3.11 o posterior y [uv](https://docs.astral.sh/uv/getting-started/installation/).
Ejecuta los comandos desde la carpeta `estimador-cag`, donde está este README:

```powershell
uv sync --locked
```

Si todavía no existe `.env`, crea una copia de `.env.example`. En PowerShell:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

En macOS/Linux, el equivalente es `test -f .env || cp .env.example .env`.
Conserva tu archivo si ya contiene la clave y el modelo. Completa al menos:

```dotenv
OPENAI_API_KEY=tu_clave_de_OpenAI
LLM_MODEL=tu modelo OpenAI
```

Inicia la aplicación:

```powershell
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Abre **[http://127.0.0.1:8001/](http://127.0.0.1:8001/)**.
La documentación técnica interactiva sigue disponible en
[/docs](http://127.0.0.1:8001/docs). Detén el servidor con `Ctrl+C`.

## Probar el ejercicio desde la web

1. Registra un proyecto / idea con un título y una descripción del proyecto.
2. Pega la transcripción o carga un archivo de texto `.txt`/`.md`.
3. También puedes cargar el ejemplo incluido en
   [docs/transcripcion_reunion.md](docs/transcripcion_reunion.md).
4. Guarda la idea y genera su estimación. La aplicación envía el texto al modelo
   configurado; esta acción consume la API de OpenAI.
5. Revisa el resultado, los supuestos, las exclusiones y las preguntas pendientes.
6. Añade notas y actualiza el estado de seguimiento: borrador, en revisión,
   planificada o completada. Puedes volver a generar una estimación y consultar
   las versiones anteriores con la transcripción que se utilizó en cada una.

El ejemplo de reunión es ficticio y trata de un módulo de reservas de salas. Está
versionado junto con este README para que todas las personas usen la misma entrada
durante el ejercicio. La web y el pipeline leen ese mismo archivo.

## Sesión 2 · Interfaz conversacional con Streamlit

Segunda interfaz, independiente de la web FastAPI, que envía la transcripción a
**Claude Sonnet 5** a través de **litellm** y reutiliza el mismo contexto CAG
(`build_system_prompt()` y `ESTIMATION_EXAMPLES` de la sesión 1).

Añade a tu `.env` local la clave de Anthropic (nunca la subas al repositorio):

```dotenv
ANTHROPIC_API_KEY=tu_clave_de_Anthropic
```

Inicia la aplicación desde la carpeta `estimador-cag`:

```powershell
uv sync --locked
uv run streamlit run streamlit_app.py
```

Abre **[http://localhost:8501](http://localhost:8501)**. La web FastAPI sigue
disponible por separado en el puerto 8001. Detén Streamlit con `Ctrl+C`.

Qué incluye `streamlit_app.py`:

| Elemento | Implementación |
| --- | --- |
| Chat e historial | `st.chat_input`, `st.chat_message` y `st.session_state` |
| Modelo anclado | `anthropic/claude-sonnet-5` mediante `litellm.completion` |
| Respuesta en streaming | Generador con `stream=True` consumido por `st.write_stream` |
| Contexto CAG | Mensaje `system` con instrucciones y ejemplos en cada petición |
| Panel lateral | `st.sidebar` con system prompt activo, ejemplos CAG y métricas |
| Métricas | Modelo, tokens de entrada y salida, primer token y tiempo total |
| Errores | Clave inválida, límite de peticiones, conexión y errores del proveedor |

El historial se envía completo en cada petición, así que se pueden pedir
ajustes sobre una estimación anterior. El botón **Nueva conversación** del
panel lateral vacía el historial y las métricas. Cada mensaje consume la API
de Anthropic.

## Sesión 5 · Memoria vs historial, y documentos adjuntos

Hasta ahora cada estimación era una petición suelta. Desde la sesión 5 el
estimador mantiene **conversaciones**: el cliente puede ir afinando la
estimación en varios turnos y adjuntar PDF o Word con especificaciones,
propuestas previas o diagramas.

Para eso separamos dos cosas que es fácil confundir:

- **Historial**: los mensajes tal cual (pregunta del usuario + respuesta del
  modelo). La API del LLM no recuerda nada entre llamadas, así que el
  historial viaja entero en cada petición, dentro del array `messages`. Para
  que no crezca sin fin, solo se conservan los **últimos 6 turnos** (ventana
  deslizante).
- **Memoria** (`project_metadata`): una ficha corta con los hechos del
  proyecto (nombre, tamaño del equipo, tecnologías, alcance acordado). Vive
  aparte del historial, se inyecta en el system prompt y no se pierde aunque
  el turno en el que se dijo algo ya haya salido de la ventana.

Las dos viven en una sesión que el servidor guarda en memoria (un
diccionario de Python, sin base de datos). Si reinicias la API, las sesiones
se pierden: es una limitación aceptada en esta fase y explicada en
[app/services/sessions.py](app/services/sessions.py).

### Probarlo

Necesitas `ANTHROPIC_API_KEY` en tu `.env` (las dos rutas usan Claude Opus
5.5). Arranca la API como siempre:

```powershell
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Y desde otra terminal, situada también en la carpeta `estimador-cag`, crea una
sesión y lanza una estimación con adjuntos. `curl.exe -N` enseña la respuesta
según llega, sin esperar al final:

```powershell
$s = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8001/sessions'
curl.exe -N -X POST "http://127.0.0.1:8001/sessions/$($s.session_id)/estimate" -F "transcript=<docs/transcripcion_reunion.md" -F "attachments=@docs/ejemplos/especificacion_tecnica.pdf" -F "attachments=@docs/ejemplos/propuesta_previa.docx" -F "attachment_mode=local"
```

`-F "transcript=<fichero"` manda el contenido de un fichero de texto como
campo del formulario, y `-F "attachments=@fichero"` sube un fichero (se
repite una vez por adjunto). Las rutas son relativas a la carpeta donde
estás: si curl responde `(26) Failed to open/read local data`, es que no
encuentra alguno de los ficheros.

En [docs/ejemplos/](docs/ejemplos/) hay dos documentos ficticios pensados
para esta prueba: una especificación técnica en PDF (equipo, stack,
requisitos nuevos y un diagrama de arquitectura en la página 2) y una
propuesta previa en Word con las horas reales de un proyecto parecido. Con
ellos la estimación debería cambiar respecto a la de la transcripción sola.
Para comparar los caminos, repite la llamada con `attachment_mode=files_api`:
el diagrama solo lo "ve" el Camino A; el B recibe únicamente sus rótulos.

La respuesta es un stream de [Server-Sent Events](https://developer.mozilla.org/es/docs/Web/API/Server-sent_events)
generado con el soporte nativo de FastAPI (`fastapi.sse.EventSourceResponse`):

| Evento | Qué trae |
| --- | --- |
| `status` | Qué está pasando: camino elegido, texto extraído, ficheros subidos, llamada al modelo |
| `delta` | Un trozo de la estimación (`{"text": "..."}`); el cliente los va pintando |
| `done` | Fin del turno: estimación completa, tokens y estado de la sesión |
| `error` | Algo ha fallado durante el turno; no se guarda y el cliente descarta el texto parcial |

Lo que se puede comprobar antes de empezar a generar (sesión inexistente,
transcripción vacía, adjunto que no es PDF ni Word, demasiado grande o
demasiados) se responde con su código HTTP: `404`, `422`, `415` o `413`.

### Adjuntos: dos caminos

Están implementados los dos, cada uno en su módulo, y se elige en cada
petición con el campo `attachment_mode` (o por defecto con `ATTACHMENT_MODE`
en el `.env`).

**Camino A · multimodal directo** (`attachment_mode=files_api`,
[path_a_files_api.py](app/services/attachments/path_a_files_api.py))

1. El PDF se sube tal cual a la Files API de Anthropic, que devuelve un `file_id`.
2. En el mensaje no va el texto del PDF, sino un bloque `document` que apunta
   a ese `file_id`, seguido de la transcripción.
3. Claude abre el documento por su cuenta: lee el texto y ve cada página como
   imagen, así que entiende tablas, diagramas y PDF escaneados.

Los `.docx` son la excepción: la API de Anthropic solo acepta PDF y texto
plano como documento, así que su texto se extrae en local y se sube como
`.txt`. Los ficheros subidos caducan solos a las 24 horas.

**Camino B · extracción local** (`attachment_mode=local`,
[path_b_local.py](app/services/attachments/path_b_local.py))

1. El texto se saca en nuestro proceso: **pypdf** para PDF con texto,
   **MarkItDown** para PDF complejos (devuelve Markdown más estructurado; se
   usa también automáticamente si pypdf no extrae casi nada) y
   **python-docx** para Word (párrafos, títulos y tablas en su orden).
2. Ese texto se pega al final de la transcripción, cada documento tras un
   separador: `--- attachment: especificacion.pdf ---`.
3. El mensaje es texto plano y se envía con **litellm**, que funciona igual
   con cualquier proveedor.

| | Camino A · Files API | Camino B · extracción local |
| --- | --- | --- |
| Código propio | Poco: subir y referenciar | Más: extraer, limpiar, concatenar |
| Imágenes, diagramas, PDF escaneados | Sí, el modelo ve las páginas | No, solo el texto |
| Qué le llega al modelo | Lo decide el proveedor | Lo vemos y controlamos nosotros |
| Proveedor | Atado a Anthropic | Cualquiera (litellm) |
| Tokens | Más (cada página es texto + imagen) | Menos (solo texto) |
| Siguiente paso natural | — | Trocear el texto para RAG (módulo 3) |

**Cuál usamos por defecto: el Camino B.** Nos deja ver exactamente qué
texto recibe el modelo, no nos ata a un proveedor y es la base del chunking
que haremos con RAG en el módulo 3. El Camino A queda disponible en cada
petición para los casos en que el texto no basta: PDF escaneados o
documentos donde lo importante es un diagrama de arquitectura.

Los dos caminos usan el mismo modelo (Claude Opus 5.5) y el mismo esfuerzo de
razonamiento (`medium`), para que al compararlos la única diferencia sea
cómo llegan los documentos. En el Camino A está activado el *fallback*
automático de Anthropic: si los filtros de seguridad rechazan una petición,
la API la repite con el modelo que recomienda para ese caso.

## Arquitectura y capa de responsabilidades

```text
estimador-cag/
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI, web, arranque y routers
│   ├── config.py                # Configuración desde entorno y .env
│   ├── errors.py                # Errores HTTP comprensibles
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── estimations.py       # POST /api/v1/estimate
│   │   ├── ideas.py             # Ideas, estados e historial
│   │   └── sessions.py          # Sesión 5: /sessions y /sessions/{id}/estimate (SSE)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── llm_service.py       # Construcción del prompt y llamada al LLM
│   │   ├── idea_service.py      # Persistencia de ideas en SQLite
│   │   ├── sessions.py          # Sesión 5: historial con ventana, memoria y almacén
│   │   ├── conversation_service.py  # Sesión 5: un turno completo, de los adjuntos al historial
│   │   └── attachments/
│   │       ├── common.py            # Validación de PDF y Word
│   │       ├── path_a_files_api.py  # Camino A: Files API de Anthropic
│   │       └── path_b_local.py      # Camino B: pypdf / MarkItDown / python-docx + litellm
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── estimation.py        # Contratos de petición y respuesta
│   │   ├── idea.py              # Contratos de ideas y ejecuciones
│   │   └── session.py           # Sesión 5: respuestas de /sessions
│   ├── context/
│   │   ├── __init__.py
│   │   └── examples.py          # Conocimiento de referencia CAG
│   └── web/
│       ├── index.html           # Interfaz de usuario
│       ├── app.js              # Formularios y comunicación con la API
│       └── styles.css          # Diseño adaptable a móvil y escritorio
├── docs/transcripcion_reunion.md
├── scripts/validate_pipeline.py
├── tests/
├── .github/workflows/validate.yml
├── .env.example
├── .gitignore
├── pyproject.toml
├── uv.lock
└── README.md
```

El contenido de `ESTIMATION_EXAMPLES` se incorpora al mensaje `system`, junto
con las instrucciones de estimación. 
La transcripción completa se envía como mensaje `user`. El mensaje `assistant` contiene la estimación devuelta por el modelo.

En este ejercicio, **CAG es el contexto de referencia que aportamos al modelo**.
Los ejemplos permanecen en el repositorio y se añaden a cada petición.

## API

| Método y ruta | Uso |
| --- | --- |
| `GET /` | Aplicación web |
| `GET /health` | Comprobar que la aplicación responde; no llama a OpenAI |
| `GET /api/v1/meta` | Nombre de aplicación, modelo, proveedor y número de referencias |
| `GET /api/v1/sample-transcription` | Transcripción ficticia del ejercicio |
| `POST /api/v1/estimate` | Estimar directamente una transcripción, sin crear una idea |
| `GET /api/v1/ideas` | Listar ideas guardadas |
| `POST /api/v1/ideas` | Crear una idea |
| `GET /api/v1/ideas/{id}` | Consultar una idea y sus versiones |
| `PATCH /api/v1/ideas/{id}` | Actualizar título, descripción, texto, notas o estado |
| `POST /api/v1/ideas/{id}/estimate` | Generar y guardar una nueva versión |
| `POST /sessions` | Sesión 5: crear una conversación vacía; devuelve `{"session_id": "..."}` |
| `GET /sessions/{id}` | Sesión 5: ver la memoria (`project_metadata`) y el historial de la sesión |
| `POST /sessions/{id}/estimate` | Sesión 5: estimar con transcripción y adjuntos (multipart); respuesta en streaming SSE |

Petición mínima para el endpoint original:

```json
{
  "transcription": "Necesitamos un módulo web para reservar salas. Ya existe autenticación y un proveedor de correo. No incluimos pagos."
}
```

Respuesta:

```json
{
  "estimation": "Texto de la estimación generada...",
  "model": "gpt-4o-mini",
  "provider": "openai"
}
```

`422` indica datos de entrada inválidos; `409`, una estimación ya en curso para
esa idea; `502`, un fallo o respuesta incompleta del proveedor; `503`, un límite
de servicio; `504`, tiempo de espera agotado. El resultado no se guarda como una
estimación correcta si el modelo devuelve una respuesta incompleta.

## Pipeline automático de validación

### Sin credenciales ni consumo de API

```powershell
uv run python scripts/validate_pipeline.py
```

El pipeline realiza cuatro comprobaciones y termina con código `0` solo si pasan:

1. Verifica los archivos esperados y analiza la sintaxis de los módulos Python.
2. Carga la transcripción del ejercicio y comprueba que tenga contenido.
3. Ejecuta todas las pruebas con `unittest` y rechaza una suite vacía.
4. Inicia la aplicación con un directorio de datos temporal y llama a la API con
   `TestClient`: verifica la web, sus recursos, `/health`, el rechazo de texto
   vacío y el recorrido completo de una petición válida por el servicio y el SDK.


### Con una llamada real a OpenAI

```powershell
uv run python scripts/validate_pipeline.py --live
```

Usa la configuración de `.env` o del entorno. Ejecuta las mismas comprobaciones
y envía una única petición al proveedor, con los reintentos desactivados. El
resultado real debe tener texto y superar el contrato de respuesta de la API.


Cada ejecución escribe un informe `pipeline-<modo>-<fecha>.json`. Si completa el
flujo, escribe también `estimation-<modo>-<fecha>.md`. El informe incluye modo,
comprobaciones, cantidad de pruebas, huella del archivo de entrada, estado HTTP,
modelo y duración. No incluye la clave ni la transcripción completa. La estimación
Markdown puede reflejar información del texto utilizado.


## Configuración y datos guardados

| Variable | Función |
| --- | --- |
| `OPENAI_API_KEY` | Clave de OpenAI, obligatoria para las llamadas reales |
| `LLM_MODEL` | Modelo; `OPENAI_MODEL` se admite como alias |
| `LLM_PROVIDER` | Proveedor; esta implementación admite `openai` |
| `APP_DEBUG` | Modo de depuración de la aplicación |
| `DATA_DIR` | Carpeta de la base de datos; por defecto, `data/` en el proyecto |
| `ANTHROPIC_API_KEY` | Clave de Anthropic: Streamlit (sesión 2) y endpoints `/sessions` (sesión 5) |
| `SESSION_LLM_MODEL` | Modelo de las sesiones; por defecto `claude-opus-5-5` |
| `SESSION_LLM_EFFORT` | Esfuerzo de razonamiento de Opus 5.5: `low`, `medium` (defecto), `high`... |
| `SESSION_MAX_TOKENS` | Techo de tokens de salida de cada respuesta, razonamiento incluido (64000) |
| `MAX_TURNS` | Ventana del historial: turnos (pregunta + respuesta) que viajan en cada llamada, contando la pregunta nueva (6). La memoria `project_metadata` no se recorta |
| `MAX_SESSIONS` | Sesiones a la vez en memoria; al pasarse se borra la menos usada (500) |
| `ATTACHMENT_MODE` | Camino de adjuntos por defecto: `local` (B, defecto) o `files_api` (A) |
| `PDF_EXTRACTOR` | Camino B: `pypdf` (defecto) o `markitdown` |
| `MAX_ATTACHMENTS` / `MAX_ATTACHMENT_MB` | Límite de adjuntos por petición (5) y de tamaño por fichero (10 MB) |
| `MAX_ATTACHMENT_CHARS` | Caracteres de texto que pueden sumar los adjuntos (300000); si se pasa, 413, nunca se recorta en silencio |
| `MAX_PDF_PAGES` / `MAX_DOCX_UNCOMPRESSED_MB` | Topes que se miran antes de abrir el fichero: páginas de un PDF (300) y tamaño de un Word descomprimido (50 MB). Cortan PDF de miles de páginas y «bombas ZIP» con un 413 |

El entorno tiene prioridad sobre `.env`, y este sobre los valores predeterminados
de `config.py`. Reinicia la aplicación cuando cambies la configuración.

Las ideas, transcripciones, notas e historial se guardan en SQLite dentro de
`data/`; persisten al recargar la página. El texto enviado a estimar  se transmite a OpenAI.

Esta versión está preparada para uso local, sin cuentas ni separación de datos
entre usuarios. Ejecuta un único proceso de servidor para el ejercicio. Si una
generación se interrumpe al reiniciar, queda registrada como fallida para poder
revisarla y volver a intentarlo desde la idea.




