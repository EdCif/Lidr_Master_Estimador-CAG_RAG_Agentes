# Stress test del CAG · ¿dónde empieza a romperse?

Sesión 6 del Máster LiDR AI Engineering · proyecto `estimador-cag`.

## Qué se ha medido

El estimador de la sesión 5 tal cual: FastAPI con SSE, historial con ventana
de 6 turnos, memoria `project_metadata` rellenada con expresiones regulares,
adjuntos por el Camino B (texto extraído en local) y **Claude Haiku 5.5** vía
litellm. Sin caché, sin resumen y sin tocar `MAX_TURNS` ni el tope de
adjuntos.

- **Batería:** 3 conversaciones sintéticas de 20 turnos (crece, cambio de
  rumbo, contradicción) × PDF de 0, 5, 20, 50, 100 y 400 KB en el turno 1 ×
  3 repeticiones = 48 conversaciones y 909 turnos. Hay 900 completos y 9
  rechazos: los del PDF de 400 KB, que se rechaza a propósito. No hizo falta
  ningún reintento.
- **Coste y duración:** 7,40 $ en total. Una conversación de 20 turnos pasa
  entre 8 y 16 minutos esperando al modelo (12 minutos de mediana).
- **Presupuestos por turno:** 20 s de latencia y 0,03 $ de coste.
- **Hoja de corrección:** en cada turno se comprueba que lo dicho en turnos
  anteriores sigue en la memoria, el historial o la respuesta, según el dato.
  Se busca como palabra completa y sin distinguir mayúsculas.
- **PDF:** cada uno lleva tres datos escondidos: «SAP» al principio, «12.000
  reservas» en el medio e «ISO 27001» al final. Se buscan en la respuesta de
  cada turno. «KB» quiere decir 1.000 caracteres de texto extraído.
- **Una fila por turno** en [`results.csv`](results.csv). La misma
  información, explicada, está en [`results.xlsx`](results.xlsx).

Se reproduce con la API arrancada con
`SESSION_LLM_MODEL=claude-haiku-5-5` y:

```powershell
uv run python -m evals.stress.run --http http://127.0.0.1:8001 --attachment-sizes 0,5,20,50,100,400 --repeats 3 --concurrency 3
uv run python -m evals.stress.report
```

## Resultados

<!-- tablas:inicio · las genera evals/stress/report.py: no editar a mano -->

_Datos: 909 turnos (9 con error) · modelo claude-haiku-5-5 · ejecución 20261026T185335Z, 20261026T202013Z · generado el 2026-10-26 22:52 UTC._

### 1. Resumen por escenario y tamaño de PDF

_Cómo leerla: una fila por tipo de conversación. Lo importante: la latencia P95 (el peor caso habitual), el coste de una conversación entera y el recuerdo medio._

