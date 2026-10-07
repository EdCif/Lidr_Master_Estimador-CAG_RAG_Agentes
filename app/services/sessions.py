"""Sesiones conversacionales: el historial y la memoria, cada uno por su lado.

La idea clave de este módulo
----------------------------
Hasta la sesión 4, cada estimación era una petición suelta: llegaba una
transcripción, se estimaba y se olvidaba. Ahora queremos que el cliente pueda
afinar la estimación en varios turnos ("quitad la app móvil", "suponed un
equipo de tres personas") y que el sistema se acuerde de lo que se ha dicho.

Para eso guardamos DOS cosas distintas por cada conversación:

* El HISTORIAL (``ConversationHistory``). Son los mensajes tal cual, user y
  assistant, con el formato que espera la API del LLM. La API no recuerda
  nada entre llamadas, así que el historial viaja entero en cada petición.
  Cada turno que añadimos encarece la siguiente llamada, por eso solo nos
  quedamos con los últimos N turnos (ventana deslizante) y tiramos los viejos.

* La MEMORIA (``ProjectMetadata``). Es una ficha corta con los hechos del
  proyecto que no queremos perder aunque el turno en el que se dijeron ya
  haya salido de la ventana: nombre, tamaño del equipo, tecnologías y alcance
  acordado. No viaja como mensajes sino dentro del system prompt, y ocupa
  unas pocas líneas en lugar de todo el texto de la conversación.

Las dos piezas viven dentro de una ``Session``, y las sesiones se guardan en
un ``SessionStore``, que por dentro es un diccionario de Python indexado por
``session_id``.

Por qué en memoria del proceso y no en una base de datos
--------------------------------------------------------
Es una decisión pensada para esta fase del máster, no un descuido:

* Lo que queremos aprender aquí es la diferencia entre historial y memoria.
  Un diccionario se entiende de un vistazo y no añade nada que distraiga.
* Lo que aceptamos perder: si se reinicia uvicorn (también cuando ``--reload``
  detecta que has guardado un fichero) todas las sesiones desaparecen. Y si
  arrancáramos varios workers, cada uno tendría su propio diccionario y no
  verían las sesiones de los demás. Para un ejercicio en local con un solo
  proceso nos vale: el cliente pide una sesión nueva y sigue trabajando.
* Cuando haga falta que las sesiones sobrevivan (producción, varios
  procesos), bastará con cambiar el ``SessionStore`` por otro con los mismos
  métodos (``create``, ``get``, ``delete``) que guarde en Redis o en una base
  de datos. El resto del código no tendría que enterarse.
"""

from __future__ import annotations

import copy
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Constantes y tipos
# ---------------------------------------------------------------------------

# Un turno es una pareja de mensajes: lo que pregunta el usuario y lo que
# contesta el asistente. Con 6 turnos el modelo ve las últimas seis idas y
# vueltas, que para ir afinando una estimación suele sobrar. En el Paso 5 lo
# haremos configurable desde el .env.
DEFAULT_MAX_TURNS = 6

# Número máximo de sesiones vivas a la vez. Sin un tope, alguien que creara
# sesiones en bucle iría llenando la RAM del proceso hasta tumbarlo. Cuando se
# supera, se borra la sesión que lleva más tiempo sin usarse.
DEFAULT_MAX_SESSIONS = 500

# El contenido de un mensaje puede ser un texto o una lista de bloques
# (texto + documentos). La lista es el formato que usaremos en el Camino A
# para adjuntar PDFs con la Files API de Anthropic. Aceptamos los dos desde
# ya para no tener que volver a tocar este módulo cuando lleguen los adjuntos.
MessageContent = str | list[dict[str, Any]]

# Un mensaje con el formato de la API: {"role": "...", "content": ...}.
Message = dict[str, Any]

# Función que recibe la ficha del proyecto y devuelve el system prompt ya
# montado. La define quien crea las sesiones (en el Paso 4 será la plantilla
# Jinja2); este módulo no necesita saber cómo se escribe el prompt.
SystemPromptBuilder = Callable[["ProjectMetadata"], str]


# ---------------------------------------------------------------------------
# 1. La memoria: ficha con los hechos del proyecto
# ---------------------------------------------------------------------------

