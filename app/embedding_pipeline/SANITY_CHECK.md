# Sanity check de los embeddings (sesión 7)

Prueba de cordura del pipeline: ¿el modelo `text-embedding-3-small` distingue
textos parecidos de textos distintos? No es una validación formal; solo
comprueba que el pipeline funciona de punta a punta y que los vectores
discriminan razonablemente.

Cómo se ha medido: `scripts/compare.py` pide a OpenAI el vector de cada texto
y calcula la similitud coseno a mano (producto escalar entre el producto de
las normas). Fecha: 2026/11/03. Modelo: `text-embedding-3-small` (1536 números).

```powershell
uv run python scripts/compare.py --text-a "<texto 1>" --text-b "<texto 2>"
```

## Resultados

| Pareja | Texto 1 | Texto 2 | Esperado | Similitud |
|---|---|---|---|---|
| **A** · cercanos | OAuth 2.0 authentication backend with JWT tokens for fintech mobile app | Authorization service using JSON Web Tokens for a banking application | alta (> 0,6) | **0,5957** |
| **B** · no relacionados | OAuth 2.0 authentication backend with JWT tokens for fintech mobile app | Database migration from MySQL to PostgreSQL with zero downtime | baja (< 0,4) | **0,1919** |
| **C** · genéricos | Backend services | API development | sin expectativa | **0,5408** |
| **D** · idioma (extra nuestro) | OAuth 2.0 authentication backend with JWT tokens for fintech mobile app | Backend de autenticación OAuth 2.0 con tokens JWT para una app móvil fintech | sin expectativa | **0,8177** |

Referencia: el material del profesor da A 0,5957, B 0,1920 y C 0,5407. Las
nuestras coinciden hasta la tercera cifra decimal (la cuarta varía un poco entre
llamadas a la API).

## Comentario

- **El modelo separa lo parecido de lo ajeno**: A (0,5957) queda 0,40 puntos por
  encima de B (0,1919). B cumple lo esperado (< 0,4); A, en cambio, queda
  **justo por debajo de 0,6**, aunque son casi sinónimos: ese umbral es solo
  orientativo y no sirve tal cual.
- **Lo que más llama la atención es C**: dos frases de tres palabras, vagas y casi
  sin contenido, puntúan 0,5408, casi igual que A. Los textos cortos y genéricos
  «parecen cercanos» a casi todo. Por eso cada chunk lleva delante una cabecera
  con el contexto de su presupuesto (proyecto, sector, año, tecnología).
- **No existe el cero**: B, que no tiene nada que ver con A, da 0,19, no 0. Y A y C
  están separadas por solo 0,055, así que un umbral de «parecido» fijado a ojo
  daría falsos positivos: habrá que calibrarlo con los datos propios en la
  sesión 8.
- **D, el español, sale la más alta (0,8177)**, más que A. Ojo con la lectura: D es
  una traducción casi literal y conserva los términos técnicos (OAuth 2.0, JWT,
  fintech), mientras que A es una paráfrasis con otras palabras. No dice que el
  idioma no importe, sino que el modelo es muy robusto cuando hay términos
  técnicos compartidos.

## Prueba adicional: el idioma sin términos técnicos compartidos

Tres pares cortos más, medidos con el mismo script:

| Par | Similitud |
|---|---|
| «Login con Google» frente a «Autenticación mediante cuenta de Google» (mismo significado, español) | 0,7630 |
| «Login con Google» frente a «Google sign-in» (mismo significado, español frente a inglés) | 0,6333 |
| «Login con Google» frente a «Receta de tortilla de patatas» (sin relación) | 0,1401 |

Aquí sí se nota el idioma: la misma idea cae de 0,7630 a 0,6333 al cruzar de
español a inglés. La estructura se mantiene (los dos parecidos muy por encima del
ajeno), pero conviene recordarlo: nuestro dataset está en español y una consulta
en inglés puntuará algo más bajo.

## Ingesta real del dataset

`scripts/ingest_sample.py` manda `data/budgets_sample.json` a
`POST /embeddings/ingest` (API en marcha, OpenAI de verdad):

| Dato | Valor |
|---|---|
| Presupuestos → chunks | 15 → 51 (uno por componente) |
| Tokens totales | 5.841 |
| Coste estimado | 0,000117 USD |
| Números por vector | 1536 en los 51 chunks (norma 1) |
| Tiempo | 0,7 s para todo el lote (la primera llamada suelta tardó unos 6 s: es la conexión inicial) |
| Chunks por sector | ecommerce 14, finance 14, healthcare 13, industrial 10 |

En una ingesta anterior de 2 chunks, los tokens contados por nosotros con
tiktoken (210) coincidieron exactamente con los que factura OpenAI
(`api_tokens`, 210): el coste que se calcula antes de llamar es el real.
Los vectores se guardan en `data/embeddings_sample.json`, que no se sube al
repositorio.

## Conclusión

El pipeline funciona de punta a punta y los vectores discriminan bien lo que
importa (A y D muy por encima de B). Quedan dos avisos para la sesión 8: los
textos genéricos engañan (C) y cualquier umbral de similitud hay que calibrarlo
con búsquedas reales sobre nuestros presupuestos.
