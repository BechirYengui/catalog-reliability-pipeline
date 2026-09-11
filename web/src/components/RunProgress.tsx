import { useEffect, useState } from "react";
import type { PipelineStep, RunDetail } from "../lib/api";
import { Spinner, formatDuration, nf, pf } from "./ui";

/**
 * Suivi d'exécution en direct.
 *
 * Le temps restant est extrapolé du temps ÉCOULÉ et de l'avancement réel, pas
 * d'une durée codée en dur : un fichier de 200 lignes et un de 10 000 n'ont
 * rien à voir, et la vérification des images change tout. Une durée prédite à
 * partir d'une constante serait fausse dans les deux cas.
 *
 * L'estimation est affichée en ordre de grandeur (« environ 2 min »), pas à la
 * seconde près : donner « 1 min 47 s » sur une extrapolation à ±40 % prétend
 * une précision qu'on n'a pas.
 */
export function RunProgress({
  run,
  definition,
}: {
  run: RunDetail;
  definition: PipelineStep[];
}) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const startedAt = new Date(run.started_at).getTime();
  const elapsedMs = Math.max(now - startedAt, 0);
  const progress = Math.min(Math.max(run.progress, 0), 1);

  // Sous 8 %, l'extrapolation est trop bruitée pour valoir mieux que rien.
  const remainingMs =
    progress > 0.08 ? (elapsedMs / progress) * (1 - progress) : null;

  const steps = new Map(run.steps.map((s) => [s.step, s]));
  const done = run.steps.filter((s) => s.status === "DONE").length;
  const total = run.steps.filter((s) => s.status !== "SKIPPED").length || definition.length;

  const currentIndex = definition.findIndex((d) => d.key === run.current_step);
  const current = run.current_step
    ? definition[currentIndex]?.label ?? run.current_step
    : null;

  // Total des lignes/corrections/anomalies déjà accumulées : le funnel se
  // remplit sous les yeux plutôt qu'à la fin.
  const corrections = run.steps.reduce((sum, s) => sum + s.corrections, 0);
  const anomalies = run.steps.reduce((sum, s) => sum + s.anomalies, 0);
  const lastDone = [...run.steps].reverse().find((s) => s.status === "DONE");

  return (
    <div className="rounded-xl border border-s1/40 bg-s1/5 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="flex items-center gap-2 text-sm font-medium text-ink">
          <Spinner className="text-s1" />
          {current ? (
            <>
              Étape {Math.max(currentIndex + 1, 1)}/{definition.length} : {current}
            </>
          ) : (
            "Initialisation…"
          )}
        </span>
        <span className="tnum text-sm font-semibold text-s1">{pf.format(progress)}</span>
      </div>

      <div className="mt-2 h-2.5 overflow-hidden rounded bg-surface-2">
        <div
          className="h-2.5 rounded-r-[3px] bg-s1 transition-[width] duration-700 ease-out"
          style={{ width: `${Math.max(progress * 100, 2)}%` }}
        />
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
        <Cell label="Écoulé" value={formatDuration(elapsedMs)} />
        <Cell
          label="Restant (estimé)"
          value={remainingMs === null ? "—" : approximate(remainingMs)}
          hint={remainingMs === null ? "trop tôt pour estimer" : "ordre de grandeur"}
        />
        <Cell label="Étapes terminées" value={`${done} / ${total}`} />
        <Cell
          label="Lignes"
          value={run.rows_in ? nf.format(run.rows_in) : "lecture…"}
        />
      </dl>

      {(corrections > 0 || anomalies > 0) && (
        <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 border-t border-s1/20 pt-3 text-[11px]">
          <span className="text-ink-2">
            <span className="tnum font-semibold text-ink">{nf.format(corrections)}</span>{" "}
            corrections appliquées
          </span>
          <span className="text-ink-2">
            <span className="tnum font-semibold text-ink">{nf.format(anomalies)}</span>{" "}
            anomalies détectées
          </span>
          {lastDone && (
            <span className="text-ink-3">
              dernière étape : {lastDone.step} en {formatDuration(lastDone.duration_ms)}
            </span>
          )}
        </div>
      )}

      {run.network_enabled !== false && run.current_step === "field_checks" && (
        <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
          Vérification des images en cours : chaque URL distincte est interrogée,
          avec une limite de requêtes simultanées par domaine pour ne pas
          saturer les serveurs tiers. C&apos;est l&apos;étape la plus longue.
        </p>
      )}

      <ol className="mt-4 flex flex-col gap-1">
        {definition.map((d, index) => {
          const step = steps.get(d.key);
          const status = step?.status ?? "PENDING";
          const isCurrent = run.current_step === d.key;
          return (
            <li
              key={d.key}
              className={`flex items-center gap-2.5 rounded px-2 py-1 text-xs ${
                isCurrent ? "bg-s1/10" : ""
              }`}
            >
              <span className="tnum w-4 text-right text-[10px] text-ink-3">{index + 1}</span>
              <StatusDot status={status} isCurrent={isCurrent} />
              <span
                className={
                  status === "DONE"
                    ? "text-ink-2"
                    : isCurrent
                      ? "font-medium text-ink"
                      : "text-ink-3"
                }
              >
                {d.label}
              </span>
              {step && status === "DONE" && (
                <span className="tnum ml-auto whitespace-nowrap text-[11px] text-ink-3">
                  {nf.format(step.rows_in)} → {nf.format(step.rows_out)} ·{" "}
                  {formatDuration(step.duration_ms)}
                </span>
              )}
              {status === "SKIPPED" && (
                <span className="ml-auto text-[11px] text-ink-3">non applicable</span>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** Arrondit à un ordre de grandeur honnête plutôt qu'à la seconde. */
function approximate(ms: number): string {
  const seconds = ms / 1000;
  if (seconds < 20) return "quelques secondes";
  if (seconds < 90) return "moins d'une minute";
  const minutes = Math.round(seconds / 60);
  return `environ ${minutes} min`;
}

function Cell({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-ink-3">{label}</dt>
      <dd className="tnum mt-0.5 font-semibold text-ink">{value}</dd>
      {hint && <dd className="text-[10px] text-ink-3">{hint}</dd>}
    </div>
  );
}

function StatusDot({ status, isCurrent }: { status: string; isCurrent: boolean }) {
  if (isCurrent || status === "RUNNING")
    return <Spinner className="h-3 w-3 shrink-0 text-s1" />;
  if (status === "DONE")
    return (
      <svg width="12" height="12" viewBox="0 0 16 16" className="shrink-0" aria-hidden="true">
        <path
          d="m3.5 8.5 3 3 6-7"
          stroke="var(--status-good)"
          strokeWidth="2.5"
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  if (status === "FAILED")
    return <span className="h-2 w-2 shrink-0 rounded-full bg-critical" aria-hidden="true" />;
  return (
    <span
      className="h-2 w-2 shrink-0 rounded-full border border-line-strong"
      aria-hidden="true"
    />
  );
}

/**
 * Le pipeline tel qu'il sera parcouru, affiché AVANT le dépôt.
 * Savoir ce qui va se passer avant de lancer vaut mieux que le découvrir après.
 */
export function PipelinePreview({
  definition,
  checkUrls,
}: {
  definition: PipelineStep[];
  checkUrls: boolean;
}) {
  const families = {
    rules: { label: "Règles", color: "var(--series-1)" },
    semantic: { label: "LLM", color: "var(--series-2)" },
    human: { label: "Humain", color: "var(--series-3)" },
  } as const;

  return (
    <div className="rounded-xl border border-line bg-surface-1">
      <header className="border-b border-line px-4 py-3">
        <h2 className="text-sm font-semibold text-ink">Le parcours du fichier</h2>
        <p className="mt-0.5 text-xs text-ink-3">
          Les {definition.length} étapes, dans l&apos;ordre. Chacune annote le fichier
          et journalise ses corrections.
        </p>
      </header>
      <ol className="flex flex-col divide-y divide-[var(--border)]">
        {definition.map((step, index) => {
          const family = families[step.family] ?? families.rules;
          const slow = step.key === "field_checks" && checkUrls;
          return (
            <li key={step.key} className="flex gap-3 px-4 py-2.5">
              <span className="tnum mt-0.5 w-4 shrink-0 text-right text-[11px] text-ink-3">
                {index + 1}
              </span>
              <span
                className="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                style={{ background: family.color }}
                aria-hidden="true"
              />
              <div className="min-w-0">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="text-xs font-medium text-ink">{step.label}</span>
                  <span className="text-[10px] uppercase tracking-wide text-ink-3">
                    {family.label}
                  </span>
                  {slow && (
                    <span className="text-[10px] text-warn">
                      l&apos;étape la plus longue
                    </span>
                  )}
                </div>
                <p className="mt-0.5 text-[11px] leading-snug text-ink-3">
                  {step.description}
                </p>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