class ProjectMetadata(BaseModel):
    """Ficha con los hechos del proyecto que el sistema tiene que recordar.

    Es la "memoria" de la conversación. El historial guarda las palabras
    exactas de cada turno; aquí solo apuntamos conclusiones: cómo se llama el
    proyecto, con cuánta gente contamos, qué tecnologías han salido y qué
    alcance se ha pactado. Todo empieza vacío porque en el primer turno
    todavía no sabemos nada.

    Quien la rellena es el extractor del Paso 4: después de cada respuesta,
    una segunda llamada al LLM lee el turno y devuelve estos campos en JSON.
    Por eso la validación es tolerante: ignora claves que no conoce, limpia
    espacios, quita tecnologías repetidas y, si el tamaño de equipo no es un
    número válido, lo deja como "no lo sabemos". Preferimos quedarnos con lo
    aprovechable de un JSON imperfecto antes que perder el turno entero por un
    detalle.
    """

    model_config = ConfigDict(
        # Si el extractor se inventa un campo que no existe, lo descartamos
        # sin fallar.
        extra="ignore",
        # "  Python " se guarda como "Python".
        str_strip_whitespace=True,
        # La validación también se aplica al asignar: metadata.campo = valor.
        validate_assignment=True,
    )

    project_name: str | None = Field(
        default=None,
        description="Nombre con el que el cliente se refiere al proyecto.",
    )
    assumed_team_size: int | None = Field(
        default=None,
        description="Personas del equipo que damos por supuestas al estimar.",
    )
    mentioned_technologies: list[str] = Field(
        default_factory=list,
        description="Tecnologías, lenguajes o servicios mencionados hasta ahora.",
    )
    agreed_scope: str | None = Field(
        default=None,
        description="Alcance acordado, en texto libre: qué entra y qué queda fuera.",
    )

    @field_validator("project_name", "agreed_scope", mode="before")
    @classmethod
    def blank_text_is_none(cls, value: object) -> object:
        """Un texto vacío ("" o solo espacios) significa que no lo sabemos."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("assumed_team_size", mode="before")
    @classmethod
    def team_size_or_none(cls, value: object) -> int | None:
        """Nos quedamos con un entero positivo; si no lo hay, con None.

        El extractor a veces devuelve "3" (como texto), 3.0 o 0. Los dos
        primeros los convertimos a 3. Un 0, un negativo o algo como "unas
        cuantas personas" no nos dicen nada útil, así que lo tratamos igual
        que si no se hubiera mencionado.
        """
        # bool es un subtipo de int en Python (True == 1): lo descartamos
        # aparte para que un True no acabe convertido en "equipo de 1".
        if value is None or isinstance(value, bool):
            return None
        try:
            size = int(float(value))  # float() admite "3", "3.0" y 3.0
        except (TypeError, ValueError, OverflowError):
            # OverflowError: float("inf") existe, pero no cabe en un int.
            return None
        return size if size >= 1 else None

    @field_validator("mentioned_technologies", mode="before")
    @classmethod
    def clean_technologies(cls, value: object) -> list[str]:
        """Limpia la lista de tecnologías y quita las repetidas.

        Admite una lista (lo normal) o un texto separado por comas
        ("Python, React"), por si el extractor se equivoca de formato. Las
        repetidas se comparan sin distinguir mayúsculas ("react" y "React"
        son la misma) y se conserva la primera forma en que aparecieron.
        """
        if value is None:
            return []
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple, set)):
            return []

        clean: list[str] = []
        seen: set[str] = set()
        for item in value:
            name = str(item).strip()
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                clean.append(name)
        return clean

    def is_empty(self) -> bool:
        """True mientras no sepamos nada del proyecto (primer turno de la sesión).

        La plantilla del system prompt lo usará en el Paso 4 para dejar vacío
        el bloque <project_metadata> en lugar de pintar una ficha llena de
        "None". Recorremos todos los campos con model_dump() para que, si
        mañana añadimos uno nuevo, esta comprobación siga funcionando sin
        tocarla.
        """
        return not any(self.model_dump().values())


# ---------------------------------------------------------------------------
# 2. El historial: mensajes con ventana deslizante
# ---------------------------------------------------------------------------

class ConversationHistory:
    """Historial de mensajes de una sesión, limitado a los últimos N turnos.

    Cómo funciona la ventana
    ------------------------
    Guardamos los mensajes en una lista, con el mismo formato que espera la
    API: ``{"role": "user" | "assistant", "content": ...}``. Van siempre por
    parejas (un turno = pregunta + respuesta). Cuando hay más turnos de los
    permitidos, borramos las parejas más antiguas. Nunca borramos medio turno:
    una respuesta sin su pregunta delante confundiría al modelo, y algunas
    APIs directamente rechazan un historial que empieza por "assistant".

    ¿Y el system prompt?
    --------------------
    No está en la lista, y es a propósito. El system prompt incluye la
    memoria (project_metadata), que cambia después de cada turno; si lo
    guardáramos como un mensaje más, se quedaría desfasado enseguida. En su
    lugar, el historial recibe una función que sabe generarlo y
    ``to_messages_list()`` la llama cada vez. Así se cumple el invariante: el
    system prompt va siempre el primero, nunca lo tira la ventana y siempre
    está al día.

    ¿Por qué un turno solo se guarda cuando está completo?
    -------------------------------------------------------
    La pregunta del usuario no se apunta en cuanto llega, sino junto con la
    respuesta cuando el LLM ha terminado (``add_turn``). Si la llamada falla,
    o el usuario corta el streaming a mitad, no queda una pregunta huérfana
    en el historial: es como si ese turno no hubiera pasado y se puede
    repetir sin más.
    """

    def __init__(
        self,
        system_prompt_factory: Callable[[], str],
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        if max_turns < 1:
            raise ValueError("max_turns tiene que ser al menos 1: sin turnos no hay conversación.")
        self.max_turns = max_turns
        self._system_prompt_factory = system_prompt_factory
        # Solo mensajes user/assistant, alternados y por parejas. El system
        # prompt no se guarda aquí (ver el docstring de la clase).
        self._messages: list[Message] = []
        # Cuántos turnos ha tirado ya la ventana. No influye en la estimación;
        # lo usaremos para enseñarlo en Streamlit y para comprobarlo en tests.
        self.discarded_turns = 0

    @property
    def turn_count(self) -> int:
        """Turnos completos que hay ahora mismo dentro de la ventana."""
        return len(self._messages) // 2

    @property
    def messages(self) -> list[Message]:
        """Copia de los mensajes guardados, sin el system prompt.

        Devolvemos una copia para que nadie pueda modificar el historial "por
        la puerta de atrás" (por ejemplo, haciendo .append() sobre lo que le
        devolvemos) y saltarse así la ventana.
        """
        return copy.deepcopy(self._messages)

    def add_turn(self, user_content: MessageContent, assistant_content: str) -> None:
        """Apunta un turno completo (pregunta + respuesta) y aplica la ventana."""
        self._messages.append({"role": "user", "content": user_content})
        self._messages.append({"role": "assistant", "content": assistant_content})
        self._apply_window()

    def _apply_window(self) -> None:
        """Si sobran turnos, borra las parejas más antiguas."""
        excess = self.turn_count - self.max_turns
        if excess > 0:
            # Cada turno son dos mensajes, por eso borramos 2 * excess desde
            # el principio de la lista.
            del self._messages[: 2 * excess]
            self.discarded_turns += excess

    def to_messages_list(self, pending_user_content: MessageContent | None = None) -> list[Message]:
        """Devuelve el array ``messages`` listo para pasárselo a la API del LLM.

        El orden es siempre el mismo:

        1. El system prompt, generado en este momento con la ficha actual.
        2. Los turnos anteriores que caben en la ventana.
        3. Si se indica ``pending_user_content``, la pregunta nueva del usuario.

        La pregunta nueva cuenta como un turno más. Con una ventana de 6 se
        envían los 5 turnos anteriores y la pregunta actual, de forma que el
        modelo nunca ve más de ``max_turns`` turnos en una misma llamada.

        El formato es el de OpenAI/litellm (el system va dentro de la lista).
        La API nativa de Anthropic lo quiere aparte, en el parámetro
        ``system``; el Camino A se encargará de separarlo.
        """
        messages: list[Message] = [{"role": "system", "content": self._system_prompt_factory()}]

        if pending_user_content is None:
            messages.extend(self.messages)
            return messages

        previous_turns = self.max_turns - 1
        # Ojo con max_turns = 1: previous_turns vale 0 y lista[-0:] devolvería
        # la lista ENTERA, no una vacía. Por eso solo cortamos si hay hueco.
        if previous_turns:
            messages.extend(self.messages[-2 * previous_turns:])
        messages.append({"role": "user", "content": pending_user_content})
        return messages


# ---------------------------------------------------------------------------
# 3. La sesión: historial + memoria de una misma conversación
# ---------------------------------------------------------------------------

class Session:
    """Una conversación con un cliente: su historial y su ficha de proyecto.

    El historial no sabe nada de la ficha. Lo único que recibe es una función
    que, cada vez que se la llama, monta el system prompt con la ficha que
    haya EN ESE MOMENTO. El ``lambda`` lee ``self.metadata`` cuando se
    ejecuta, no cuando se crea, así que el prompt sale al día aunque
    sustituyamos la ficha entera por otra nueva tras un turno.
    """

    def __init__(
        self,
        session_id: str,
        system_prompt_builder: SystemPromptBuilder,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self.session_id = session_id
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.metadata = ProjectMetadata()
        self.history = ConversationHistory(
            system_prompt_factory=lambda: system_prompt_builder(self.metadata),
            max_turns=max_turns,
        )

    def touch(self) -> None:
        """Marca la sesión como usada ahora mismo."""
        self.updated_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 4. El almacén: todas las sesiones vivas, en un diccionario
# ---------------------------------------------------------------------------

class SessionNotFoundError(KeyError):
    """El session_id no existe.

    Puede que nunca se creara, que se perdiera al reiniciar el servidor o que
    se borrara por el tope de sesiones. En el endpoint lo traduciremos a un
    404 para que el cliente sepa que tiene que pedir una sesión nueva.
    """


class SessionStore:
    """Las sesiones vivas, guardadas en un diccionario en memoria del proceso.

    Usamos un ``OrderedDict``, que es un diccionario que además recuerda el
    orden. Cada vez que se usa una sesión la movemos al final, de modo que la
    primera es siempre la que lleva más tiempo sin usarse. Cuando superamos
    ``max_sessions``, borramos esa primera. Es la política LRU (least recently
    used, "la menos usada recientemente").

    Volatilidad: todo vive en la RAM del proceso de uvicorn. Al reiniciar se
    pierde, y con varios workers cada uno tendría su propio almacén. El
    porqué de aceptarlo en esta fase está explicado al principio del módulo.
    """

    def __init__(
        self,
        system_prompt_builder: SystemPromptBuilder,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions tiene que ser al menos 1.")
        self._system_prompt_builder = system_prompt_builder
        self.max_turns = max_turns
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        # FastAPI atiende los endpoints síncronos desde varios hilos a la vez.
        # El candado evita que dos peticiones simultáneas pisen el diccionario
        # mientras uno añade una sesión y otro reordena o borra.
        self._lock = threading.Lock()

    def create(self) -> Session:
        """Crea una sesión vacía con un identificador UUID v4 y la guarda.

        UUID v4 son 122 bits aleatorios: no se pueden adivinar ni chocan en
        la práctica, así que nadie puede colarse en la sesión de otro
        probando números seguidos.
        """
        session = Session(
            session_id=str(uuid.uuid4()),
            system_prompt_builder=self._system_prompt_builder,
            max_turns=self.max_turns,
        )
        with self._lock:
            self._sessions[session.session_id] = session
            if len(self._sessions) > self.max_sessions:
                # last=False saca el primer elemento: el que más tiempo lleva
                # sin usarse.
                self._sessions.popitem(last=False)
        return session

    def get(self, session_id: str) -> Session:
        """Devuelve la sesión o lanza ``SessionNotFoundError`` si no existe."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise SessionNotFoundError(session_id)
            # Recién usada: la pasamos al final de la cola de borrado.
            self._sessions.move_to_end(session_id)
        session.touch()
        return session

    def delete(self, session_id: str) -> None:
        """Borra la sesión. Si ya no existía, no pasa nada."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)
