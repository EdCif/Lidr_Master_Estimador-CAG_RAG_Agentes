"""Adjuntos de la sesión 05: PDF y Word que acompañan a la transcripción.

Hay dos caminos para hacérselos llegar al LLM, cada uno en su módulo:

* ``path_a_files_api.py`` · Camino A (multimodal directo): el PDF se sube tal
  cual a la Files API de Anthropic y Claude lo lee entero, con imágenes y
  diagramas incluidos.
* ``path_b_local.py`` · Camino B (extracción local): sacamos el texto aquí
  con pypdf / MarkItDown / python-docx y lo pegamos en el prompt.

Lo que comparten los dos (qué es un adjunto, cómo se valida) está en
``common.py``.
"""
