import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type AuditEntry, type LlmBudget, type Metrics, type Run } from "../lib/api";
import { TTL, invalidate, put, useResource } from "../lib/cache";
import { Badge, Button, Card, EmptyState, Spinner, StatTile, nf, pf } from "../components/ui";
import { BarList, TrendLine } from "../components/charts";
import { RunStatusBadge } from "./RunView";
import { BudgetGauge } from "../components/BudgetGauge";

export default function Dashboard() {
  // Les trois ressources viennent du cache partage : revenir sur le tableau
  // de bord affiche immediatement ce qu'on y avait vu, et le rafraichissement
  // se fait derriere. L'attente n'a lieu qu'a la toute premiere visite.
  const { data: metrics, loading } = useResource<Metrics>("/api/metrics", api.metrics);
  const { data: runs = [] } = useResource<Run[]>("/api/runs", api.runs);
  const { data: budget } = useResource<LlmBudget>("/api/llm/budget", api.llmBudget);

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-16 text-ink-3">
        <Spinner /> Chargement des indicateurs…
      </div>
    );
  }

  if (!metrics || metrics.runs_total === 0) {
    return (
      <Card title="Aucun traitement pour l'instant">
        <EmptyState
          title="Déposez un premier catalogue pour voir les indicateurs"
          hint="Le tableau de bord se construit à partir des runs : taux de produits publiables, anomalies par type, décisions en attente."
        />
        <div className="flex justify-center">
          <Link
            to="/traitement"
            className="rounded-lg bg-s1 px-4 py-2 text-sm font-medium text-white"
          >
            Déposer un fichier
          </Link>
        </div>
      </Card>
    );
  }

  const anomalyItems = Object.entries(metrics.anomalies_by_code)
    .map(([label, value]) => ({ label, value }))
    .slice(0, 12);
  const anomalyTotal = Object.values(metrics.anomalies_by_code).reduce((a, b) => a + b, 0);

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="text-lg font-semibold text-ink">Tableau de bord</h1>
        <p className="mt-0.5 text-xs text-ink-3">
          L&apos;indicateur qui compte n&apos;est pas le nombre d&apos;anomalies corrigées,
          mais la part du catalogue réellement publiable.
        </p>
      </div>

      <Card title="Derniers traitements">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[46rem] text-left text-xs">
            <thead>
              <tr className="border-b border-line text-ink-3">
                <th className="py-2 pr-3 font-medium">Magasin</th>
                <th className="py-2 pr-3 font-medium">Statut</th>
                <th className="py-2 pr-3 text-right font-medium">Lignes</th>
                <th className="py-2 pr-3 text-right font-medium">Fiches</th>
                {/* Ce taux porte sur les lignes reçues : c'est la santé du
                    fichier. Les fiches publiables se comptent après
                    regroupement, sur la page du traitement. */}
                <th className="py-2 pr-3 text-right font-medium">Lignes exploitables</th>
                <th className="py-2 pr-3 font-medium">Déposé par</th>
                <th className="py-2 pr-3 font-medium">Date</th>
                <th className="py-2 font-medium"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {runs.slice(0, 15).map((run) => (
                <tr key={run.id} className="hover:bg-surface-2">
                  <td className="py-2 pr-3">
                    <Link
                      to={`/runs/${run.id}`}
                      className="font-medium text-ink hover:text-s1 hover:underline"
                    >
                      {run.store_id}
                    </Link>
                  </td>
                  <td className="py-2 pr-3">
                    <RunStatusBadge status={run.status} />
                  </td>
                  <td className="tnum py-2 pr-3 text-right text-ink-2">
                    {nf.format(run.rows_in)}
                  </td>
                  <td className="tnum py-2 pr-3 text-right text-ink-2">
                    {nf.format(run.rows_out)}
                  </td>
                  {/* Le taux est écrit en blanc comme le reste du tableau : un
                      chiffre coloré se lit moins bien que le chiffre lui-même,
                      et la colonne « Statut » porte déjà l'état du run. */}
                  <td className="tnum py-2 pr-3 text-right text-ink">
                    {pf.format(run.publishable_rate)}
                  </td>
                  <td className="py-2 pr-3 text-ink-3">{run.triggered_by ?? "—"}</td>
                  <td className="py-2 pr-3 text-ink-3">
                    {new Date(run.started_at).toLocaleString("fr-FR")}
                  </td>
                  <td className="py-2 text-right">
                    <Button
                      variant="ghost"
                      title="Supprimer ce traitement"
                      onClick={() => {
                        if (!confirm(`Supprimer le traitement de ${run.store_id} ? Les fiches et l'historique de corrections qu'il a produits seront effacés.`))
                          return;
                        api
                          .deleteRun(run.id)
                          .then(() => {
                            // Un traitement supprime emporte ses fiches et son
                            // historique : tout ce qui en decoule doit etre
                            // redemande, pas seulement la liste des runs.
                            put(
                              "/api/runs",
                              runs.filter((r) => r.id !== run.id),
                            );
                            invalidate("/api/metrics");
                            invalidate("/api/products");
                            invalidate("/api/stores");
                            invalidate("/api/audit");
                            invalidate("/api/review/tasks");
                          })
                          .catch(() => undefined);
                      }}
                    >
                      Supprimer
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <StatTile
          label="Produits publiables"
          value={pf.format(metrics.publishable_rate)}
          tone={
            metrics.publishable_rate > 0.7
              ? "good"
              : metrics.publishable_rate > 0.4
                ? "warn"
                : "critical"
          }
          hint="EAN + image + taxonomie + TVA"
        />
        <StatTile
          label="Fiches au référentiel"
          value={nf.format(metrics.products_total)}
          hint="après fusion des doublons"
        />
        <StatTile label="Traitements" value={nf.format(metrics.runs_total)} hint={`${metrics.runs_last_7d} sur 7 jours`} />
        <StatTile
          label="Décisions en attente"
          value={nf.format(metrics.pending_reviews)}
          tone={metrics.pending_reviews > 0 ? "warn" : "good"}
          hint="groupées, pas ligne à ligne"
        />
        <StatTile
          label="Règles apprises"
          value={nf.format(metrics.learned_rules)}
          tone="good"
          hint="questions qui ne reviendront plus"
        />
      </div>

      {budget && <BudgetGauge budget={budget} />}

      <div className="grid gap-5 lg:grid-cols-[1.35fr_1fr]">
        <Card
          title="Taux de produits publiables"
          subtitle="Évolution au fil des traitements"
        >
          <TrendLine
            points={metrics.trend.map((point) => ({
              label: `${point.store_id} · ${new Date(point.started_at).toLocaleDateString("fr-FR")}`,
              value: point.publishable_rate,
              sub: `${nf.format(point.rows_in)} lignes reçues`,
            }))}
          />
        </Card>

        <Card
          title="Anomalies les plus fréquentes"
          subtitle="Cumul sur les 5 derniers traitements"
        >
          <BarList items={anomalyItems} total={anomalyTotal} />
        </Card>
      </div>

      <AuditTrail />
    </div>
  );
}

/**
 * Le journal d'audit, en bas du tableau de bord.
 *
 * La règle du projet est que toute valeur du référentiel doit pouvoir être
 * expliquée. Elle l'était déjà en base, mais nulle part à l'écran : on voyait
 * combien de corrections avaient été faites, jamais lesquelles. Un journal
 * qu'il faut une session SQL pour lire n'en est pas tout à fait un.
 */
function AuditTrail() {
  const [author, setAuthor] = useState<string>("");
  // Le filtre fait partie de la cle : revenir sur « HUMAN » reaffiche les
  // memes lignes sans les redemander.
  const { data: entries = [], loading } = useResource<AuditEntry[]>(
    `/api/audit?author=${author}&limit=40`,
    () => api.audit({ author: author || undefined, limit: 40 }),
    TTL.court,
  );

  const tons: Record<string, "info" | "serious" | "good"> = {
    RULE: "info",
    LLM: "serious",
    HUMAN: "good",
  };

  return (
    <Card
      title="Journal d'audit"
      subtitle="Chaque valeur modifiée, avec sa valeur d'origine et son auteur"
      action={
        <div className="flex gap-1">
          {["", "RULE", "LLM", "HUMAN"].map((valeur) => (
            <button
              key={valeur || "tous"}
              onClick={() => setAuthor(valeur)}
              className={`rounded-md border px-2 py-0.5 text-[11px] transition-colors ${
                author === valeur
                  ? "border-s1/60 bg-s1/10 text-ink"
                  : "border-line-strong bg-surface-2 text-ink-3 hover:bg-surface-3"
              }`}
            >
              {valeur || "tous"}
            </button>
          ))}
        </div>
      }
    >
      {loading ? (
        <div className="flex items-center gap-2 py-6 text-xs text-ink-3">
          <Spinner /> Chargement…
        </div>
      ) : entries.length === 0 ? (
        <EmptyState
          title="Aucune correction enregistrée"
          hint="Chaque valeur corrigée par une règle, par le LLM ou par un humain apparaîtra ici."
        />
      ) : (
        <div className="max-h-[26rem] overflow-auto">
          <table className="w-full min-w-[40rem] text-left text-xs">
            <thead className="sticky top-0 bg-surface-1">
              <tr className="border-b border-line text-ink-3">
                <th className="py-2 pr-3 font-medium">Quand</th>
                <th className="py-2 pr-3 font-medium">Auteur</th>
                <th className="py-2 pr-3 font-medium">Champ</th>
                <th className="py-2 pr-3 font-medium">Valeur d&apos;origine</th>
                <th className="py-2 font-medium">Valeur retenue</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {entries.map((entry) => (
                <tr key={entry.id} className="align-top">
                  <td className="whitespace-nowrap py-2 pr-3 text-ink-3">
                    {new Date(entry.created_at).toLocaleString("fr-FR", {
                      day: "2-digit",
                      month: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </td>
                  <td className="py-2 pr-3">
                    <Badge tone={tons[entry.author] ?? "neutral"}>{entry.author}</Badge>
                    {entry.user_email && (
                      <div className="mt-0.5 text-[10px] text-ink-3">{entry.user_email}</div>
                    )}
                  </td>
                  <td className="py-2 pr-3">
                    <div className="text-ink-2">{entry.field_name}</div>
                    <div className="text-[10px] text-ink-3">
                      {entry.store_id ?? "—"}
                      {entry.product_label ? ` · ${entry.product_label}` : ""}
                    </div>
                  </td>
                  <td className="py-2 pr-3 font-mono text-ink-3 line-through decoration-ink-3/40">
                    {entry.old_value || "(vide)"}
                  </td>
                  <td className="py-2 font-mono text-ink">{entry.new_value || "(vide)"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
        Les 40 dernières corrections, du plus récent au plus ancien. Une règle
        est déterministe et gratuite, le LLM propose sous seuil de confiance, un
        humain tranche ce dont l&apos;erreur coûte cher. Toute valeur du
        référentiel peut ainsi être expliquée, et défaite.
      </p>
    </Card>
  );
}
