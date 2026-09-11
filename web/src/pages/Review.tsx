import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type ReviewTask, type User } from "../lib/api";
import { TTL, invalidate, put, useResource } from "../lib/cache";

/** Une seule cle pour la file : c'est elle qu'on relit d'une page a l'autre. */
const TASKS_KEY = "/api/review/tasks?status_filter=pending";
import { Badge, Button, Card, EmptyState, Spinner, nf, pf } from "../components/ui";

/**
 * File de revue.
 *
 * Le point qui rend la revue tenable : une tâche porte sur un GROUPE de lignes,
 * jamais sur une ligne. Les centaines de lignes d'un même whisky produisent UNE
 * question, pas des dizaines d'arbitrages isolés.
 *
 * Raccourcis clavier parce que l'objectif est de traiter vingt décisions en
 * moins de deux minutes : à la souris, c'est hors d'atteinte.
 */
export default function Review({ user }: { user: User }) {
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(0);

  // La file vit dans le cache partage : revenir de la fiche produit qu'on
  // vient d'aller verifier ne rejoue pas la requete et ne perd pas sa place.
  const {
    data: tasks = [],
    loading,
    reload,
  } = useResource<ReviewTask[]>(TASKS_KEY, () => api.reviewTasks("pending"), TTL.court);

  const task = tasks[index];

  const decide = useCallback(
    async (action: "approve" | "reject" | "edit", value?: string) => {
      if (!task || busy) return;
      setBusy(true);
      setError(null);
      try {
        await api.decide(task.id, action, value);
        setDone((d) => d + 1);
        // La tache traitee sort de la file tout de suite, cache compris : la
        // reafficher le temps d'un aller-retour serait un mensonge de plus
        // court terme, mais un mensonge quand meme.
        put(
          TASKS_KEY,
          tasks.filter((row) => row.id !== task.id),
        );
        setIndex((i) => Math.min(i, Math.max(tasks.length - 2, 0)));
        // Une decision change le referentiel et les compteurs : ce qui est
        // affiche ailleurs ne doit pas rester sur l'ancienne version. Les
        // compteurs par magasin en font partie — approuver une correction
        // rend des fiches publiables, et c'est le chiffre qu'ils portent.
        for (const prefix of ["/api/products", "/api/stores", "/api/metrics", "/api/audit"]) {
          invalidate(prefix);
        }
      } catch (exc) {
        setError(
          exc instanceof ApiError && exc.status === 403
            ? "Cette décision touche à la TVA : elle demande un rôle administrateur."
            : "La décision n'a pas pu être enregistrée.",
        );
      } finally {
        setBusy(false);
      }
    },
    [task, busy, tasks],
  );

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.target instanceof HTMLInputElement) return;
      if (event.key === "a") decide("approve");
      if (event.key === "r") decide("reject");
      if (event.key === "j" || event.key === "ArrowDown")
        setIndex((i) => Math.min(i + 1, tasks.length - 1));
      if (event.key === "k" || event.key === "ArrowUp") setIndex((i) => Math.max(i - 1, 0));
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [decide, tasks.length]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-16 text-ink-3">
        <Spinner /> Chargement de la file…
      </div>
    );
  }

  if (tasks.length === 0) {
    return (
      <Card title="File de revue">
        <EmptyState
          title={done > 0 ? `${done} décision(s) traitée(s). La file est vide.` : "Aucune décision en attente"}
          hint="Chaque décision validée enrichit les règles apprises : la même question ne sera plus posée lors des prochains dépôts."
        />
        {/* Une file vide peut l'etre depuis dix secondes : le bouton donne le
            moyen de verifier sans recharger la page entiere. */}
        <div className="flex justify-center">
          <Button variant="ghost" onClick={reload}>
            Rechercher des décisions
          </Button>
        </div>
      </Card>
    );
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[20rem_1fr]">
      <Card
        title={`File d'attente (${tasks.length})`}
        subtitle={done > 0 ? `${done} traitée(s) dans cette session` : "Triée par nombre de lignes concernées"}
      >
        <ul className="flex max-h-64 flex-col gap-1 overflow-auto lg:max-h-[32rem]">
          {tasks.map((row, i) => (
            <li key={row.id}>
              <button
                onClick={() => setIndex(i)}
                className={`w-full rounded-lg border px-2.5 py-2 text-left transition-colors ${
                  i === index
                    ? "border-s1/60 bg-s1/10"
                    : "border-transparent hover:bg-surface-2"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-xs font-medium text-ink">{row.title}</span>
                  <span className="tnum shrink-0 text-[11px] font-semibold text-warn">
                    {nf.format(row.affected_rows)}
                  </span>
                </div>
                <div className="mt-1 flex items-center gap-1.5">
                  <Badge tone={row.requires_admin ? "critical" : "neutral"}>
                    {row.field_name}
                  </Badge>
                  <span className="text-[11px] text-ink-3">{row.store_id}</span>
                </div>
              </button>
            </li>
          ))}
        </ul>

        <div className="mt-3 border-t border-line pt-3 text-[11px] text-ink-3">
          <p className="font-medium text-ink-2">Raccourcis</p>
          <ul className="mt-1 space-y-0.5">
            <li>
              <Kbd>a</Kbd> approuver · <Kbd>r</Kbd> rejeter
            </li>
            <li>
              <Kbd>j</Kbd> / <Kbd>k</Kbd> naviguer
            </li>
          </ul>
        </div>
      </Card>

      {task && <TaskPanel task={task} user={user} busy={busy} error={error} onDecide={decide} />}
    </div>
  );
}

function TaskPanel({
  task,
  user,
  busy,
  error,
  onDecide,
}: {
  task: ReviewTask;
  user: User;
  busy: boolean;
  error: string | null;
  onDecide: (action: "approve" | "reject" | "edit", value?: string) => void;
}) {
  const [customValue, setCustomValue] = useState("");
  const distribution = Object.entries(task.context.distribution ?? {}).sort(
    (a, b) => b[1] - a[1],
  );
  const total = distribution.reduce((sum, [, count]) => sum + count, 0) || 1;
  const blocked = task.requires_admin && user.role !== "admin";
  // Un doublon possible n'est pas une question de valeur mais d'identité :
  // « est-ce le même produit ? ». Ni distribution, ni taux à appliquer.
  const doublon = task.kind === "possible_duplicate";

  return (
    <Card
      title={task.title}
      subtitle={
        doublon
          ? "Le pipeline a refusé de trancher : trop proches pour être séparées, pas assez pour être fusionnées"
          : `${nf.format(task.context.total_rows ?? 0)} lignes du fichier décrivent ce produit`
      }
      action={
        task.requires_admin ? (
          <Badge tone="critical">décision fiscale · admin</Badge>
        ) : (
          <Badge tone="info">{doublon ? "doublon possible" : task.field_name}</Badge>
        )
      }
    >
      <p className="text-sm leading-relaxed text-ink">{task.question}</p>

      {doublon && (
        <div className="mt-4 grid gap-2 sm:grid-cols-2">
          {[
            String(task.context.left_label ?? task.title),
            String(task.context.right_label ?? task.current_value ?? ""),
          ].map((libelle, i) => (
            <div key={i} className="rounded-lg border border-line bg-surface-2/50 p-3">
              <div className="text-[11px] uppercase tracking-wide text-ink-3">
                Fiche {i + 1}
              </div>
              <div className="mt-1 font-mono text-xs text-ink">{libelle}</div>
            </div>
          ))}
          <p className="col-span-full text-[11px] leading-snug text-ink-3">
            Fusionner est réversible et laisse une trace. Rejeter garde les deux
            fiches séparées. Dans les deux cas la question ne sera plus posée au
            prochain dépôt.
          </p>
        </div>
      )}

      {distribution.length > 0 && (
        <div className="mt-4 rounded-lg border border-line bg-surface-2/50 p-3">
          <p className="text-xs font-medium text-ink-2">Répartition observée</p>
          <div className="mt-2 flex flex-col gap-1.5">
            {distribution.map(([rate, count], i) => (
              <div key={rate} className="grid grid-cols-[3rem_1fr_auto] items-center gap-2">
                <span className="tnum text-xs font-semibold text-ink">{rate} %</span>
                <div className="h-2.5 rounded bg-surface-3">
                  <div
                    className="h-2.5 rounded-r-[3px]"
                    style={{
                      width: `${(count / total) * 100}%`,
                      background: i === 0 ? "var(--status-good)" : "var(--status-warning)",
                    }}
                  />
                </div>
                <span className="tnum whitespace-nowrap text-right text-[11px] text-ink-2 sm:text-xs">
                  {nf.format(count)} · {pf.format(count / total)}
                </span>
              </div>
            ))}
          </div>
          <p className="mt-2.5 text-[11px] leading-snug text-ink-3">
            Le taux majoritaire fait référence, mais il n&apos;est jamais appliqué
            sans votre accord : une TVA fausse a une conséquence comptable et
            fiscale réelle.
          </p>
        </div>
      )}

      <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-2">
        <div>
          <dt className="text-ink-3">Taxonomie</dt>
          <dd className="mt-0.5 text-ink-2">
            {(task.context.taxonomy ?? []).filter(Boolean).join(" › ") || "—"}
          </dd>
        </div>
        <div>
          <dt className="text-ink-3">EAN retenu</dt>
          <dd className="mt-0.5 font-mono text-ink-2">{task.context.ean ?? "—"}</dd>
        </div>
        <div>
          <dt className="text-ink-3">Source de la proposition</dt>
          <dd className="mt-0.5 text-ink-2">
            {task.source === "RULE" ? "Règle déterministe" : task.source}
            {" · "}confiance {pf.format(task.confidence)}
          </dd>
        </div>
        <div>
          <dt className="text-ink-3">Lignes concernées</dt>
          <dd className="tnum mt-0.5 text-ink-2">{nf.format(task.affected_rows)}</dd>
        </div>
      </dl>

      {error && (
        <p
          role="alert"
          className="mt-4 rounded-lg border border-critical/40 bg-critical/10 px-3 py-2 text-xs text-critical"
        >
          {error}
        </p>
      )}

      {blocked && (
        <p className="mt-4 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
          Votre rôle ne permet pas de trancher une décision de TVA. Un
          administrateur doit la valider.
        </p>
      )}

      <div className="mt-5 flex flex-col gap-2 border-t border-line pt-4 sm:flex-row sm:flex-wrap sm:items-center">
        <Button
          variant="primary"
          disabled={busy || blocked}
          onClick={() => onDecide("approve")}
          title="Raccourci : a"
        >
          {busy ? <Spinner /> : null}
          {doublon ? "Même produit, fusionner" : `Appliquer ${task.proposed_value} %`}{" "}
          <Kbd className="ml-1">a</Kbd>
        </Button>
        <Button disabled={busy || blocked} onClick={() => onDecide("reject")} title="Raccourci : r">
          {doublon ? "Produits différents" : "Rejeter"} <Kbd className="ml-1">r</Kbd>
        </Button>

        <div className="flex items-center gap-2 sm:ml-auto">
          <input
            value={customValue}
            onChange={(e) => setCustomValue(e.target.value)}
            placeholder="autre taux"
            inputMode="decimal"
            className="w-28 rounded-lg border border-line-strong bg-surface-2 px-2.5 py-1.5 text-xs text-ink outline-none focus:border-s1"
          />
          <Button
            disabled={busy || blocked || !customValue}
            onClick={() => onDecide("edit", customValue)}
          >
            Appliquer
          </Button>
        </div>
      </div>

      <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
        Votre décision est appliquée au référentiel, tracée dans le journal
        d&apos;audit avec votre identité, et enregistrée comme règle apprise :         la même question ne reviendra plus au prochain dépôt de ce magasin.
      </p>
    </Card>
  );
}

function Kbd({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return (
    <kbd
      className={`rounded border border-line-strong bg-surface-3 px-1 py-0.5 font-mono text-[10px] text-ink-2 ${className}`}
    >
      {children}
    </kbd>
  );
}
