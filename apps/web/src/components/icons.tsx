interface IconProps {
  className?: string;
}

function base(className?: string) {
  return {
    className: className ?? "h-4 w-4",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
}

export function IconGrid({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="3.5" width="7" height="7" rx="1.5" />
      <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
      <rect x="13.5" y="13.5" width="7" height="7" rx="1.5" />
    </svg>
  );
}

export function IconPapers({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M8 3.5h8l4 4v13H8z" />
      <path d="M16 3.5v4h4" />
      <path d="M4 7.5v13h12" />
      <path d="M11 12h6M11 15.5h6" />
    </svg>
  );
}

export function IconPulse({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M3 12h4l2.5-6 4 12 2.5-6h5" />
    </svg>
  );
}

export function IconLogout({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M14 4h-8v16h8" />
      <path d="M10 12h11M18 8.5 21.5 12 18 15.5" />
    </svg>
  );
}

export function IconMenu({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M4 7h16M4 12h16M4 17h16" />
    </svg>
  );
}

export function IconX({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

export function IconTrash({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7" />
    </svg>
  );
}

export function IconPdf({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M6 3.5h9l4 4v13H6z" />
      <path d="M15 3.5v4h4" />
      <path d="M9 13h1.6a1.4 1.4 0 0 0 0-2.8H9v5.6M13.4 10.2v5.6h1.2a2.8 2.8 0 0 0 0-5.6z" strokeWidth="1.3" />
    </svg>
  );
}

export function IconUpload({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M12 16V4.5M7.5 9 12 4.5 16.5 9" />
      <path d="M4 15v4.5h16V15" />
    </svg>
  );
}

export function IconLink({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.2 1.2" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1.2-1.2" />
    </svg>
  );
}

export function IconSearch({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <circle cx="10.5" cy="10.5" r="6" />
      <path d="m15 15 5.5 5.5" />
    </svg>
  );
}

export function IconArrowRight({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M4 12h16M14 6l6 6-6 6" />
    </svg>
  );
}

export function IconArrowLeft({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M20 12H4M10 6l-6 6 6 6" />
    </svg>
  );
}

export function IconCheck({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="m4.5 12.5 5 5 10-11" />
    </svg>
  );
}

export function IconWarn({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M12 3.5 22 20H2z" />
      <path d="M12 9.5v5M12 17.4v.2" />
    </svg>
  );
}

export function IconPlay({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M7 4.5 19 12 7 19.5z" />
    </svg>
  );
}

export function IconStop({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}

export function IconRefresh({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M20 12a8 8 0 1 1-2.6-5.9" />
      <path d="M20 4v4.5h-4.5" />
    </svg>
  );
}

export function IconDownload({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M12 4v11.5M7.5 11 12 15.5 16.5 11" />
      <path d="M4 19.5h16" />
    </svg>
  );
}

export function IconCode({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="m8 8-4.5 4L8 16M16 8l4.5 4L16 16M13.5 5l-3 14" />
    </svg>
  );
}

export function IconFlask({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M9.5 3.5h5M10.5 3.5v5.2L4.8 18a2 2 0 0 0 1.7 3h11a2 2 0 0 0 1.7-3l-5.7-9.3V3.5" />
      <path d="M7.5 15h9" />
    </svg>
  );
}

export function IconLock({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <rect x="5" y="10.5" width="14" height="9.5" rx="2" />
      <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" />
      <path d="M12 14.5v2" />
    </svg>
  );
}

export function IconQuote({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="M9.5 7c-3 1-4.5 3-4.5 6v4h5v-5H7.2c.2-2 1-3.2 2.3-4zM19 7c-3 1-4.5 3-4.5 6v4h5v-5h-2.8c.2-2 1-3.2 2.3-4z" />
    </svg>
  );
}

export function IconClock({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7v5l3.5 2" />
    </svg>
  );
}

export function IconChip({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <rect x="7" y="7" width="10" height="10" rx="2" />
      <path d="M10 3.5V7M14 3.5V7M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5" />
    </svg>
  );
}

export function IconChevronDown({ className }: IconProps) {
  return (
    <svg {...base(className)}>
      <path d="m6 9.5 6 6 6-6" />
    </svg>
  );
}

export function LogoMark({ className }: IconProps) {
  return (
    <svg className={className ?? "h-7 w-7"} viewBox="0 0 32 32" fill="none" aria-hidden>
      <path
        d="M16 2.5 28 9v14l-12 6.5L4 23V9z"
        stroke="var(--color-brand)"
        strokeWidth="1.6"
      />
      <path d="M16 9.5v13M10 12.5l12 7M22 12.5l-12 7" stroke="var(--color-brand)" strokeWidth="1.1" opacity="0.55" />
      <circle cx="16" cy="9.5" r="2.1" fill="var(--color-brand)" />
      <circle cx="10" cy="19.5" r="1.7" fill="var(--color-info)" />
      <circle cx="22" cy="19.5" r="1.7" fill="var(--color-scale)" />
      <circle cx="16" cy="16" r="1.4" fill="var(--color-fg)" />
    </svg>
  );
}