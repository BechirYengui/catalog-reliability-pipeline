import type { LlmBudget } from "../lib/api";
import { nf, pf } from "./ui";
import { formatUsd } from "./LlmUsage";

/**
 * Enveloppe de dépense LLM de la démonstration.
 *
 * Le seuil est CUMULÉ sur tous les traitements : un plafond par run seul ne
 * protège de rien, puisque dix runs sous leur plafond dépassent quand même
 * l'enveloppe. Une fois à zéro, l'API est coupée.
 *
 * Barre unique, pas de camembert : une part consommée sur un total se lit
 * mieux sur une longueur alignée. Le seuil des 80 % passe la couleur du bleu
 * neutre à l'orange d'avertissement, toujours doublé d'un libellé : la teinte
 * ne porte jamais le sens seule.
 */
export function BudgetGauge({
  budget,
  compact = false,
}: {
  budget: LlmBudget;
  compact?: boolean;
}) {
  const ratio = Math.min(budget.used_ratio, 1);
  const barColor = budget.exhausted
    ? "var(--status-critical)"
    : ratio > 0.8
      ? "var(--status-warning)"
      : "var(--series-1)";

  if (compact) {
    return (
      <div className="flex items-center gap-2" title="Enveloppe LLM de la démonstration">
        <div className="h-1.5 w-20 overflow-hidden rounded bg-surface-3">
          <div
            className="h-1.5 rounded-r-[2px]"
            style={{ width: `${Math.max(ratio * 100, 2)}%`, background: barColor }}
          />
        </div>
        <span className="tnum text-[11px] text-ink">
          {formatUsd(budget.remaining_usd)} restants
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-line bg-surface-1 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-ink">Enveloppe LLM</h3>
          <p className="mt-0.5 text-xs text-ink">
            Plafond cumulé sur toute la démonstration, en {budget.currency}
          </p>
        </div>
        {/* Pastille écrite en blanc : la couleur reste, portée par le fond et
            le liseré, mais aucun mot n'est rendu dans une teinte moins lisible
            que le reste de la carte. Le libellé dit déjà l'état, la teinte ne
            fait que le confirmer. */}
        <span
          className="inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-medium text-ink"
          style={{
            background: `color-mix(in oklab, ${barColor} 18%, transparent)`,
            borderColor: `color-mix(in oklab, ${barColor} 45%, transparent)`,
          }}
        >
          {budget.exhausted ? "épuisée, API coupée" : `${pf.format(ratio)} utilisé`}
        </span>
      </div>

      <div className="mt-3 h-2.5 overflow-hidden rounded bg-surface-2">
        <div
          className="h-2.5 rounded-r-[3px] transition-[width] duration-500"
          style={{ width: `${Math.max(ratio * 100, 1.5)}%`, background: barColor }}
        />
      </div>

      <dl className="mt-3 grid grid-cols-3 gap-3 text-xs">
        <div>
          <dt className="text-ink">Dépensé</dt>
          <dd className="tnum mt-0.5 font-semibold text-ink">{formatUsd(budget.spent_usd)}</dd>
        </div>
        <div>
          <dt className="text-ink">Restant</dt>
          <dd className="tnum mt-0.5 font-semibold text-ink">
            {formatUsd(budget.remaining_usd)}
          </dd>
        </div>
        <div>
          <dt className="text-ink">Plafond</dt>
          <dd className="tnum mt-0.5 text-ink">{formatUsd(budget.limit_usd)}</dd>
        </div>
      </dl>

      {budget.exhausted ? (
        <p className="mt-3 rounded-lg border border-critical/40 bg-critical/10 px-3 py-2 text-[11px] leading-relaxed text-ink">
          L&apos;enveloppe est épuisée : l&apos;étage LLM est désormais sauté et
          le reste du pipeline continue de tourner. Aucun appel supplémentaire
          ne sera facturé.
        </p>
      ) : (
        <p className="mt-3 text-[11px] leading-relaxed text-ink">
          {budget.runs_charged > 0
            ? `${nf.format(budget.runs_charged)} traitement(s) facturé(s). `
            : "Aucun traitement facturé pour l'instant. "}
          Les libellés déjà vus sont servis par le cache et ne coûtent rien : un
          second dépôt du même magasin n&apos;entame pas l&apos;enveloppe.
        </p>
      )}
    </div>
  );
}
