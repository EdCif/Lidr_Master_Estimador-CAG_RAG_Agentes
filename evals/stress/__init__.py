"""Stress test del CAG (sesión 6): medir dónde empieza a romperse.

Sometemos el estimador a conversaciones largas (hasta 20 turnos) y a
adjuntos grandes, y apuntamos qué pasa en cada turno. Las piezas:

* ``metrics.py``: las tres métricas que dicen "pasa / no pasa" en cada
  turno (presupuesto de latencia, presupuesto de coste y deriva de memoria).
* ``scenarios.py``: las tres conversaciones sintéticas (un proyecto que
  crece, uno que cambia de tecnología y uno que se contradice).
* ``fixtures/build_pdfs.py``: genera los PDF de prueba de 5 a 100 KB.
* ``run.py``: lanza la batería contra la API y escribe ``results.csv``.
* ``report.py``: convierte el CSV en las tablas de ``REPORT.md``.

Cada turno de la API deja una observación (``turn_observed``, ver
``app/services/turn_observer.py``); estas piezas la leen y la evalúan.
"""
