"""Internal CTS cheque-verification vision pipeline (ported from the Ravi fork).

This subpackage holds the tested, banking-critical OCR/validation modules that
back the ``cap.cheque_*`` / ``cap.micr_read`` / ``cap.signature_detect``
capability wrappers in ``aakaar_caps.caps``. It is NOT a capability package
itself — it starts with a normal name but lives OUTSIDE ``aakaar_caps.caps``, so
the loader never scans it. Modules here only cross-import each other, the stdlib,
and (lazily, inside functions) the optional ``cv2`` / ``numpy`` / ``rapidocr``
deps, so importing this package at module top level is dependency-free.

The public entry points the cap wrappers call:
  - ``cheque_ocr.extract_fields(png_bytes, *, side, dom=None) -> ChequeFields``
  - ``cheque_validation.validate_cheque(*, front, back, dom, validity_days=...) -> ChequeValidationReport``
  - ``cheque_decision.decide(report) -> ChequeDecision``
  - ``micr.run_micr_ocr(png_bytes, *, bottom_fraction=..., upscale=...) -> MicrResult``
  - ``signature_detector.detect_signature(png_bytes) -> SignatureResult``
"""
