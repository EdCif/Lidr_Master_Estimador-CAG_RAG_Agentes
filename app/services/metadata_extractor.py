"""Extractor heurístico: actualiza la memoria del proyecto después de cada turno.

Qué es "heurístico"
-------------------
Que no usa ningún modelo de IA: son reglas sencillas escritas a mano que
buscan frases conocidas en el texto ("el proyecto se llama...", "un equipo
de 3 personas", "tecnologías: React, FastAPI"). Para buscarlas usamos
expresiones regulares (el módulo ``re`` de Python): patrones de texto con
huecos. Cada patrón de este fichero lleva un comentario con una frase de
ejemplo que reconoce.

Ventajas frente a una segunda llamada al LLM: es gratis, instantáneo y
siempre da el mismo resultado. Inconveniente: solo entiende las frases para
las que tiene regla.

De dónde saca los hechos
------------------------
1. Del bloque ``<hechos_del_proyecto>`` con el que el modelo cierra cada
   respuesta (se lo pide la plantilla ``app/prompts/estimator_system_v1.j2``).
   Es la fuente que más hechos aporta: el modelo ya ha leído la
   transcripción, los adjuntos y los turnos anteriores, y resume ahí cómo
   queda el proyecto. Al tener un formato fijo ("Proyecto: ...", "Equipo
   asumido: ..."), leerlo con reglas es fiable.
2. De la transcripción del cliente, con frases típicas en español. Lo que
   el cliente dice de forma explícita en ESTE turno es la decisión más
   reciente, así que tiene la última palabra: si dice "equipo de 4
   personas" y el bloque del modelo dice 3, se queda el 4.

En la respuesta del modelo NO buscamos tecnologías sueltas por el texto: el
modelo menciona muchas como sugerencia ("podríais usar Redis") y llenaríamos
la ficha de cosas que nadie ha decidido.

Cómo se combina con lo que ya sabíamos
--------------------------------------
``merge_metadata``: un dato nuevo sustituye al anterior (si el cliente
cambia el equipo de 3 a 4 personas, gana el 4); las tecnologías se suman; y
un hecho nunca se borra porque no se repita en el turno siguiente. Límite
conocido: si el cliente descarta una tecnología, la ficha la conserva.
"""

from __future__ import annotations

import re

from app.services.sessions import ProjectMetadata

# ---------------------------------------------------------------------------
# Piezas comunes
# ---------------------------------------------------------------------------

# Valores que significan "todavía no se sabe" y que, por tanto, no deben
# sobrescribir lo que ya tuviéramos. Incluye los textos de ejemplo de la
# plantilla, por si el modelo los copiara tal cual.
_UNKNOWN_VALUES = {
    "", "-", "—", "sin definir", "no definido", "desconocido", "n/a", "ninguno", "ninguna",
    "nombre del proyecto", "número de personas", "tecnologías separadas por comas",
    "una frase con el alcance pactado",
}

# Números escritos con letras que aparecen al hablar de equipos.
_NUMBER_WORDS = {
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11,
    "doce": 12, "quince": 15, "veinte": 20,
}
_NUMBER = r"(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")"

# Tecnologías que reconocemos por su nombre en la transcripción del cliente.
# La clave es cómo se busca (en minúsculas) y el valor, cómo se guarda.
_KNOWN_TECHNOLOGIES = {
    "react": "React", "angular": "Angular", "vue": "Vue", "next.js": "Next.js",
    "typescript": "TypeScript", "javascript": "JavaScript", "node.js": "Node.js",
    "python": "Python", "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "java": "Java", "spring": "Spring", ".net": ".NET", "c#": "C#", "php": "PHP",
    "laravel": "Laravel", "kotlin": "Kotlin", "swift": "Swift", "flutter": "Flutter",
    "react native": "React Native", "postgresql": "PostgreSQL", "postgres": "PostgreSQL",
    "mysql": "MySQL", "sql server": "SQL Server", "mongodb": "MongoDB", "redis": "Redis",
    "kafka": "Kafka", "rabbitmq": "RabbitMQ", "celery": "Celery", "docker": "Docker",
    "kubernetes": "Kubernetes", "aws": "AWS", "azure": "Azure", "gcp": "GCP",
    "salesforce": "Salesforce", "sap": "SAP", "power bi": "Power BI",
}


