import type { ReactNode } from "react";

/** Formats partagés : les chiffres se comparent d'une vue à l'autre. */
export const nf = new Intl.NumberFormat("fr-FR");
export const pf = new Intl.NumberFormat("fr-FR", {
  style: "percent",
  maximumFractionDigits: 1,
});

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

export function Card({
  title,
  subtitle,
  action,
  children,
  className = "",
  bodyClassName = "",
}: {
  title?: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Pour les cartes à hauteur bornée : `flex flex-col` fait du corps une
   *  colonne, sans quoi le `flex-1` de la zone défilante ne s'applique à rien
   *  et le contenu sort de la carte au lieu de défiler dedans. */
  bodyClassName?: string;
}) {
  return (
    <section
      className={`rounded-xl border border-line bg-surface-1 ${className}`}
    >
      {(title || action) && (
        <header className="flex items-start justify-between gap-3 border-b border-line px-3 py-2.5 sm:px-4 sm:py-3">
          <div>
            {title && <h2 className="text-sm font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-3">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      {/* `flex-1 min-h-0` reste sans effet sur une carte ordinaire, et permet a
          celles qu'on met en `flex h-full flex-col` de laisser leur contenu
          occuper toute la hauteur disponible plutot que de s'arreter au milieu. */}
      <div className={`min-h-0 flex-1 p-3 sm:p-4 ${bodyClassName}`}>{children}</div>
    </section>
  );
}

/**
 * Chiffre isolé. Une valeur unique n'a pas besoin d'un graphique : le nombre
 * lui-même est la visualisation la plus lisible.
 */
export function StatTile({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "neutral" | "good" | "warn" | "critical";
}) {
  // Le texte est d'une seule encre : la hiérarchie vient de la taille et de la
  // graisse, pas de la teinte. Le statut passe donc au liseré de la tuile —
  // ainsi un chiffre qui demande attention se repère toujours, sans qu'aucun
  // mot soit moins lisible qu'un autre.
  const toneBorder = {
    neutral: "border-line",
    good: "border-good/45",
    warn: "border-warn/55",
    critical: "border-critical/55",
  }[tone];
  return (
    <div className={`rounded-xl border ${toneBorder} bg-surface-1 px-4 py-3`}>
      <div className="text-xs font-medium uppercase tracking-wide text-ink">
        {label}
      </div>
      <div className="tnum mt-1 text-2xl font-semibold text-ink">{value}</div>
      {hint && <div className="mt-1 text-xs text-ink">{hint}</div>}
    </div>
  );
}

/**
 * Pastille de statut. La couleur ne porte JAMAIS le sens seule : elle est
 * toujours accompagnée du libellé : c'est ce qui la rend lisible en cas de
 * daltonisme, en impression noir et blanc, ou en contraste forcé.
 */
export function Badge({
  tone,
  children,
}: {
  tone: "good" | "warn" | "serious" | "critical" | "neutral" | "info";
  children: ReactNode;
}) {
  const map = {
    good: "bg-good/15 text-good border-good/40",
    warn: "bg-warn/15 text-warn border-warn/40",
    serious: "bg-serious/15 text-serious border-serious/40",
    critical: "bg-critical/15 text-critical border-critical/40",
    info: "bg-s1/15 text-s1 border-s1/40",
    neutral: "bg-surface-3 text-ink-2 border-line-strong",
  }[tone];
  return (
    <span
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-1.5 py-0.5 text-xs font-medium ${map}`}
    >
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
  className = "",
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "ghost" | "danger";
  disabled?: boolean;
  type?: "button" | "submit";
  className?: string;
  title?: string;
}) {
  const variants = {
    default:
      "border-line-strong bg-surface-2 text-ink hover:bg-surface-3",
    primary: "border-transparent bg-s1 text-white hover:opacity-90",
    ghost: "border-transparent bg-transparent text-ink-2 hover:bg-surface-2",
    danger: "border-critical/40 bg-critical/10 text-critical hover:bg-critical/20",
  }[variant];
  return (
    <button
      type={type}
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${variants} ${className}`}
    >
      {children}
    </button>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <svg
      className={`animate-spin ${className}`}
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 py-10 text-center">
      <p className="text-sm text-ink-2">{title}</p>
      {hint && <p className="max-w-md text-xs text-ink-3">{hint}</p>}
    </div>
  );
}
