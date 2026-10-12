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

## Interfaz Streamlit (sesión 5: cliente de la API de sesiones)

En la sesión 2, Streamlit llamaba al modelo directamente con litellm (esa
versión sigue en la rama `sesion-02-streamlit`). Desde la sesión 5 es un
**cliente de la API de sesiones**: la conversación, la memoria, los adjuntos
y la caducidad los gestiona la API, y Streamlit los muestra.

Añade a tu `.env` local la clave de Anthropic (nunca la subas al repositorio).
Tiene que ser una clave creada **dentro de un espacio de trabajo** (*workspace*)
de la consola de Anthropic:

```dotenv
ANTHROPIC_API_KEY=tu_clave_de_Anthropic
```

Arranca, desde la carpeta `estimador-cag`, **dos terminales**:

```powershell
uv sync --locked
uv run uvicorn app.main:app --host 127.0.0.1 --port 8001   # 1) la API
uv run streamlit run streamlit_app.py                       # 2) la interfaz
```

Abre **[http://localhost:8501](http://localhost:8501)**. Si la API no está
arrancada, la página lo dice y explica cómo arrancarla.

| Lo que pide el ejercicio | Cómo lo hace `streamlit_app.py` |
| --- | --- |
| a) Crear una sesión al cargar la página | `POST /sessions` y `session_id` guardado en `st.session_state` |
| b) Texto para la transcripción y selector múltiple de ficheros | `st.form` con `st.text_area`, `st.file_uploader(accept_multiple_files=True)` y la elección de camino (A o B) |
| c) `project_metadata` visible | Panel lateral con la **memoria** (los datos que cambian en el turno se marcan con 🆕) y, aparte, el **historial**: barra de turnos en la ventana y turnos descartados |
| d) Botón «Nueva conversación» | Vuelve a llamar a `POST /sessions` y limpia la pantalla |

Además: la estimación se pinta en directo (eventos SSE `status`, `delta`,
`done`, `error`); los turnos que han salido de la ventana se marcan en el chat
(«el modelo ya no ve este turno, pero la memoria conserva sus hechos»); el
panel enseña el system prompt exacto del próximo turno y las métricas de la
última llamada; y si la sesión caduca (410) aparecen dos botones: **Empezar de
cero** o **Continuar con la memoria heredada**. Cada turno consume la API de
Anthropic.

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

### Cómo se construye y se actualiza la memoria (`project_metadata`)

En cada turno la memoria hace un viaje de ida y vuelta:

```text
 ficha (ProjectMetadata) ──► plantilla Jinja2 ──► system prompt ─┐
                                                                 ├──► Claude ──► respuesta
 historial (últimos turnos) + pregunta nueva ────────────────────┘                  │
        ▲                                                                           │
        └──────── extractor heurístico (lee el turno y actualiza la ficha) ◄────────┘
```

**Ida: la ficha entra en el system prompt.** El prompt de las sesiones es una
plantilla Jinja2 versionada,
[app/prompts/estimator_system_v1.j2](app/prompts/estimator_system_v1.j2), que
rellena [prompt_builder.py](app/services/prompt_builder.py). Lleva las mismas
reglas y ejemplos CAG de la sesión 1 y un bloque `<project_metadata>` con los
hechos conocidos, una línea por dato. En el primer turno el bloque va vacío.
El historial no guarda el system prompt: lo genera de nuevo en cada llamada
con la ficha de ese momento, así que siempre va el primero y siempre está al
día. Cada dato pasa por el filtro `as_fact` (una sola línea, sin `<` ni `>`,
200 caracteres como máximo), para que lo que escribe el cliente no pueda
salirse del bloque ni colar instrucciones.

**Vuelta: la ficha se actualiza sola tras cada respuesta.** Lo hace
[metadata_extractor.py](app/services/metadata_extractor.py) con **heurísticas**
(reglas y expresiones regulares en español), sin una segunda llamada al
modelo. Por eso es gratis, instantáneo y siempre da el mismo resultado. Lee
dos fuentes:

1. El bloque `<hechos_del_proyecto>` con el que la plantilla pide al modelo
   que cierre cada respuesta (`Proyecto: …`, `Equipo asumido: …`,
   `Tecnologías: …`, `Alcance acordado: …`). Al tener un formato fijo,
   leerlo con reglas es fiable, y el modelo ya ha tenido en cuenta la
   transcripción, los adjuntos y los turnos anteriores.
2. Frases típicas del cliente en su mensaje: «el proyecto se llama…», «un
   equipo de tres personas», «las tecnologías son…», «hemos acordado que…»,
   y tecnologías conocidas por su nombre (React, PostgreSQL, Kubernetes…).

Al fundirlo con lo que ya sabíamos, un dato nuevo sustituye al anterior, las
tecnologías se suman y ningún hecho se borra por no repetirse. Si el cliente
dice algo explícito en ese turno («ahora seremos 4»), gana sobre el bloque
del modelo, porque es la decisión más reciente. La ficha tiene topes (200
caracteres por dato, 30 tecnologías, equipo de 1 a 1000 personas), porque
viaja en cada system prompt y no puede crecer sin control.

### Ventana deslizante y caducidad de las sesiones

- **Ventana:** en cada llamada viajan como mucho `MAX_TURNS` turnos (6 por
  defecto). Un turno es la pareja pregunta + respuesta, y la pregunta nueva
  cuenta como uno de ellos: con 6, viajan los 5 anteriores más la nueva. Los
  más antiguos se descartan de dos en dos, sin partir nunca un turno.
- **Caducidad (TTL):** una sesión sin actividad durante
  `SESSION_TTL_SECONDS` (1 hora) se archiva: se guarda solo su ficha y el
  historial se descarta. Si el cliente vuelve, recibe un `410` con esa ficha
  y dos opciones: sesión nueva de cero (`POST /sessions`) o heredando la
  memoria (`POST /sessions` con `{"inherit_from": "<id>"}`). La comprobación
  se hace al pedir o crear sesiones, sin ningún proceso de fondo.
- **Tope de sesiones:** como mucho `MAX_SESSIONS` vivas (500). Al pasarse se
  borra la que lleva más tiempo sin usarse.

### Probarlo

