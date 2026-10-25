"""Tres conversaciones sintéticas de 20 turnos para el stress test (sesión 6).

Qué son
-------
Guiones de conversación entre un cliente y el estimador, siempre sobre el
mismo proyecto (Nimbus, un portal de reservas de salas). El runner
(``run.py``) los manda turno a turno a la API, como lo haría un cliente de
verdad. Cada perfil está pensado para provocar un comportamiento distinto:

* ``growing`` · El proyecto que crece: en cada turno se añade un requisito
  coherente. Mide la curva de coste y si el nombre del proyecto sobrevive
  hasta el turno 20.
* ``pivot`` · El proyecto que cambia de rumbo: en el turno 5 la interfaz
  pasa de React a Flutter. Mide si la memoria se actualiza o acumula las
  dos tecnologías.
* ``contradiction`` · El proyecto que se contradice: turno 3, presupuesto de
  30.000 €; turno 8, 80.000 € y el equipo pasa de 3 a 5 personas. Mide cuál
  de los dos datos se conserva.

El "fact-tracker": qué hay que recordar
---------------------------------------
Cada turno declara qué hechos deberían seguir vivos en los turnos
siguientes (``Fact``). El enunciado lo pide como una lista de tuplas
``(turn_index, transcript, fact_to_remember)``; aquí cada ``Turn`` es
exactamente esa tupla, con la diferencia de que un turno puede plantar
varios hechos (o ninguno).

Un hecho dice qué texto buscar y dónde (la memoria, el historial o la
última respuesta). En cada turno N, el runner comprueba con
``MemoryDriftMetric`` todos los hechos plantados en turnos ANTERIORES
(k < N), como pide el enunciado.

Dos detalles que hacen falta para medir bien:

* ``expected=False``: tras "descartad React", lo que queremos comprobar es
  que React HA DESAPARECIDO.
* ``valid_until``: un hecho que el propio cliente contradice después deja de
  comprobarse. Tras pasar de 30.000 € a 80.000 €, recordar 30.000 € ya no es
  "recordar bien".

Cómo están escritas las frases
------------------------------
La memoria (``project_metadata``) la rellena el extractor heurístico de la
sesión 5 (``app/services/metadata_extractor.py``) con reglas sobre frases
concretas. Para que los hechos de la ficha se puedan medir, las
transcripciones usan esas frases: "El proyecto se llama Nimbus.", "un
equipo de 3 personas", "Las tecnologías serán React, FastAPI y PostgreSQL",
"Hemos acordado que...", y tecnologías de su lista conocida (Azure, Power
BI, Celery, Kubernetes, Flutter...). El test ``tests/test_stress_scenarios.py``
comprueba que siguen reconociéndose.

Y no se repiten: un hecho dicho en el turno 1 no vuelve a aparecer en las
transcripciones siguientes. Si el cliente lo repitiera, el sistema lo
"recordaría" porque se lo acaban de decir y la medición no serviría.

El presupuesto no tiene sitio en la ficha (no hay campo para él): solo vive
en el historial y en las respuestas del modelo. Lo dejamos así a propósito
("el ejercicio mide, no optimiza"): es justo lo que queremos ver perderse.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from evals.stress.metrics import LOCATIONS, Location, MemoryDriftMetric

# Turnos de cada conversación y en cuáles mira el informe la deriva (N).
TURNS_PER_SCENARIO = 20
CHECKPOINTS = (1, 3, 6, 10, 20)

# Atajos para declarar dónde se busca un hecho.
EVERYWHERE: tuple[Location, ...] = LOCATIONS          # memoria, historial y respuesta
MEMORY: tuple[Location, ...] = ("metadata",)          # solo la ficha del proyecto
CONVERSATION: tuple[Location, ...] = ("history", "reply")  # lo que no cabe en la ficha
REPLY: tuple[Location, ...] = ("reply",)              # lo que usa el modelo al contestar


@dataclass(frozen=True)
class Fact:
    """Un hecho que los turnos siguientes deberían recordar (u olvidar).

    * ``label``: nombre corto y único dentro del escenario; es el que sale en
      el CSV ("project_name=1;budget_30k=0").
    * ``text``: lo que se busca, como palabra completa y sin distinguir
      mayúsculas. Para la memoria se usa el texto tal como sale en la ficha
      ("equipo asumido: 3 personas").
    * ``where``: en qué sitios se busca (ver ``MemoryDriftMetric``).
    * ``expected``: True = debería estar; False = debería haber desaparecido.
    * ``valid_until``: último turno en el que se comprueba (None = hasta el
      final). Sirve para los hechos que el cliente contradice más tarde.
    """

    label: str
    text: str
    where: tuple[Location, ...] = EVERYWHERE
    expected: bool = True
    valid_until: int | None = None

    def metric(self) -> MemoryDriftMetric:
        return MemoryDriftMetric(self.text, where=self.where, expected=self.expected)


class Turn(NamedTuple):
    """Un turno del guion: la tupla (turn_index, transcript, hechos) del enunciado."""

    turn_index: int
    transcript: str
    facts_to_remember: tuple[Fact, ...] = ()


@dataclass(frozen=True)
class Scenario:
    """Una conversación completa: su nombre, qué mide y sus turnos."""

    name: str
    description: str
    turns: tuple[Turn, ...]

    def facts_to_check(self, at_turn: int) -> list[tuple[int, Fact]]:
        """Los hechos que hay que comprobar después del turno ``at_turn``.

        Son los plantados en turnos anteriores (k < N) que siguen vigentes
        (``valid_until`` no ha pasado). Devuelve (turno en que se plantó, hecho).

        Ejemplo, en ``contradiction``: tras el turno 5 se comprueban "nimbus",
        el equipo de 3 y los 30.000 €; tras el turno 9, "nimbus", el equipo de
        5, los 80.000 € y que los 30.000 € y el equipo de 3 ya no estén.
        """
        return [
            (turn.turn_index, fact)
            for turn in self.turns
            if turn.turn_index < at_turn
            for fact in turn.facts_to_remember
            if fact.valid_until is None or at_turn <= fact.valid_until
        ]


# ---------------------------------------------------------------------------
# El turno 1, común a los tres: la reunión de arranque
# ---------------------------------------------------------------------------
# Versión corta de docs/transcripcion_reunion.md, con el nombre, el equipo y
# las tecnologías dichos de forma explícita para que los recoja la memoria.
OPENING = (
    "Reunión de arranque. El proyecto se llama Nimbus: un portal web de reservas de "
    "salas para una empresa con ocho salas en una única sede. Contamos con un equipo de "
    "3 personas. Las tecnologías serán React, FastAPI y PostgreSQL. En la primera versión "
    "queremos consultar la disponibilidad por día y sala, reservar una franja horaria y "
    "ver las reservas propias, desde el ordenador y desde el navegador del móvil. "
    "Necesitamos una primera estimación de horas."
)

PROJECT_NAME = Fact("project_name", "nimbus")
TEAM_OF_3 = Fact("team_3", "equipo asumido: 3 personas", MEMORY)

RECAP = (
    "Recapitulemos antes de cerrar: dadnos la estimación completa y actualizada con todo "
    "lo acordado hasta ahora, empezando por el nombre del proyecto."
)


# ---------------------------------------------------------------------------
# 1. growing · El proyecto que crece
# ---------------------------------------------------------------------------
GROWING = Scenario(
    name="growing",
    description=(
        "Cada turno añade un requisito coherente. Mide la curva de coste y si el nombre "
        "del proyecto, dicho solo en el turno 1, sobrevive hasta el turno 20."
    ),
    turns=(
        Turn(1, OPENING, (PROJECT_NAME, TEAM_OF_3, Fact("postgresql", "postgresql", MEMORY))),
        Turn(2, "Los usuarios entrarán con su cuenta corporativa mediante inicio de sesión "
                "único con Azure AD.", (Fact("azure", "azure", MEMORY),)),
        Turn(3, "Hemos acordado que la primera fase no incluye pagos ni facturación.",
             (Fact("scope_no_payments", "no incluye pagos", MEMORY),)),
        Turn(4, "Queremos sincronizar cada reserva con Google Calendar, en los dos sentidos.",
             (Fact("google_calendar", "google calendar", CONVERSATION),)),
        Turn(5, "Añadid un panel de uso de las salas hecho con Power BI.",
             (Fact("power_bi", "power bi", MEMORY),)),
        Turn(6, "La empresa abre otra sede en Bilbao: el sistema tiene que ser multisede, con "
                "salas y administradores propios en cada una.",
             (Fact("multisede", "multisede", CONVERSATION),)),
        Turn(7, "Necesitamos un registro de auditoría de quién reserva, cambia o cancela cada "
                "sala.", (Fact("audit_log", "registro de auditoría", CONVERSATION),)),
        Turn(8, "Añadid la opción de exportar las reservas a CSV.",
             (Fact("csv", "csv", CONVERSATION),)),
        Turn(9, "Queremos recordatorios por correo quince minutos antes de cada reserva, "
                "enviados en segundo plano con Celery.", (Fact("celery", "celery", MEMORY),)),
        Turn(10, "Si nadie hace check-in en los diez primeros minutos, la sala se libera sola.",
             (Fact("check_in", "check-in", CONVERSATION),)),
        Turn(11, "Las salas grandes necesitarán la aprobación del responsable de operaciones "
                 "antes de confirmar la reserva.",
             (Fact("approval", "aprobación del responsable", CONVERSATION),)),
        Turn(12, "Añadid reservas recurrentes, semanales y mensuales.",
             (Fact("recurring", "recurrentes", CONVERSATION),)),
        Turn(13, "Cuando una sala esté ocupada, que se pueda apuntar uno a una lista de espera.",
             (Fact("waiting_list", "lista de espera", CONVERSATION),)),
        Turn(14, "En la sede nueva hay tornos en la entrada de cada sala: hay que integrarlos "
                 "con las reservas.", (Fact("turnstiles", "tornos", CONVERSATION),)),
        Turn(15, "Junto a la puerta de cada sala pondremos una tablet que muestre si está libre.",
             (Fact("tablet", "tablet", CONVERSATION),)),
        Turn(16, "Dirección quiere un informe mensual de ocupación por departamento.",
             (Fact("monthly_report", "informe mensual", CONVERSATION),)),
        Turn(17, "Lo desplegaremos en Kubernetes, en nuestra nube privada.",
             (Fact("kubernetes", "kubernetes", MEMORY),)),
        Turn(18, "La web tiene que cumplir las pautas de accesibilidad WCAG 2.1 AA.",
             (Fact("wcag", "wcag", CONVERSATION),)),
        Turn(19, "Y tiene que estar en castellano, euskera e inglés.",
             (Fact("basque", "euskera", CONVERSATION),)),
        Turn(20, RECAP),
    ),
)


# ---------------------------------------------------------------------------
# 2. pivot · El proyecto que cambia de rumbo (React -> Flutter en el turno 5)
# ---------------------------------------------------------------------------
PIVOT = Scenario(
    name="pivot",
    description=(
        "En el turno 5 la interfaz pasa de una web en React a una app en Flutter. Mide si "
        "la memoria se actualiza limpiamente o sigue arrastrando React."
    ),
    turns=(
        Turn(1, OPENING, (
            PROJECT_NAME,
            # React vale hasta el turno 4: en el 5 el cliente lo descarta.
            Fact("react_in_memory", "react", MEMORY, valid_until=4),
            Fact("fastapi", "fastapi", MEMORY),
        )),
        Turn(2, "Los usuarios entrarán con su cuenta corporativa mediante inicio de sesión único.",
             (Fact("sso", "inicio de sesión único", CONVERSATION),)),
        Turn(3, "Queremos ver la ocupación de todas las salas en un calendario semanal.",
             (Fact("weekly_calendar", "calendario semanal", CONVERSATION),)),
        Turn(4, "Las reservas de más de cuatro horas necesitarán aprobación del responsable.",
             (Fact("long_bookings", "más de cuatro horas", CONVERSATION),)),
        Turn(5, "Cambio de planes importante: la interfaz ya no será una web hecha con React, "
                "será una app móvil nativa hecha con Flutter. Descartad React por completo; "
                "el backend sigue igual.", (
            Fact("flutter", "flutter", MEMORY),
            # Lo que queremos ver: ¿React ha salido de la memoria? ¿Y de las respuestas?
            Fact("react_gone_memory", "react", MEMORY, expected=False),
            Fact("react_gone_reply", "react", REPLY, expected=False),
        )),
        Turn(6, "En la app queremos notificaciones push para avisar de cada reserva.",
             (Fact("push", "notificaciones push", CONVERSATION),)),
        Turn(7, "La app tiene que funcionar sin conexión y sincronizar al recuperar la cobertura.",
             (Fact("offline", "sin conexión", CONVERSATION),)),
        Turn(8, "La publicaremos en la App Store y en Google Play.",
             (Fact("stores", "google play", CONVERSATION),)),
        Turn(9, "Para hacer check-in, se escaneará un código QR pegado en la puerta de la sala.",
             (Fact("qr", "qr", CONVERSATION),)),
        Turn(10, "¿Cómo queda la estimación con este nuevo enfoque? Dadnos el total actualizado."),
        Turn(11, "Añadid un modo oscuro en toda la app.",
             (Fact("dark_mode", "modo oscuro", CONVERSATION),)),
        Turn(12, "Queremos un widget en la pantalla de inicio del teléfono con la próxima reserva.",
             (Fact("widget", "widget", CONVERSATION),)),
        Turn(13, "Las reservas tienen que aparecer también en el calendario del teléfono.",
             (Fact("phone_calendar", "calendario del teléfono", CONVERSATION),)),
        Turn(14, "La app tiene que ser usable con lectores de pantalla.",
             (Fact("screen_readers", "lectores de pantalla", CONVERSATION),)),
        Turn(15, "Queremos analítica de uso para saber qué funciones se usan más.",
             (Fact("analytics", "analítica de uso", CONVERSATION),)),
        Turn(16, "El acceso a la app será con huella dactilar o reconocimiento facial.",
             (Fact("biometrics", "huella", CONVERSATION),)),
        Turn(17, "En la recepción de cada sede habrá una versión para tablet de solo consulta.",
             (Fact("reception", "recepción", CONVERSATION),)),
        Turn(18, "Las notificaciones tienen que poder silenciarse fuera del horario laboral.",
             (Fact("quiet_hours", "horario laboral", CONVERSATION),)),
        Turn(19, "Y la app estará en castellano, euskera e inglés.",
             (Fact("basque", "euskera", CONVERSATION),)),
        Turn(20, "Dadnos la estimación completa y actualizada, indicando con qué tecnologías "
                 "se hará la interfaz."),
    ),
)


# ---------------------------------------------------------------------------
# 3. contradiction · El proyecto que se contradice (turno 3 frente a turno 8)
# ---------------------------------------------------------------------------
CONTRADICTION = Scenario(
    name="contradiction",
    description=(
        "Turno 3: presupuesto de 30.000 €. Turno 8: 80.000 € y el equipo pasa de 3 a 5 "
        "personas. Mide cuál se conserva; el presupuesto no tiene sitio en la memoria."
    ),
    turns=(
        Turn(1, OPENING, (
            PROJECT_NAME,
            # El equipo de 3 vale hasta el turno 7: en el 8 pasa a 5 personas.
            Fact("team_3", "equipo asumido: 3 personas", MEMORY, valid_until=7),
        )),
        Turn(2, "Los usuarios entrarán con su cuenta corporativa mediante inicio de sesión único.",
             (Fact("sso", "inicio de sesión único", CONVERSATION),)),
        # "30.000" sin "euros": el modelo puede escribir "30.000 €" o "30.000 euros".
        Turn(3, "Dato importante: el presupuesto aprobado es de 30.000 euros y no se puede "
                "superar.", (Fact("budget_30k", "30.000", CONVERSATION, valid_until=7),)),
        Turn(4, "Queremos ver la ocupación de todas las salas en un calendario semanal.",
             (Fact("weekly_calendar", "calendario semanal", CONVERSATION),)),
        Turn(5, "Añadid un panel con estadísticas de uso de las salas.",
             (Fact("usage_panel", "estadísticas de uso", CONVERSATION),)),
        Turn(6, "Queremos recordatorios por correo antes de cada reserva.",
             (Fact("reminders", "recordatorios por correo", CONVERSATION),)),
        Turn(7, "Añadid la opción de exportar las reservas a Excel.",
             (Fact("excel", "excel", CONVERSATION),)),
        Turn(8, "Novedad: dirección ha ampliado el presupuesto a 80.000 euros y contaremos con "
                "un equipo de 5 personas.", (
            Fact("budget_80k", "80.000", CONVERSATION),
            Fact("team_5", "equipo asumido: 5 personas", MEMORY),
            # Y lo que debería haber desaparecido:
            Fact("budget_30k_gone_reply", "30.000", REPLY, expected=False),
            Fact("team_3_gone_memory", "equipo asumido: 3 personas", MEMORY, expected=False),
        )),
        Turn(9, "Si nadie hace check-in en los diez primeros minutos, la sala se libera sola.",
             (Fact("check_in", "check-in", CONVERSATION),)),
        Turn(10, "Añadid reservas recurrentes, semanales y mensuales.",
             (Fact("recurring", "recurrentes", CONVERSATION),)),
        Turn(11, "Cuando una sala esté ocupada, que se pueda apuntar uno a una lista de espera.",
             (Fact("waiting_list", "lista de espera", CONVERSATION),)),
        Turn(12, "Necesitamos un registro de auditoría de quién reserva, cambia o cancela.",
             (Fact("audit_log", "registro de auditoría", CONVERSATION),)),
        Turn(13, "Junto a la puerta de cada sala pondremos una tablet que muestre si está libre.",
             (Fact("tablet", "tablet", CONVERSATION),)),
        Turn(14, "Dirección quiere un informe mensual de ocupación por departamento.",
             (Fact("monthly_report", "informe mensual", CONVERSATION),)),
        # Pregunta trampa: no repite las cifras. ¿Con cuáles se queda el sistema?
        Turn(15, "Antes de seguir, confirmadnos con qué presupuesto y con qué equipo estáis "
                 "estimando."),
        Turn(16, "La web tiene que cumplir las pautas de accesibilidad WCAG 2.1 AA.",
             (Fact("wcag", "wcag", CONVERSATION),)),
        Turn(17, "Añadid un modo oscuro en toda la web.",
             (Fact("dark_mode", "modo oscuro", CONVERSATION),)),
        Turn(18, "Queremos que se pueda reservar también desde Microsoft Teams.",
             (Fact("teams", "teams", CONVERSATION),)),
        Turn(19, "Y tiene que estar en castellano, euskera e inglés.",
             (Fact("basque", "euskera", CONVERSATION),)),
        Turn(20, "Dadnos la estimación completa y actualizada, ajustada al presupuesto y al "
                 "equipo acordados."),
    ),
)


SCENARIOS: dict[str, Scenario] = {s.name: s for s in (GROWING, PIVOT, CONTRADICTION)}


def get_scenarios(names: Iterable[str]) -> list[Scenario]:
    """Los escenarios pedidos por nombre ("growing,pivot"), en ese orden."""
    wanted = [name.strip() for name in names if name.strip()]
    unknown = [name for name in wanted if name not in SCENARIOS]
    if unknown:
        raise ValueError(f"Escenarios desconocidos: {', '.join(unknown)}. Hay: {', '.join(SCENARIOS)}.")
    return [SCENARIOS[name] for name in wanted]
