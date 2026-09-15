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
│   │   └── ideas.py             # Ideas, estados e historial
│   ├── services/
│   │   ├── __init__.py
│   │   ├── llm_service.py       # Construcción del prompt y llamada al LLM
│   │   └── idea_service.py      # Persistencia de ideas en SQLite
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── estimation.py        # Contratos de petición y respuesta
│   │   └── idea.py              # Contratos de ideas y ejecuciones
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

El entorno tiene prioridad sobre `.env`, y este sobre los valores predeterminados
de `config.py`. Reinicia la aplicación cuando cambies la configuración.

Las ideas, transcripciones, notas e historial se guardan en SQLite dentro de
`data/`; persisten al recargar la página. El texto enviado a estimar  se transmite a OpenAI.

Esta versión está preparada para uso local, sin cuentas ni separación de datos
entre usuarios. Ejecuta un único proceso de servidor para el ejercicio. Si una
generación se interrumpe al reiniciar, queda registrada como fallida para poder
revisarla y volver a intentarlo desde la idea.




