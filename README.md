# Estimador CAG · de la reunión a una estimación

Aplicación web para guardar ideas, incorporar una transcripción de reunión y
generar una estimación de software con OpenAI. Cada idea conserva sus notas,
su estado y el historial de estimaciones, para poder revisar cómo evoluciona.

El objetivo del ejercicio es comprobar el flujo completo:

**Transcripción → validación → instrucciones y ejemplos CAG → OpenAI → estimación.**

La calidad y precisión de las estimaciones se revisarán en las siguientes
iteraciones. Una respuesta correcta de la API demuestra que funciona el circuito;
no demuestra que las horas propuestas sean adecuadas para un proyecto real.

## Arranque en tu equipo

Necesitas Python 3.11 o posterior y [uv](https://docs.astral.sh/uv/getting-started/installation/).
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
LLM_MODEL=gpt-4o-mini
```

Inicia la aplicación:

```powershell
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

Abre **[http://127.0.0.1:8001/](http://127.0.0.1:8001/)**.
La documentación técnica interactiva sigue disponible en
[/docs](http://127.0.0.1:8001/docs). Detén el servidor con `Ctrl+C`.

## Probar el ejercicio desde la web

1. En **Preparar**, añade un nombre y una descripción del proyecto.
2. Pega la transcripción o pulsa **Importar archivo** para cargar un `.txt`/`.md`.
3. También puedes pulsar **Cargar reunión de ejemplo**, que usa
   [docs/transcripcion_reunion.md](docs/transcripcion_reunion.md).
4. Pulsa **Generar estimación**: guarda la idea y envía el texto al modelo
   configurado; esta acción consume la API de OpenAI.
5. En **Estimación**, revisa el resultado, los supuestos y las preguntas pendientes.
   Puedes copiarlo o descargarlo como Markdown.
6. En **Seguimiento**, añade notas y actualiza el estado: idea, en revisión,
   planificada o completada. Pulsa **Guardar seguimiento** para conservarlos.
   Puedes volver a generar una estimación y consultar
   las versiones anteriores con la transcripción que se utilizó en cada una.

**Solo la transcripción se envía al modelo.** El nombre, la descripción, el estado
y las notas organizan el seguimiento local. Si una decisión debe cambiar la
próxima estimación, incorpórala a la transcripción antes de generar otra versión.
**Guardar idea** permite conservar un borrador sin realizar una llamada al LLM.

El ejemplo de reunión es ficticio y trata de un módulo de reservas de salas. Está
versionado junto con este README para que todas las personas usen la misma entrada
durante el ejercicio. La web y el pipeline leen ese mismo archivo.

## Arquitectura y responsabilidades

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
con las instrucciones de estimación. La transcripción completa se envía como
mensaje `user`. El mensaje `assistant` contiene la estimación devuelta por el
modelo mediante [Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create).

En este ejercicio, **CAG es el contexto de referencia que aportamos al modelo**.
Los ejemplos permanecen en el repositorio y se añaden a cada petición; no entrenamos
un modelo ni utilizamos una base vectorial. Tampoco implementamos una caché propia
de respuestas o del estado interno del modelo.

### Referencias pendientes de revisar

El ejemplo de Tesorería en `examples.py` tiene un desglose que suma **183 horas** y
un total declarado de **320 horas**. Se conserva para la revisión del ejercicio.
El prompt pide no copiar esa inconsistencia. La validación automatizada comprueba
que se inyecten los ejemplos, no aprueba la exactitud de sus cifras.

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

Para la última comprobación se usa el SDK real de OpenAI con
`httpx.MockTransport`: la respuesta HTTP del proveedor se simula. Se inspecciona
que el mensaje `system` contenga todos los ejemplos, que el `user` contenga la
transcripción y que se utilice el modelo configurado. La respuesta simulada se
identifica expresamente como tal. El directorio de datos habitual no se modifica.

### Con una llamada real a OpenAI

```powershell
uv run python scripts/validate_pipeline.py --live
```

Usa la configuración de `.env` o del entorno. Ejecuta las mismas comprobaciones
y envía una única petición al proveedor, con los reintentos desactivados. El
resultado real debe tener texto y superar el contrato de respuesta de la API.

Opciones para usar otro archivo y elegir la carpeta de salida:

```powershell
uv run python scripts/validate_pipeline.py --transcription docs/transcripcion_reunion.md --output-dir artifacts/ejercicio
```

Cada ejecución escribe un informe `pipeline-<modo>-<fecha>.json`. Si completa el
flujo, escribe también `estimation-<modo>-<fecha>.md`. El informe incluye modo,
comprobaciones, cantidad de pruebas, huella del archivo de entrada, estado HTTP,
modelo y duración. No incluye la clave ni la transcripción completa. La estimación
Markdown puede reflejar información del texto utilizado.

### GitHub Actions

El workflow [.github/workflows/validate.yml](.github/workflows/validate.yml)
ejecuta automáticamente el pipeline simulado en **Ubuntu y Windows con Python
3.11**, en cada `push` y `pull_request`. Instala las dependencias fijadas en
`uv.lock` y adjunta los resultados como artefactos durante siete días.

Para habilitar una prueba real manual en un repositorio de GitHub:

1. Sube el contenido de **esta carpeta `estimador-cag` como raíz del repositorio**;
   `.github/workflows` debe estar directamente en la raíz.
2. Crea el secreto de Actions `OPENAI_API_KEY` en la configuración del repositorio.
3. Opcionalmente, define la variable de Actions `LLM_MODEL`.
4. Abre **Actions → Validar estimador CAG → Run workflow**, selecciona la rama
   predeterminada y activa `live_llm`.

La llamada real se ejecuta solo mediante esa acción manual, desde la rama
predeterminada y después de pasar las validaciones automáticas. Las peticiones
de cambio no reciben la clave. Crear el workflow localmente lo deja preparado;
la ejecución en GitHub empieza cuando exista el repositorio remoto con el archivo.

Se utilizan [checkout](https://github.com/actions/checkout),
[setup-python](https://github.com/actions/setup-python),
[setup-uv](https://github.com/astral-sh/setup-uv) y
[upload-artifact](https://github.com/actions/upload-artifact).

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
`data/`; persisten al recargar la página y al reiniciar el servidor. `.env`,
`data/` y `artifacts/` están excluidos de Git. La clave se utiliza en el servidor
y no se entrega al navegador. El texto enviado a estimar sí se transmite a OpenAI.

Esta versión está preparada para uso local, sin cuentas ni separación de datos
entre usuarios. Ejecuta un único proceso de servidor para el ejercicio. Si una
generación se interrumpe al reiniciar, queda registrada como fallida para poder
revisarla y volver a intentarlo desde la idea.

## Problemas habituales

- **`ModuleNotFoundError`:** ejecuta `uv sync --locked` y arranca con `uv run`,
  para utilizar el entorno del proyecto.
- **Puerto ocupado:** sustituye `8001` por otro puerto libre en el comando y la URL.
- **Clave o modelo no válidos:** revisa `.env`, guárdalo y reinicia el servidor.
  `/health` solo comprueba la aplicación; una generación real verifica el proveedor.
- **Respuesta incompleta o límite de API:** la web muestra el error; la transcripción
  guardada y las versiones anteriores siguen disponibles para volver a intentarlo.
