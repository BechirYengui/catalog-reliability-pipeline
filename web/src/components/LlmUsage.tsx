import { Badge, Card, nf, pf } from "./ui";

export interface LlmUsageData {
  model?: string;
  model_display_name?: string;
  input_price_per_mtok?: number;
  output_price_per_mtok?: number;
  calls_real?: number;
  calls_cached?: number;
  // Libellés servis par le cache SANS appel : un run entièrement recyclé a
  // zéro appel et n'en a pas moins travaillé.
  labels_total?: number;
  labels_cached?: number;
  cache_hit_rate?: number;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  cost_usd?: number;
  cost_breakdown_usd?: {
    input: number;
    output: number;
    cache_read: number;
    cache_write: number;
  };
  errors?: number;
  queued?: number;
  dry_run?: boolean;
  duration_ms?: number;
}

/**
 * Un coût de 0,004 $ affiché « 0,00 $ » donne l'impression que rien n'a été
 * dépensé. On garde donc assez de décimales pour que le chiffre reste vrai.
 */
export function formatUsd(amount: number): string {
  if (amount === 0) return "0,00 $";
  if (amount < 0.01) return `${amount.toFixed(4).replace(".", ",")} $`;
  return `${amount.toFixed(2).replace(".", ",")} $`;
}

/**
 * Consommation LLM d'un run : quel modèle, combien de jetons, combien d'argent.
 *
 * Une plateforme qui appelle un LLM sans montrer ce qu'il coûte est une
 * plateforme dont personne ne peut arbitrer l'usage.
 */
