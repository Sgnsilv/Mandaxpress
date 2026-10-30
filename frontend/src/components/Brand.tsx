export function Logo({ size = 36 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" role="img" aria-label="Mandaxpress" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="mx-grad" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#7c3aed" />
          <stop offset="1" stopColor="#4c1d95" />
        </linearGradient>
      </defs>
      <rect width="48" height="48" rx="12" fill="url(#mx-grad)" />
      <path d="M11 33V15l8.5 11L28 15v18" fill="none" stroke="#fff" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M32 17l6 6-6 6" fill="none" stroke="#a3e635" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Brand({ size = 36 }: { size?: number }) {
  return (
    <span className="brand">
      <Logo size={size} />
      <span className="brand-name">
        Manda<span className="brand-x">x</span>press
      </span>
    </span>
  );
}