def _clean(value: str | None) -> str | None:
    """Limpia un valor: quita espacios, comillas y punto final; "sin definir" → None."""
    if value is None:
        return None
    value = value.strip().strip("«»\"“”'").strip().rstrip(".").strip()
    return None if value.casefold() in _UNKNOWN_VALUES else value


def _to_int(word: str | None) -> int | None:
    """Convierte "3" o "tres" en 3. Si no es un número, None."""
    if not word:
        return None
    word = word.strip().casefold()
    if word.isdigit():
        return int(word)
    return _NUMBER_WORDS.get(word)


def _split_list(value: str | None) -> list[str]:
    """Convierte "React, FastAPI y PostgreSQL" en ["React", "FastAPI", "PostgreSQL"]."""
    if not value:
        return []
    parts = re.split(r",|;|\s+y\s+|\s+e\s+", value)
    return [part for part in (_clean(p) for p in parts) if part]


# ---------------------------------------------------------------------------
# 1. El bloque <hechos_del_proyecto> de la respuesta del modelo
# ---------------------------------------------------------------------------

# Captura lo que hay entre <hechos_del_proyecto> y </hechos_del_proyecto>.
# Si el modelo lo escribiera dos veces, nos quedamos con el último.
_FACTS_BLOCK = re.compile(r"<hechos_del_proyecto>(.*?)</hechos_del_proyecto>", re.S | re.I)

# Una línea "Etiqueta: valor" dentro del bloque. Ejemplo: "Equipo asumido: 3 personas".
_FACT_LINE = re.compile(r"^\s*[-*]?\s*(?P<label>[^:\n]+?)\s*:\s*(?P<value>.*?)\s*$", re.M)


def facts_from_reply(reply: str) -> ProjectMetadata:
    """Lee el bloque <hechos_del_proyecto> de una respuesta. Sin bloque, ficha vacía."""
    blocks = _FACTS_BLOCK.findall(reply)
    if not blocks:
        return ProjectMetadata()

    found: dict[str, object] = {}
    for match in _FACT_LINE.finditer(blocks[-1]):
        label = match.group("label").casefold()
        value = _clean(match.group("value"))
        if value is None:
            continue
        if label.startswith("proyecto"):
            found["project_name"] = value
        elif label.startswith("equipo"):
            # "3 personas", "tres desarrolladores"... nos quedamos con el número.
            number = re.search(_NUMBER, value, re.I)
            found["assumed_team_size"] = _to_int(number.group(1)) if number else None
        elif label.startswith("tecnolog"):
            found["mentioned_technologies"] = _split_list(value)
        elif label.startswith("alcance"):
            found["agreed_scope"] = value
    return ProjectMetadata(**found)


# ---------------------------------------------------------------------------
# 2. Frases típicas en la transcripción del cliente
# ---------------------------------------------------------------------------

# Nombre del proyecto. Reconoce, por ejemplo:
#   "el proyecto se llama Portal de Reservas."
#   "el proyecto se llamará «Reservas 360»"
#   "Proyecto: Portal de Reservas" (al principio de una línea)
# Sin comillas, el nombre acaba en el primer signo de puntuación o antes de
# palabras como "y", "que" o "para": en "se llama Portal y queremos...",
# el nombre es solo "Portal".
_PROJECT_NAME = [
    re.compile(
        r"proyecto se llama(?:rá)?\s+"
        r"(?P<v>[«\"“][^»\"”\n]+[»\"”]|[^.\n,;:]+?)(?=\s+(?:y|que|para|con|porque)\b|[.\n,;:]|$)",
        re.I,
    ),
    re.compile(r"^\s*proyecto\s*:\s*(?P<v>[^\n]+)$", re.I | re.M),
]