export function LlmUsageCard({
  usage,
  distinctLabels,
  rowsIn,
}: {
  usage?: LlmUsageData;
  distinctLabels: number;
  rowsIn: number;
}) {
  // Un run entierement servi par le cache n'a AUCUN appel : ni reel, ni
  // « cache d'appel ». Il a pourtant enrichi ses libellés, et le dire « non
  // exécuté » revenait à nier le travail que la plateforme vient de montrer
  // dans l'étape juste au-dessus.
  const labelsCached = usage?.labels_cached ?? 0;
  const calls = (usage?.calls_real ?? 0) + (usage?.calls_cached ?? 0);
  const notRun = !usage || (calls === 0 && labelsCached === 0);
  const fullyCached = !notRun && (usage?.calls_real ?? 0) === 0;

  if (notRun) {
    return (
      <Card
        title="Étage LLM"
        subtitle="Enrichissement sémantique : libellés caisse, taxonomie manquante, attributs"
        action={<Badge tone="neutral">non exécuté</Badge>}
      >
        <p className="text-xs leading-relaxed text-ink-2">
          {(usage?.queued ?? 0) > 0
            ? "Les lots sont restés en attente : sans clé API, ou l'enveloppe de dépense épuisée, l'étage se saute et le reste du pipeline continue."
            : "L'étage n'a rien enrichi sur ce fichier."}{" "}
          C&apos;est la dégradation gracieuse voulue : un catalogue à moitié
          enrichi vaut mieux qu&apos;un run en échec.
        </p>
        <dl className="mt-4 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
          <div>
            <dt className="text-ink-3">Libellés à traiter</dt>
            <dd className="tnum mt-0.5 font-semibold text-ink">{nf.format(distinctLabels)}</dd>
          </div>
          <div>
            <dt className="text-ink-3">Lignes du fichier</dt>
            <dd className="tnum mt-0.5 text-ink-2">{nf.format(rowsIn)}</dd>
          </div>
          <div>
            <dt className="text-ink-3">Facteur d&apos;économie</dt>
            <dd className="tnum mt-0.5 font-semibold text-good">
              ×{Math.round(rowsIn / Math.max(distinctLabels, 1))}
            </dd>
          </div>
          <div>
            <dt className="text-ink-3">Appels évités</dt>
            <dd className="tnum mt-0.5 text-ink-2">
              {nf.format(Math.max(rowsIn - distinctLabels, 0))}
            </dd>
          </div>
        </dl>
        <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
          Le LLM ne verra que les <strong>{nf.format(distinctLabels)} libellés
          distincts</strong>, jamais les {nf.format(rowsIn)} lignes. C&apos;est la
          principale économie du projet.
        </p>
      </Card>
    );
  }

  const cost = usage.cost_usd ?? 0;
  const breakdown = usage.cost_breakdown_usd;
  const totalTokens =
    (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0) + (usage.cache_read_tokens ?? 0);

  return (
    <Card
      title="Étage LLM"
      subtitle={`${usage.model_display_name ?? usage.model} : ${usage.input_price_per_mtok} $ / ${usage.output_price_per_mtok} $ par million de jetons`}
      action={
        usage.dry_run ? (
          <Badge tone="warn">simulation</Badge>
        ) : fullyCached ? (
          <Badge tone="good">servi par le cache</Badge>
        ) : (
          <Badge tone="info">{usage.model}</Badge>
        )
      }
    >
      {fullyCached && (
        <p className="mb-3 rounded-lg border border-good/40 bg-good/10 px-3 py-2 text-[11px] leading-relaxed text-ink">
          <strong>Ce run n&apos;a coûté zéro.</strong> Les{" "}
          {nf.format(labelsCached)} libellés distincts avaient déjà été traduits
          lors d&apos;un dépôt précédent : ils sortent du cache, aucun appel
          n&apos;a été passé. C&apos;est l&apos;économie promise par le cache,
          constatée, pas un étage sauté.
        </p>
      )}
      <div className="grid gap-3 sm:grid-cols-4">
        <Metric label="Coût de ce run" value={formatUsd(cost)} strong />
        <Metric
          label="Appels réels"
          value={nf.format(usage.calls_real ?? 0)}
          hint={`${nf.format(labelsCached)} libellés sortis du cache`}
        />
        <Metric
          label="Taux de cache"
          value={pf.format(usage.cache_hit_rate ?? 0)}
          hint="les runs suivants coûtent moins"
        />
        <Metric label="Jetons" value={nf.format(totalTokens)} hint="entrée + sortie + cache" />
      </div>

      {/* Décomposer 0 $ en quatre lignes à 0 $ n'apprend rien. */}
      {breakdown && cost > 0 && (
        <div className="mt-4 border-t border-line pt-3">
          <p className="text-xs font-medium text-ink-2">Décomposition</p>
          <ul className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2">
            <CostLine
              label="Entrée"
              tokens={usage.input_tokens ?? 0}
              usd={breakdown.input}
            />
            <CostLine
              label="Sortie"
              tokens={usage.output_tokens ?? 0}
              usd={breakdown.output}
            />
            <CostLine
              label="Lecture du cache"
              tokens={usage.cache_read_tokens ?? 0}
              usd={breakdown.cache_read}
              hint="≈ 10 % du tarif d'entrée"
            />
            <CostLine
              label="Écriture du cache"
              tokens={usage.cache_write_tokens ?? 0}
              usd={breakdown.cache_write}
              hint="≈ 1,25× le tarif d'entrée"
            />
          </ul>
        </div>
      )}

      {(usage.queued ?? 0) > 0 && (
        <p className="mt-3 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[11px] text-warn">
          {nf.format(usage.queued ?? 0)} lot(s) en file d&apos;attente, budget du run
          atteint ou API indisponible. Le reste du pipeline a continué.
        </p>
      )}

      <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
        Le LLM n&apos;a vu que les <strong>{nf.format(distinctLabels)} libellés
        distincts</strong> du fichier, envoyés par lots, et non les{" "}
        {nf.format(rowsIn)} lignes. Il ne valide aucun checksum et ne propose
        jamais de taux de TVA : ces décisions appartiennent aux règles et à
        l&apos;humain.
      </p>
    </Card>
  );
}

function Metric({
  label,
  value,
  hint,
  strong,
}: {
  label: string;
  value: string;
  hint?: string;
  strong?: boolean;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface-2/40 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-ink-3">{label}</div>
      <div className={`tnum mt-0.5 font-semibold ${strong ? "text-lg text-s2" : "text-sm text-ink"}`}>
        {value}
      </div>
      {hint && <div className="mt-0.5 text-[11px] text-ink-3">{hint}</div>}
    </div>
  );
}

function CostLine({
  label,
  tokens,
  usd,
  hint,
}: {
  label: string;
  tokens: number;
  usd: number;
  hint?: string;
}) {
  return (
    <li className="flex items-baseline justify-between gap-3">
      <span className="text-ink-2">
        {label}
        {hint && <span className="ml-1 text-[11px] text-ink-3">({hint})</span>}
      </span>
      <span className="tnum whitespace-nowrap text-ink-3">
        {nf.format(tokens)} jetons · <span className="text-ink">{formatUsd(usd)}</span>
      </span>
    </li>
  );
}