| Escenario | PDF | Turnos | Con error | Latencia P50 | Latencia P95 | Coste medio por conversación | Caché exacta | Caché semántica | Recuerdo medio (hoja de corrección) | Datos del PDF en el turno 1 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Crece | sin PDF | 60 | 0 | 40,3 s | 69,9 s | 0,1753 $ | 0 % | 0 % | 91 % | — |
| Crece | 5 KB | 60 | 0 | 36,0 s | 70,7 s | 0,1694 $ | 0 % | 0 % | 91 % | 100 % |
| Crece | 20 KB | 60 | 0 | 43,9 s | 80,3 s | 0,2099 $ | 0 % | 0 % | 91 % | 100 % |
| Crece | 50 KB | 60 | 0 | 34,4 s | 66,8 s | 0,1807 $ | 0 % | 0 % | 91 % | 100 % |
| Crece | 100 KB | 60 | 0 | 40,4 s | 70,7 s | 0,2243 $ | 0 % | 0 % | 92 % | 100 % |
| Crece | 400 KB | 3 | 3 | — | — | 0,0000 $ | — | — | — | — |
| Cambio de rumbo | sin PDF | 60 | 0 | 28,3 s | 44,6 s | 0,1206 $ | 0 % | 0 % | 89 % | — |
| Cambio de rumbo | 5 KB | 60 | 0 | 29,2 s | 59,6 s | 0,1371 $ | 0 % | 0 % | 90 % | 100 % |
| Cambio de rumbo | 20 KB | 60 | 0 | 27,4 s | 39,8 s | 0,1264 $ | 0 % | 0 % | 89 % | 100 % |
| Cambio de rumbo | 50 KB | 60 | 0 | 28,6 s | 44,3 s | 0,1420 $ | 0 % | 0 % | 88 % | 100 % |
| Cambio de rumbo | 100 KB | 60 | 0 | 41,1 s | 71,9 s | 0,2100 $ | 0 % | 0 % | 88 % | 100 % |
| Cambio de rumbo | 400 KB | 3 | 3 | — | — | 0,0000 $ | — | — | — | — |
| Contradicción | sin PDF | 60 | 0 | 28,1 s | 48,0 s | 0,1302 $ | 0 % | 0 % | 97 % | — |
| Contradicción | 5 KB | 60 | 0 | 33,5 s | 56,6 s | 0,1540 $ | 0 % | 0 % | 97 % | 100 % |
| Contradicción | 20 KB | 60 | 0 | 32,2 s | 65,6 s | 0,1573 $ | 0 % | 0 % | 98 % | 100 % |
| Contradicción | 50 KB | 60 | 0 | 28,9 s | 54,1 s | 0,1508 $ | 0 % | 0 % | 95 % | 100 % |
| Contradicción | 100 KB | 60 | 0 | 31,3 s | 58,0 s | 0,1786 $ | 0 % | 0 % | 96 % | 100 % |
| Contradicción | 400 KB | 3 | 3 | — | — | 0,0000 $ | — | — | — | — |

_No hay caché de respuestas en este proyecto: las dos columnas de caché valen 0 % por diseño._

### 2. Curva 1 · Latencia frente a tokens de entrada

_Cómo leerla: ¿tarda más cuanto más le mandamos? Mira «Primer texto»: es lo que tarda el modelo en leer antes de empezar a escribir. La latencia total depende sobre todo de lo larga que sea la respuesta._

| Tokens de entrada | Turnos | Primer texto P50 | Latencia P50 | Latencia P95 | Tokens de salida (media) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 – 5.000 | 9 | 7,3 s | 17,6 s | 19,2 s | 4.117 |
| 5.000 – 10.000 | 36 | 7,9 s | 18,8 s | 29,1 s | 4.993 |
| 10.000 – 20.000 | 76 | 9,4 s | 22,8 s | 32,8 s | 5.784 |
| 20.000 – 40.000 | 500 | 11,5 s | 31,6 s | 47,5 s | 8.497 |
| 40.000 – 80.000 | 270 | 14,6 s | 47,6 s | 74,2 s | 12.613 |
| 80.000 o más | 9 | 17,7 s | 69,4 s | 82,2 s | 18.463 |

### 3. Curva 2 · Coste acumulado por turno (media de las repeticiones)

_Cómo leerla: lo gastado hasta cada turno. Con PDF, el coste sube deprisa mientras el documento viaja en cada llamada (turnos 1 a 6) y se frena cuando sale de la ventana (turno 7)._