# Tamaño del equipo. Reconoce, por ejemplo:
#   "un equipo de 3 personas", "suponed un equipo de tres desarrolladores"
#   "contamos con 4 desarrolladores"
_TEAM_SIZE = [
    re.compile(r"equipo de\s+" + _NUMBER + r"\s+(?:personas|desarrolladores|devs|programadores|ingenieros)", re.I),
    re.compile(r"\b" + _NUMBER + r"\s+(?:desarrolladores|programadores|ingenieros)\b", re.I),
]

# Lista de tecnologías dicha de forma explícita. Reconoce, por ejemplo:
#   "las tecnologías usadas son React y FastAPI."
#   "Tecnologías: Java, Spring, Oracle"
#   "el stack será Vue, Node.js y MongoDB"
# Detrás tiene que venir ":" o un verbo ("son", "será"...). Así una frase
# como "no sabemos qué tecnologías usar" no apunta "usar" como tecnología.
_TECH_LIST = re.compile(
    r"(?:tecnolog[ií]as?(?:\s+(?:usadas|utilizadas|que usaremos))?|stack)"
    r"(?:\s*:\s*|\s+(?:son|será|serán|es)\s+)(?P<v>[^.\n]+)",
    re.I,
)

# Alcance pactado. Reconoce, por ejemplo:
#   "Alcance acordado: solo la web, sin app móvil"
#   "hemos acordado que la primera fase no incluye pagos"
_SCOPE = [
    re.compile(r"alcance acordado\s*:?\s*(?P<v>[^\n]+)", re.I),
    re.compile(r"(?:hemos acordado|acordamos|quedamos en)\s+que\s+(?P<v>[^.\n]+)", re.I),
]


def _first(patterns: list[re.Pattern[str]], text: str) -> str | None:
    """Valor del primer patrón que encuentre algo en el texto."""
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return _clean(match.groups()[0] if "v" not in pattern.groupindex else match.group("v"))
    return None


def facts_from_transcript(transcript: str) -> ProjectMetadata:
    """Saca los hechos que el cliente dice de forma explícita en su mensaje."""
    technologies = _split_list(match.group("v")) if (match := _TECH_LIST.search(transcript)) else []
    # Además de la lista explícita, cualquier tecnología conocida que nombre.
    lowered = transcript.casefold()
    for key, name in _KNOWN_TECHNOLOGIES.items():
        if re.search(r"(?<![\w.])" + re.escape(key) + r"(?![\w])", lowered):
            technologies.append(name)

    return ProjectMetadata(
        project_name=_first(_PROJECT_NAME, transcript),
        assumed_team_size=_to_int(_first(_TEAM_SIZE, transcript)),
        mentioned_technologies=technologies,
        agreed_scope=_first(_SCOPE, transcript),
    )


# ---------------------------------------------------------------------------
# 3. Combinar con lo que ya sabíamos
# ---------------------------------------------------------------------------

def merge_metadata(current: ProjectMetadata, new: ProjectMetadata) -> ProjectMetadata:
    """Funde la ficha que teníamos con los hechos nuevos.

    * Nombre, equipo y alcance: si hay un valor nuevo, sustituye al anterior;
      si no, se mantiene el que había.
    * Tecnologías: se suman. ProjectMetadata ya quita las repetidas sin
      distinguir mayúsculas ("react" y "React" cuentan como una).
    """
    return ProjectMetadata(
        project_name=new.project_name or current.project_name,
        assumed_team_size=new.assumed_team_size or current.assumed_team_size,
        mentioned_technologies=current.mentioned_technologies + new.mentioned_technologies,
        agreed_scope=new.agreed_scope or current.agreed_scope,
    )


def update_metadata_from_turn(current: ProjectMetadata, transcript: str, reply: str) -> ProjectMetadata:
    """Lo que se llama al terminar cada turno: ficha anterior + hechos nuevos.

    Orden: primero el bloque del modelo (resume el proyecto con todo en
    cuenta, adjuntos incluidos) y después lo que el cliente ha dicho de forma
    explícita en este turno, que es la decisión más reciente y gana.
    """
    with_model_facts = merge_metadata(current, facts_from_reply(reply))
    return merge_metadata(with_model_facts, facts_from_transcript(transcript))
