"""Pipeline mínimo de embeddings y chunking (sesión 7).

Para qué sirve. Más adelante el estimador buscará, entre los presupuestos
históricos, los componentes parecidos al proyecto nuevo («ya hicimos un login
con OAuth para un banco: 120 horas»). Esa búsqueda por significado necesita
que cada trozo de presupuesto sea una lista de números (un *embedding*). Este
módulo construye ese primer tramo y todavía no guarda nada: los vectores
salen en la respuesta HTTP. La base de datos llega en la sesión 8.

El recorrido de un presupuesto:

    presupuestos JSON ──► chunker ──► chunks ──► embedder ──► vectores ──► respuesta
       (Budget)        (un componente    (texto +      (OpenAI, lotes     (+ estadísticas
                        = un chunk)      metadata)      de 100)            y coste)

Qué hay en cada fichero:

- ``schemas.py``  los «moldes» de datos: qué es un presupuesto válido, qué es un
                  chunk y qué devuelve el endpoint.
- ``chunker.py``  corta cada presupuesto en chunks (un componente = un chunk).
- ``embedder.py`` convierte los chunks en vectores llamando a OpenAI.
- ``router.py``   el endpoint ``POST /embeddings/ingest`` que une las piezas.
- ``SANITY_CHECK.md``  prueba de cordura con parejas de textos reales.

No importamos nada aquí a propósito: cada fichero se importa por su nombre
(``from app.embedding_pipeline.schemas import Budget``) y así un fallo en uno
no impide cargar los demás.
"""