| Turno | Crece · sin PDF ($) | Crece · 100 KB ($) | Cambio de rumbo · sin PDF ($) | Cambio de rumbo · 100 KB ($) | Contradicción · sin PDF ($) | Contradicción · 100 KB ($) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0,0023 | 0,0073 | 0,0024 | 0,0072 | 0,0024 | 0,0072 |
| 2 | 0,0050 | 0,0146 | 0,0052 | 0,0142 | 0,0053 | 0,0143 |
| 3 | 0,0081 | 0,0220 | 0,0088 | 0,0220 | 0,0085 | 0,0220 |
| 4 | 0,0130 | 0,0311 | 0,0133 | 0,0313 | 0,0123 | 0,0305 |
| 5 | 0,0182 | 0,0408 | 0,0191 | 0,0414 | 0,0168 | 0,0398 |
| 6 | 0,0251 | 0,0518 | 0,0247 | 0,0516 | 0,0223 | 0,0498 |
| 7 | 0,0321 | 0,0591 | 0,0316 | 0,0591 | 0,0279 | 0,0561 |
| 8 | 0,0391 | 0,0664 | 0,0384 | 0,0664 | 0,0337 | 0,0629 |
| 9 | 0,0472 | 0,0749 | 0,0463 | 0,0753 | 0,0407 | 0,0710 |
| 10 | 0,0566 | 0,0841 | 0,0513 | 0,0806 | 0,0485 | 0,0800 |
| 11 | 0,0662 | 0,0942 | 0,0574 | 0,0891 | 0,0559 | 0,0891 |
| 12 | 0,0774 | 0,1058 | 0,0636 | 0,0988 | 0,0641 | 0,0989 |
| 13 | 0,0886 | 0,1173 | 0,0699 | 0,1093 | 0,0733 | 0,1095 |
| 14 | 0,1015 | 0,1311 | 0,0764 | 0,1205 | 0,0838 | 0,1204 |
| 15 | 0,1141 | 0,1448 | 0,0827 | 0,1327 | 0,0892 | 0,1264 |
| 16 | 0,1260 | 0,1600 | 0,0898 | 0,1467 | 0,0983 | 0,1378 |
| 17 | 0,1392 | 0,1767 | 0,0976 | 0,1617 | 0,1060 | 0,1480 |
| 18 | 0,1516 | 0,1937 | 0,1046 | 0,1775 | 0,1146 | 0,1582 |
| 19 | 0,1644 | 0,2114 | 0,1130 | 0,1939 | 0,1238 | 0,1695 |
| 20 | 0,1753 | 0,2243 | 0,1206 | 0,2100 | 0,1302 | 0,1786 |

### 4. Curva 3 · Recuerdo de la hoja de corrección tras el turno N

_Cómo leerla: qué parte de lo dicho en turnos anteriores sigue viva. Si baja cuando N crece, el sistema olvida sin avisar: es la degradación silenciosa._

| Turno N | Crece | Cambio de rumbo | Contradicción |
| --- | ---: | ---: | ---: |
| 1 | — | — | — |
| 3 | 100 % (4 datos) | 100 % (4 datos) | 100 % (3 datos) |
| 6 | 86 % (7 datos) | 83 % (8 datos) | 100 % (6 datos) |
| 10 | 91 % (11 datos) | 82 % (12 datos) | 95 % (11 datos) |
| 20 | 94 % (21 datos) | 86 % (21 datos) | 97 % (20 datos) |

_Media de las comprobaciones de ese turno (datos dichos antes de N). En el turno 1 no hay nada anterior que comprobar._

### 5. Datos del PDF en la respuesta del turno 1, por posición en el documento

_Cómo leerla: ¿usa el modelo los tres datos escondidos en el PDF? Si con los PDF grandes falla el del medio y no los de los extremos, es el efecto «lost in the middle»._

| PDF | Conversaciones | «sap» (principio) | «12.000 reservas» (medio) | «iso 27001» (final) |
| --- | ---: | ---: | ---: | ---: |
| 5 KB | 9 | 100 % | 100 % | 100 % |
| 20 KB | 9 | 100 % | 100 % | 100 % |
| 50 KB | 9 | 100 % | 100 % | 100 % |
| 100 KB | 9 | 100 % | 100 % | 100 % |

<!-- tablas:fin -->

## Lectura: dónde empieza a romperse este CAG y por qué

**Lo primero que se rompe es la latencia, y no por el contexto sino por la
respuesta.** Sin PDF, los turnos 1 a 3 tardan unos 18 s (P50) y caben en
el presupuesto de 20 s. En el turno 4 la mediana ya es de 25 s.

En toda la batería, contando los PDF, del turno 4 al 20 solo cumple el 5 %
de los turnos (el 52 % en los tres primeros; el 12 % en total). En el
turno 19, la P50 llega a 52 s y la P95 a 83 s.

El motivo es la respuesta. El estimador reescribe la estimación entera en
cada turno, y cada turno añade un requisito. La salida pasa de unos 4.100
tokens en el turno 1 a 10.600 en el 19, y Haiku escribe a unos 420 tokens por
segundo. La latencia sigue a los tokens de salida (correlación de 0,985), y
mucho menos a los de entrada (0,67). Lo que sí depende de la entrada es lo
que tarda en salir el primer texto: 7,3 s con menos de 5.000 tokens y
17,7 s con más de 80.000.