Lo más cómodo es la [interfaz Streamlit](#interfaz-streamlit-sesión-5-cliente-de-la-api-de-sesiones):
el panel lateral enseña la memoria y el historial por separado, turno a turno.
Para probar la API directamente necesitas `ANTHROPIC_API_KEY` en tu `.env`
(las dos rutas usan Claude Opus 5.5). Arranca la API como siempre:

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

Lo que se puede comprobar antes de empezar a generar (sesión inexistente o
caducada, transcripción vacía, adjunto que no es PDF ni Word, demasiado grande
o demasiados) se responde con su código HTTP: `404`, `410`, `422`, `415` o
`413`.

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

La extracción se hace en **un proceso Python aparte con tiempo máximo**
(`ATTACHMENT_EXTRACTION_TIMEOUT_S`, 30 s),
[extraction_worker.py](app/services/attachments/extraction_worker.py): si un
documento trucado deja colgado al lector, se cancela ese proceso y la API
sigue funcionando.

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

**Límites de seguridad de los adjuntos.** Un fichero pequeño puede costar
mucho al abrirlo, así que, además del tamaño (`MAX_ATTACHMENT_MB`), se
comprueba antes de extraer nada:

| Riesgo | Defensa |
| --- | --- |
| Un `.docx` es un ZIP: 10 MB pueden ocupar gigas al descomprimirlo | Se suma el tamaño descomprimido leyendo el índice del ZIP, sin descomprimir (`MAX_DOCX_UNCOMPRESSED_MB`) |
| Un PDF pequeño con miles de páginas | Se cuentan las páginas antes de extraer (`MAX_PDF_PAGES`) |
| Un PDF trucado que enseña páginas distintas a cada lector | MarkItDown solo recibe una copia del PDF reescrita por pypdf |
| Un PDF que pasa todos los topes y aun así cuelga al lector | Proceso aparte con tiempo máximo (Camino B) |
| Demasiado texto en total | `413` si se supera `MAX_ATTACHMENT_CHARS`; nunca se recorta en silencio |

En el Camino A el PDF lo abre Anthropic en sus servidores, con sus propios
límites; los `.docx` se convierten a texto en local con los mismos topes.

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
│   ├── prompts/
│   │   └── estimator_system_v1.j2   # Sesión 5: plantilla Jinja2 del system prompt (<project_metadata>)
│   ├── services/
│   │   ├── __init__.py
│   │   ├── llm_service.py       # Construcción del prompt y llamada al LLM
│   │   ├── idea_service.py      # Persistencia de ideas en SQLite
│   │   ├── sessions.py          # Sesión 5: historial con ventana, memoria, TTL y almacén
│   │   ├── prompt_builder.py    # Sesión 5: rellena la plantilla con la memoria
│   │   ├── metadata_extractor.py    # Sesión 5: extractor heurístico de project_metadata
│   │   ├── conversation_service.py  # Sesión 5: un turno completo, de los adjuntos a la memoria
│   │   └── attachments/
│   │       ├── common.py            # Validación y límites de PDF y Word
│   │       ├── path_a_files_api.py  # Camino A: Files API de Anthropic
│   │       ├── path_b_local.py      # Camino B: pypdf / MarkItDown / python-docx + litellm
│   │       └── extraction_worker.py # Camino B: extracción en un proceso aparte con tiempo máximo
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
├── docs/
│   ├── transcripcion_reunion.md
│   └── ejemplos/                # Sesión 5: PDF y Word ficticios para probar los adjuntos
├── scripts/validate_pipeline.py
├── tests/
├── streamlit_app.py             # Interfaz Streamlit (sesión 5: cliente de la API de sesiones)
├── .streamlit/config.toml       # Tema de la interfaz Streamlit (colores y letra)
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
| `POST /sessions` | Sesión 5: crear una conversación; devuelve `session_id`. Con `{"inherit_from": "<id>"}` hereda la memoria de otra sesión |
| `GET /sessions/{id}` | Sesión 5: ver la memoria (`project_metadata`), el historial y cuándo caduca (`expires_at`). `410` si ha caducado |
| `POST /sessions/{id}/estimate` | Sesión 5: estimar con transcripción y adjuntos (multipart); respuesta en streaming SSE. `410` si ha caducado |

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
| `SESSION_TTL_SECONDS` | Caducidad de una sesión sin actividad (3600 = 1 hora). Al caducar se archiva solo su ficha; `GET` o `estimate` responden 410 con dos opciones: sesión nueva de cero o heredando la memoria (`POST /sessions` con `{"inherit_from": "<id>"}`) |
| `ATTACHMENT_MODE` | Camino de adjuntos por defecto: `local` (B, defecto) o `files_api` (A) |
| `PDF_EXTRACTOR` | Camino B: `pypdf` (defecto) o `markitdown` |
| `MAX_ATTACHMENTS` / `MAX_ATTACHMENT_MB` | Límite de adjuntos por petición (5) y de tamaño por fichero (10 MB) |
| `MAX_ATTACHMENT_CHARS` | Caracteres de texto que pueden sumar los adjuntos (300000); si se pasa, 413, nunca se recorta en silencio |
| `MAX_PDF_PAGES` / `MAX_DOCX_UNCOMPRESSED_MB` | Topes que se miran antes de abrir el fichero: páginas de un PDF (300) y tamaño de un Word descomprimido (50 MB). Cortan PDF de miles de páginas y «bombas ZIP» con un 413 |
| `ATTACHMENT_EXTRACTION_TIMEOUT_S` | Camino B: segundos como máximo para extraer el texto (30). La extracción va en un proceso aparte que se cancela si se pasa, así un documento trucado no cuelga la API. `0` = sin límite, solo para depurar |

El entorno tiene prioridad sobre `.env`, y este sobre los valores predeterminados
de `config.py`. Reinicia la aplicación cuando cambies la configuración.

Las ideas, transcripciones, notas e historial se guardan en SQLite dentro de
`data/`; persisten al recargar la página. El texto enviado a estimar  se transmite a OpenAI.

Esta versión está preparada para uso local, sin cuentas ni separación de datos
entre usuarios. Ejecuta un único proceso de servidor para el ejercicio. Si una
generación se interrumpe al reiniciar, queda registrada como fallida para poder
revisarla y volver a intentarlo desde la idea.




