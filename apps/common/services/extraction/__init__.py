"""Deterministic invoice extraction: OCR + layout rules, no LLM.

Stage one of the extraction router. A bill goes through `parser` first;
only when the result fails the gate does it cost a GPT-4o call.

Nothing here is imported at Django start-up and `indra_ocr` is imported
lazily inside ``parser.run_ocr``, so adding this package cannot affect a
deployment that has not installed it.
"""
