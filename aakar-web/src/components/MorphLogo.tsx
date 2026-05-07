export function MorphLogo({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`morph-logo ${className}`}
      viewBox="0 0 100 100"
      role="img"
      aria-label="Aakaar animated shape logo"
    >
      <g className="morph-logo__shape morph-logo__shape--triangle">
        <path d="M50 12 L88 82 L12 82 Z" />
      </g>
      <g className="morph-logo__shape morph-logo__shape--square">
        <path d="M22 22 H78 V78 H22 Z" />
      </g>
      <g className="morph-logo__shape morph-logo__shape--circle">
        <circle cx="50" cy="50" r="31" />
      </g>
      <g className="morph-logo__shape morph-logo__shape--sine">
        <path d="M12 50 C22 24 34 24 44 50 S66 76 78 50 S92 24 98 42" />
      </g>
      <g className="morph-logo__shape morph-logo__shape--arrow">
        <path d="M15 50 H72" />
        <path d="M56 28 L78 50 L56 72" />
      </g>
    </svg>
  );
}