El coste, en cambio, no rompe con Haiku. El turno más caro de los 900 costó
0,020 $, y una conversación entera de 20 turnos, entre 0,10 y 0,24 $. Con
Opus 5.5, que cuesta 40 veces más por token, ese mismo presupuesto de
0,03 $ se rompería ya en el turno 1 (unos 0,09 $). El PDF de 100 KB:
- Triplica el coste del turno 1: 0,0073 $ frente a 0,0023 $.
- Lleva la entrada del turno 6 a 64.000 tokens.
- En el turno 7 sale de la ventana, y la entrada cae de golpe a 26.000 tokens.

El tope duro está en los 300.000 caracteres: el PDF de 400 KB se rechaza en
menos de un segundo, con un error claro y sin gastar nada.

**La memoria falla en silencio, y por los dos lados.** Lo que no entra en la
ficha (`project_metadata`) solo vive mientras su turno siga en la ventana de
6 o mientras el modelo lo vaya repitiendo:
- «Las reservas de más de cuatro horas necesitarán aprobación» (turno 4 del
  cambio de rumbo) se recuerda en el 100 % de las conversaciones hasta el
  turno 9. En el turno 10, justo cuando ese turno sale de la ventana, cae al
  27 %, y termina en el 13 %.
- El inicio de sesión único de la contradicción baja del 100 % al 60 % en el
  turno 8, por la misma razón.

Ningún turno da error: es la degradación silenciosa.

Lo que sí entra en la ficha no sale nunca, porque `merge_metadata` solo suma
tecnologías. React sigue en la memoria en el 100 % de las conversaciones
después de descartarlo en el turno 5. En la recapitulación del turno 20
reaparece en el 87 % de las respuestas, y en algún caso como partida nueva:
«panel web interno en React», +16 h.

Con el PDF pasa lo contrario de lo esperado: no hay *lost in the middle*
hasta 100 KB. Los tres datos, también el del medio, aparecen en el 100 % de
las respuestas del turno 1. Además siguen apareciendo cuando el documento sale
de la ventana, porque el modelo los repite en cada respuesta. Aun así, en el
turno 20 el dato del medio («12.000 reservas») solo sigue en el 83 % de las
respuestas, frente al 100 % de SAP.

Con estas cifras, RAG se justificaría por los documentos:
- Los de más de 300.000 caracteres no entran.
- Los que entran viajan solo 6 turnos y en cada uno pagan unos 40.000 tokens
  (el PDF de 100 KB).

Pero RAG no arregla lo que más rompe aquí. La latencia la causa la longitud
de la respuesta, y la deriva, una memoria que solo sabe añadir. Para eso hace
falta pedir respuestas que solo cuenten lo que cambia y una ficha que pueda
sustituir y borrar hechos.

### Matices de la medida

- **La métrica busca la palabra literal.** Tras corregir el presupuesto a
  80.000, «30.000» sigue en el 13-20 % de las respuestas. En la conversación
  que hemos podido revisar a mano aparece como «80.000 € (ampliado desde
  30.000 €)», que es un uso correcto pero cuenta como fallo.
- **El 0 % de «no incluye pagos»** (conversación que crece) tiene una causa
  concreta. Desde el turno 4, el bloque `<hechos_del_proyecto>` del modelo
  sustituye el alcance que dictó el cliente por su propia paráfrasis, y la
  ficha la recorta a 200 caracteres. La idea puede seguir ahí, pero la frase
  del cliente ya no.
- **La ficha se ensucia.** El bloque del modelo mete en «tecnologías» idiomas
  y trozos partidos por las comas, como «calendario del sistema (EventKit en
  iOS». El número de hechos de la ficha (`anchors_count`) llega a 33.
- **Un solo modelo y datos sintéticos.** Todo es con un solo modelo (Haiku
  5.5), conversaciones sintéticas y 3 repeticiones por combinación. Las cifras
  sirven para ver la forma de las curvas y el turno en que cambian, no como
  valores absolutos.
