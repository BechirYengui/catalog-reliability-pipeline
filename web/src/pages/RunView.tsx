import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  subscribeToRun,
  type Anomaly,
  type PipelineStep,
  type Run,
  type RunDetail,
  type Step,
  type User,
  type LlmMode,
  type RunSummary,
} from "../lib/api";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  Spinner,
  StatTile,
  formatDuration,
  nf,
  pf,
} from "../components/ui";
import { BarList, Funnel, StackedShare } from "../components/charts";
import { LlmUsageCard } from "../components/LlmUsage";
import { PipelinePreview, RunProgress } from "../components/RunProgress";
import { TTL, invalidate, useResource } from "../lib/cache";

/**
 * Ce que fait chaque code d'anomalie, en clair, et qui le traite.
 *
 * Un code technique seul (`ean_internal_gs1`) ne dit rien à un utilisateur
 * métier. Cette table est ce qui transforme un compteur en information
 * exploitable : quoi, combien, corrigé par qui, et pourquoi.
 */
const ANOMALY_INFO: Record<
  string,
  { label: string; tier: "rules" | "llm" | "human"; why: string }
> = {
  mojibake_fixed: {
    label: "Encodage cassé réparé",
    tier: "rules",
    why: "« CAFÃ‰ » relu comme « CAFÉ ». Réparation mécanique, sans risque.",
  },
  marketing_suffix_detached: {
    label: "Suffixe marketing détaché",
    tier: "rules",
    why: "« LESSIVE 2LORIGINE ITALIA » → « LESSIVE 2L » + origine. Sinon la déduplication compare du bruit.",
  },
  ean_padded: {
    label: "EAN, zéros de tête restaurés",
    tier: "rules",
    why: "Corrigé uniquement si la clé de contrôle GS1 redevient valide après réparation.",
  },
  ean_unpadded: {
    label: "EAN, zéros en trop retirés",
    tier: "rules",
    why: "Passage sous Excel typique. Même garantie, on ne garde que si le checksum retombe juste.",
  },
  ean_internal_gs1: {
    label: "Code interne (préfixe GS1 réservé)",
    tier: "rules",
    why: "Checksum valide mais préfixe 2, usage interne au magasin, pas un identifiant universel. Routé, pas corrigé.",
  },
  ean_gtin14: {
    label: "Code carton (GTIN-14)",
    tier: "human",
    why: "Le premier chiffre est l'indicateur de conditionnement : c'est le carton, pas l'unité vendue. Une marketplace attend l'EAN-13 consommateur. Signalé, jamais converti : en déduire le GTIN-13 demanderait d'inventer un chiffre de contrôle.",
  },
  ean_not_a_gtin: {
    label: "Code court (PLU / balance)",
    tier: "rules",
    why: "Ce n'est pas un EAN. Le « réparer » serait inventer une donnée, il change de colonne.",
  },
  ean_missing: {
    label: "EAN absent",
    tier: "human",
    why: "Rien à corriger automatiquement. Résolution possible via un référentiel externe, sinon signalé.",
  },
  ean_invalid: {
    label: "EAN irrécupérable",
    tier: "human",
    why: "Signalé, jamais deviné.",
  },
  url_scheme_repaired: {
    label: "URL, schéma réparé",
    tier: "rules",
    why: "« htp:// » → « https:// », « www.x » → « https://www.x ». Faute de frappe déterministe.",
  },
  url_malformed: {
    label: "URL inexploitable",
    tier: "human",
    why: "« ftp:// » ou « not-a-url » : aucune marketplace ne les consomme. Pas de réparation possible.",
  },
  url_missing: {
    label: "Image absente",
    tier: "human",
    why: "Un produit sans visuel conforme est mal vendu, voire non publié.",
  },
  url_unreachable: {
    label: "Image morte",
    tier: "rules",
    why: "Vérifié par requête HEAD. Le produit n'est pas publiable en l'état.",
  },
  url_domain_unresolvable: {
    label: "Domaine d'images injoignable",
    tier: "rules",
    why: "Le DNS ne résout pas, une seule vérification invalide toutes les URLs du domaine.",
  },
  taxonomy_gap: {
    label: "Trou dans la hiérarchie",
    tier: "rules",
    why: "Niveau 2 vide alors que 3 et 4 sont remplis.",
  },
  taxonomy_gap_filled: {
    label: "Trou comblé par déduction",
    tier: "rules",
    why: "La feuille détermine le chemin sans ambiguïté, aucun appel LLM nécessaire.",
  },
  taxonomy_incomplete: {
    label: "Taxonomie tronquée",
    tier: "llm",
    why: "Seul le niveau 1 est renseigné. Le LLM propose le chemin, en choisissant dans le référentiel fermé.",
  },
  taxonomy_name_mismatch: {
    label: "Taxonomie contredisant le nom",
    tier: "human",
    why: "« WHISKY » rangé en LESSIVE. Détecté par mots-clés, tranché par un humain.",
  },
  taxonomy_unknown_path: {
    label: "Chemin hors référentiel",
    tier: "human",
    why: "Créer une catégorie n'est jamais automatique.",
  },
  vat_category_mismatch: {
    label: "TVA incohérente avec le produit",
    tier: "human",
    why: "Taux légal appliqué au mauvais produit. JAMAIS corrigé automatiquement, conséquence fiscale.",
  },
  vat_missing: {
    label: "TVA absente",
    tier: "human",
    why: "Aucun taux sur la ligne. Le déduire de la catégorie serait une décision fiscale prise par une machine.",
  },
  vat_illegal_rate: {
    label: "Taux hors barème légal",
    tier: "human",
    why: "Signalé, jamais fixé seul.",
  },
  label_empty: {
    label: "Libellé vide",
    tier: "human",
    why: "Sans libellé, aucune étape en aval ne peut travailler.",
  },
};

const TIER_COLOR = {
  rules: "var(--series-1)",
  llm: "var(--series-2)",
  human: "var(--series-3)",
} as const;

const TIER_LABEL = {
  rules: "Règles",
  llm: "LLM",
  human: "Humain",
} as const;

export default function RunView({ user }: { user: User }) {
  const { runId } = useParams();
  return runId ? <RunDetailView runId={runId} user={user} /> : <UploadView />;
}

/* ------------------------------------------------------------------ dépôt */

function UploadView() {
  const [file, setFile] = useState<File | null>(null);
  const [storeId, setStoreId] = useState("");
  const [checkUrls, setCheckUrls] = useState(true);
  const [llmMode, setLlmMode] = useState<LlmMode>("live");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const navigate = useNavigate();

  const { data: runs = [] } = useResource<Run[]>("/api/runs", api.runs);
  // La description des etapes ne change jamais d'une session a l'autre : elle
  // etait pourtant retelechargee a chaque passage sur la page.
  const { data: definition = [] } = useResource<PipelineStep[]>(
    "/api/pipeline",
    api.pipeline,
    TTL.fixe,
  );

  async function submit() {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const run = await api.upload(file, storeId, checkUrls, llmMode);
      // Un depot de plus : la liste des traitements et les indicateurs ne
      // doivent pas rester sur la version d'avant.
      invalidate("/api/runs");
      invalidate("/api/metrics");
      navigate(`/runs/${run.id}`);
    } catch {
      setError("Le dépôt a échoué. Vérifiez le fichier et réessayez.");
      setBusy(false);
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[1.3fr_1fr] lg:gap-5">
      <Card
        title="Déposer un catalogue"
        subtitle="Un CSV par magasin et par jour. Le traitement démarre immédiatement."
      >
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            const dropped = e.dataTransfer.files?.[0];
            if (dropped) setFile(dropped);
          }}
          className={`flex flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 transition-colors ${
            dragging ? "border-s1 bg-s1/5" : "border-line-strong bg-surface-2/40"
          }`}
        >
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M12 16V4m0 0L8 8m4-4 4 4M4 17v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2"
              stroke="var(--text-muted)"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          {file ? (
            <>
              <p className="mt-3 text-sm font-medium text-ink">{file.name}</p>
              <p className="text-xs text-ink-3">{nf.format(Math.round(file.size / 1024))} Ko</p>
            </>
          ) : (
            <p className="mt-3 text-sm text-ink-2">
              Glissez le fichier ici, ou{" "}
              <label className="cursor-pointer font-medium text-s1 underline underline-offset-2">
                parcourez
                <input
                  type="file"
                  accept=".csv,text/csv"
                  className="hidden"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                />
              </label>
            </p>
          )}
        </div>

        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <label className="block">
            <span className="text-xs font-medium text-ink-2">
              Magasin <span className="text-critical">*</span>
            </span>
            <input
              value={storeId}
              onChange={(e) => setStoreId(e.target.value)}
              required
              placeholder="Franprix_Paris"
              aria-invalid={storeId.trim() === "" ? true : undefined}
              className="mt-1.5 w-full rounded-lg border border-line-strong bg-surface-2 px-3 py-2 text-sm text-ink outline-none focus:border-s1"
            />
            <span className="mt-1 block text-[11px] leading-snug text-ink-3">
              Obligatoire. Le référentiel est cloisonné par magasin, et ce nom
              décide dans quel catalogue le fichier atterrit. Lettres, chiffres,
              tiret et souligné.
            </span>
          </label>

          <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-line-strong bg-surface-2 px-3 py-2">
            <input
              type="checkbox"
              checked={checkUrls}
              onChange={(e) => setCheckUrls(e.target.checked)}
              className="mt-0.5 accent-[var(--series-1)]"
            />
            <span>
              <span className="block text-xs font-medium text-ink">Vérifier les images</span>
              <span className="block text-[11px] leading-snug text-ink-3">
                Interroge chaque URL. <strong>Sans cette vérification, aucun
                produit ne peut être déclaré publiable</strong> : une image non
                contrôlée ne prouve rien. Compte quelques minutes au lieu de
                quelques secondes.
              </span>
            </span>
          </label>
        </div>

        <fieldset className="mt-4">
          <legend className="text-xs font-medium text-ink-2">
            Traitement par le LLM
          </legend>
          <p className="mt-0.5 text-[11px] leading-snug text-ink-3">
            Même modèle, même réponse dans les deux cas. Ce qui change, c&apos;est
            le délai : Anthropic facture moitié prix les requêtes qu&apos;il peut
            traiter quand il veut.
          </p>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <ModeChoice
              active={llmMode === "live"}
              onSelect={() => setLlmMode("live")}
              title="Immédiat"
              price="plein tarif"
              detail="La réponse arrive tout de suite. À choisir quand vous attendez le résultat."
            />
            <ModeChoice
              active={llmMode === "batch"}
              onSelect={() => setLlmMode("batch")}
              title="Économique"
              price="−50 %"
              detail="Traitement différé : souvent quelques minutes, 24 h au maximum. À choisir quand personne n'attend."
            />
          </div>
        </fieldset>

        {error && (
          <p className="mt-4 rounded-lg border border-critical/40 bg-critical/10 px-3 py-2 text-xs text-critical">
            {error}
          </p>
        )}

        <Button
          variant="primary"
          disabled={!file || busy || storeId.trim() === ""}
          onClick={submit}
          className="mt-4 w-full"
        >
          {busy ? (
            <>
              <Spinner /> Envoi…
            </>
          ) : (
            "Lancer le traitement"
          )}
        </Button>

        <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
          Le traitement est idempotent : redéposer le même fichier ne crée pas de
          doublon, il renvoie le run existant.
        </p>
      </Card>

      <div className="flex flex-col gap-5">
      <Card title="Traitements récents" subtitle="Cliquez pour ouvrir le détail">
        {runs.length === 0 ? (
          <EmptyState title="Aucun fichier traité pour l'instant" />
        ) : (
          <ul className="flex flex-col divide-y divide-[var(--border)]">
            {runs.slice(0, 12).map((run) => (
              <li key={run.id}>
                <button
                  onClick={() => navigate(`/runs/${run.id}`)}
                  className="flex w-full items-center gap-3 px-1 py-2.5 text-left hover:bg-surface-2"
                >
                  <RunStatusBadge status={run.status} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-xs font-medium text-ink">{run.store_id}</div>
                    <div className="text-[11px] text-ink-3">
                      {new Date(run.started_at).toLocaleString("fr-FR")}
                    </div>
                  </div>
                  {/* Ce taux porte sur les LIGNES reçues, pas sur les fiches :
                      c'est la santé du fichier déposé. Le compte de fiches
                      publiables est sur la page du traitement. */}
                  <div className="tnum text-right text-xs">
                    <div className="font-semibold text-ink">{pf.format(run.publishable_rate)}</div>
                    <div className="text-[11px] text-ink-3">
                      lignes exploitables sur {nf.format(run.rows_in)}
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {definition.length > 0 && (
        <PipelinePreview definition={definition} checkUrls={checkUrls} />
      )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------- exécution */

function RunDetailView({ runId, user }: { runId: string; user: User }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [batchState, setBatchState] = useState<Record<string, unknown> | null>(null);
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const navigate = useNavigate();
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    const detail = await api.run(runId);
    setRun(detail);
    return detail;
  }, [runId]);

  const { data: definition = [] } = useResource<PipelineStep[]>(
    "/api/pipeline",
    api.pipeline,
    TTL.fixe,
  );

  // Le detail d'un run n'est PAS mis en cache : il porte une progression en
  // direct, et en servir une version de quarante secondes reviendrait a figer
  // la barre a l'ecran.
  useEffect(() => {
    refresh().catch(() => undefined);
  }, [refresh]);

  // Progression en direct. Le flux SSE porte l'événement, et on rafraîchit
  // l'état complet depuis l'API : le flux dit QUAND, la base dit QUOI, ainsi
  // un événement perdu ne laisse jamais l'affichage désynchronisé.
  useEffect(() => {
    if (!run || ["COMPLETED", "QUARANTINE", "FAILED"].includes(run.status)) return;
    const stop = subscribeToRun(runId, (event) => {
      // Attendre un lot dure des minutes. Sans ce message, l'etape resterait
      // figee a l'ecran et l'utilisateur conclurait a une panne.
      const kind = String(event.type ?? "");
      if (kind.startsWith("batch.")) {
        setBatchState(event as Record<string, unknown>);
      }
      refresh().catch(() => undefined);
    });
    // Filet de sécurité si le flux est coupé par un proxy.
    pollRef.current = window.setInterval(() => refresh().catch(() => undefined), 3000);
    return () => {
      stop();
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [run?.status, runId, refresh, run]);

  useEffect(() => {
    if (run && ["COMPLETED", "QUARANTINE"].includes(run.status)) {
      api.anomalies(runId, selectedCode ?? undefined).then(setAnomalies).catch(() => undefined);
    }
  }, [run?.status, runId, selectedCode, run]);

  useEffect(() => {
    if (run && ["COMPLETED", "QUARANTINE"].includes(run.status)) {
      api.runSummary(runId).then(setSummary).catch(() => undefined);
    }
  }, [run?.status, runId, run]);

  // Un traitement qui s'acheve ecrit des fiches, des taches de revue et de la
  // depense LLM. Tout ce que les autres pages gardent en memoire vient d'etre
  // rendu faux : on le leur dit, plutot que de les laisser afficher l'etat
  // d'avant le run que l'utilisateur vient justement de regarder finir.
  useEffect(() => {
    if (!run || !["COMPLETED", "QUARANTINE", "FAILED"].includes(run.status)) return;
    for (const prefix of [
      "/api/runs",
      "/api/metrics",
      "/api/products",
      "/api/stores",
      "/api/review/tasks",
      "/api/llm/budget",
      "/api/audit",
    ]) {
      invalidate(prefix);
    }
  }, [run?.status]);

  if (!run) {
    return (
      <div className="flex items-center gap-2 py-16 text-ink-3">
        <Spinner /> Chargement du traitement…
      </div>
    );
  }

  const steps = new Map(run.steps.map((s) => [s.step, s]));
  const codes = run.report?.anomalies_by_code ?? {};
  const running = ["PENDING", "RUNNING"].includes(run.status);

  const anomalyItems = Object.entries(codes)
    .map(([code, value]) => ({
      label: code,
      value,
      hint: ANOMALY_INFO[code]?.why ?? code,
    }))
    .sort((a, b) => b.value - a.value);

  // ATTENTION : ces totaux comptent des ANOMALIES, pas des lignes. Une meme
  // ligne peut porter un EAN invalide, une URL cassee et un libelle vide : elle
  // compte trois fois. C'est ce qui affichait « 601 lignes signalees » sur un
  // fichier de 300 lignes.
  const byTier = { rules: 0, llm: 0, human: 0 };
  for (const [code, count] of Object.entries(codes)) {
    byTier[ANOMALY_INFO[code]?.tier ?? "rules"] += count;
  }

  // Le vrai volume de travail humain se compte en FICHES a trancher, pas en
  // anomalies : la file de revue pose une question par groupe de lignes, jamais
  // une par ligne. C'est le chiffre que l'utilisateur peut mettre en face de son
  // temps.
  const needsReview = steps.get("scoring_routing")?.counters?.NEEDS_REVIEW ?? 0;

  // « Publiable » se compte en FICHES, pas en lignes. Les deux mesures existent
  // et divergent : 81,5 % des lignes d'un fichier peuvent porter une TVA
  // cohérente alors qu'AUCUNE fiche ne l'est, parce que le conflit ne naît
  // qu'au regroupement. Le mot « produits » impose donc le compte de fiches —
  // c'est aussi le seul que l'export confirme, ligne par ligne.
  // Le comptage en base d'abord : c'est l'etat courant, celui que confirme le
  // CSV exporte, et celui qu'une validation humaine fait bouger. Le rapport ne
  // sert que de repli — et les runs anterieurs a ce champ n'en ont pas du tout,
  // ce qui affichait « 0 / 43 » sur un catalogue qui en publie 28.
  const recordsPublishable =
    summary?.records_publishable ?? run.report?.records_publishable ?? 0;
  const recordsTotal = summary?.records_total || run.report?.records_total || run.rows_out;
  const recordsRate = recordsTotal ? recordsPublishable / recordsTotal : 0;

  const dedup = steps.get("dedup_exact");
  const resolution = steps.get("entity_resolution");
  // Le compte final est celui de la DERNIERE etape qui reduit la cardinalite,
  // pas de la premiere : la resolution floue passe apres la dedup exacte.
  const finalRecords = run.rows_out;
  const funnelStages = [
    { label: "Lignes du fichier", value: run.rows_in },
    {
      label: "Après contrôles",
      value: steps.get("field_checks")?.rows_out ?? run.rows_in,
    },
    {
      label: "Après dédup exacte",
      value: dedup?.rows_out ?? run.rows_in,
    },
    {
      label: "Catalogue final",
      value: resolution?.rows_out ?? finalRecords,
      note: "toutes les fiches",
    },
    {
      label: "dont publiables",
      value: recordsPublishable,
      note: "prêtes à pousser",
    },
  ];

  // 3. Le nombre de libelles REELLEMENT envoyes au LLM vient de son propre
  //    compteur. Le deduire du nombre de fiches finales donnait un chiffre
  //    faux : et flatteur, ce qui est pire.
  const distinctLabels =
    steps.get("llm_enrich")?.counters?.distinct_labels ?? finalRecords;

  // Les corrections portent leur auteur : c'est la reponse directe a
  // « qui corrige quoi ». Les anomalies repondent a une autre question (  // « qu'est-ce qui n'allait pas »), et un fichier propre en a zero tout en
  // ayant des corrections (un libelle abrege n'est pas casse, il est illisible).
  // Nombre de controles reellement passes : « 0 correction » se lit comme
  // « n'a rien fait », alors qu'un controle qui ne trouve rien a fait son
  // travail. On montre donc le volume verifie a cote du volume corrige.
  const fieldChecks = steps.get("field_checks")?.counters ?? {};
  const checksRun = Object.entries(fieldChecks)
    .filter(([key]) => key.startsWith("ean_") || key.startsWith("url_") || key.startsWith("vat_"))
    .reduce((sum, [, value]) => sum + value, 0);

  // La repartition du RAPPORT est figee a la fin du traitement : l'etage humain
  // y vaut toujours zero, puisque decisions de revue, fusions et corrections a
  // la main arrivent forcement apres. On prefere donc le comptage en base, qui
  // dit l'etat courant, et on retombe sur le rapport s'il n'a pas repondu.
  const authors = summary?.corrections_by_author ?? run.report?.corrections_by_author ?? {};
  const corrByAuthor = {
    RULE: authors.RULE ?? 0,
    LLM: authors.LLM ?? 0,
    HUMAN: authors.HUMAN ?? 0,
  };
  // Les corrections APPLIQUEES, pas les anomalies. Une anomalie est signalee ;
  // toutes ne sont pas reparables, et la TVA ne l'est jamais d'office. Melanger
  // les deux compteurs surestimerait le travail reellement fait.
  const corrTotal = corrByAuthor.RULE + corrByAuthor.LLM + corrByAuthor.HUMAN;

  // Les DECISIONS, a ne pas confondre avec les lignes corrigees ci-dessus. Le
  // LLM decide sur des libelles DISTINCTS puis applique sa reponse a toutes
  // les lignes qui portent le meme libelle : 273 decisions deviennent 7 711
  // corrections. Compter les lignes pour repondre a « qui corrige quoi »
  // donnait 84 % au LLM et 16 % aux regles — l'inverse exact de qui a
  // tranche. Repli sur les lignes si l'API ne renvoie pas encore le compte,
  // pour qu'un ancien run continue d'afficher quelque chose de juste.
  const decisionSource =
    summary?.decisions_by_author ?? run.report?.decisions_by_author ?? null;
  const decByAuthor = {
    RULE: decisionSource?.RULE ?? corrByAuthor.RULE,
    LLM: decisionSource?.LLM ?? corrByAuthor.LLM,
    HUMAN: decisionSource?.HUMAN ?? corrByAuthor.HUMAN,
  };
  const hasDecisions = decisionSource !== null;
  // « ×28 » ne se dit que quand le rapport est reel : une decision qui ne
  // touche qu'une ligne n'a pas de facteur a annoncer.
  const linesHint = (lines: number, decisions: number) => {
    if (!hasDecisions || lines === 0) return undefined;
    const factor = lines / Math.max(decisions, 1);
    const suffix = factor >= 1.5 ? ` (×${factor.toFixed(0)})` : "";
    return `${nf.format(lines)} ligne(s) touchée(s)${suffix}`;
  };

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold text-ink">{run.store_id}</h1>
            <RunStatusBadge status={run.status} />
          </div>
          <p className="mt-1 text-xs text-ink-3">
            Déposé par {run.triggered_by ?? "—"} ·{" "}
            {new Date(run.started_at).toLocaleString("fr-FR")} · empreinte{" "}
            <span className="font-mono">{run.file_sha256.slice(0, 12)}</span>
          </p>
        </div>
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
          {/* Pas de « Nouveau fichier » ici : l'onglet « Traiter un fichier »
              y mène déjà, et un doublon de navigation vole la place des deux
              actions propres à cette page — les téléchargements. */}
          {/* L'original est téléchargeable dès le dépôt, sans attendre la fin :
              c'est la pièce de comparaison. Un export « fiabilisé » que l'on ne
              peut pas confronter au fichier d'entrée demande de le croire sur
              parole. */}
          <a href={api.sourceUrl(run.id)} download>
            {/* Bordure plutôt que fantôme : c'est une action secondaire du
                couple de téléchargements, pas de la navigation. */}
            <Button variant="default" title="Le CSV déposé, octet pour octet, avant traitement">
              Fichier d&apos;origine
            </Button>
          </a>
          {/* Rien à exporter d'un fichier en quarantaine : il n'a pas été
              intégré au référentiel, et proposer un « catalogue fiabilisé »
              qui serait vide ferait douter du message de quarantaine lui-même. */}
          {!running && run.status !== "QUARANTINE" && (
            <a href={api.exportUrl(run.id)} download>
              <Button variant="primary">Télécharger le catalogue fiabilisé</Button>
            </a>
          )}
        </div>
      </div>

      {running && <RunProgress run={run} definition={definition} />}

      {running && batchState && String(batchState.type).startsWith("batch.") && (
        <div className="rounded-xl border border-s1/40 bg-s1/10 p-4">
          <p className="text-sm font-medium text-ink">
            {batchState.type === "batch.timeout"
              ? "Le lot n'a pas répondu dans le délai imparti"
              : "Traitement par lots en cours chez Anthropic"}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-ink-2">
            {batchState.type === "batch.timeout" ? (
              <>
                Le pipeline continue sans l&apos;étage LLM. Le lot{" "}
                <span className="font-mono">{String(batchState.batch_id)}</span>{" "}
                reste récupérable : le travail est payé, il n&apos;est pas perdu.
              </>
            ) : (
              <>
                Ce mode coûte moitié prix parce que la réponse peut attendre.
                Souvent quelques minutes, 24 h au maximum.
                {typeof batchState.waited_s === "number" && (
                  <> En attente depuis {Math.round(batchState.waited_s / 60)} min.</>
                )}
              </>
            )}
          </p>
        </div>
      )}

      {run.status === "FAILED" && (
        <div className="rounded-xl border border-critical/40 bg-critical/10 p-4">
          <p className="text-sm font-semibold text-critical">Le traitement a échoué</p>
          <p className="mt-1 font-mono text-xs text-ink-2">{run.error}</p>
        </div>
      )}

      {run.report?.quarantined && (
        <div className="rounded-xl border border-warn/40 bg-warn/10 p-4">
          <p className="text-sm font-semibold text-warn">Fichier mis en quarantaine</p>
          <p className="mt-1 text-xs text-ink-2">
            {run.report.quarantine_reason} : aucune fiche n&apos;a été écrite au
            référentiel et aucune décision n&apos;a été mise en file de revue. Un
            tel écart signale presque toujours un changement de format côté
            magasin. Les anomalies ci-dessous, elles, sont conservées : ce sont
            elles qui expliquent le rejet.
          </p>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile label="Lignes reçues" value={nf.format(run.rows_in)} />
        <StatTile
          label="Fiches uniques"
          value={nf.format(run.rows_out)}
          hint={
            run.rows_in && run.rows_out
              ? `${(run.rows_in / run.rows_out).toFixed(1)}× de doublons absorbés`
              : undefined
          }
        />
        <StatTile
          label="Fiches publiables"
          value={`${nf.format(recordsPublishable)} / ${nf.format(recordsTotal)}`}
          tone={recordsRate > 0.7 ? "good" : recordsRate > 0.4 ? "warn" : "critical"}
          hint={
            corrTotal > 0
              ? `${nf.format(corrTotal)} corrections appliquées sur ce fichier`
              : "aucune correction appliquée sur ce fichier"
          }
        />
        <StatTile
          label="À valider par un humain"
          value={nf.format(needsReview)}
          tone={needsReview > 0 ? "warn" : "good"}
          hint={
            byTier.human > 0
              ? `fiches à trancher, portant ${nf.format(byTier.human)} anomalies`
              : "fiches à trancher, jamais corrigées seules"
          }
        />
      </div>

      <Card
        title="Le pipeline, étage par étage"
        subtitle="Chaque étape annote le fichier et journalise ses corrections, dans l’ordre où elles s’exécutent."
      >
        <ol className="flex flex-col gap-2">
          {definition.map((definitionStep) => (
            <StepRow
              key={definitionStep.key}
              definition={definitionStep}
              step={steps.get(definitionStep.key)}
              isCurrent={run.current_step === definitionStep.key}
            />
          ))}
        </ol>
      </Card>

      {!running && (
        <>
          <div className="grid gap-5 lg:grid-cols-2">
            <Card
              title="Entonnoir de traitement"
              subtitle="Ce que devient le fichier, du dépôt au catalogue"
            >
              <Funnel stages={funnelStages} />
              <p className="mt-4 border-t border-line pt-3 text-[11px] leading-relaxed text-ink-3">
                Le <strong className="text-ink-2">catalogue final</strong> est ce
                que contient le CSV exporté :{" "}
                {nf.format(finalRecords)} fiche(s), avec une colonne{" "}
                <code className="text-ink-2">publiable</code> à oui/non. Les{" "}
                <strong className="text-ink-2">publiables</strong> en sont le
                sous-ensemble prêt à pousser vers la marketplace : les autres ne
                sont pas jetées, elles attendent une correction.
              </p>
            </Card>

            <Card
              title="Qui corrige quoi"
              subtitle={
                hasDecisions
                  ? "Qui a pris la décision de corriger : une décision par valeur, pas par ligne"
                  : "Répartition des corrections selon l'étage qui les a produites"
              }
            >
              {corrByAuthor.RULE + corrByAuthor.LLM + corrByAuthor.HUMAN === 0 ? (
                <div className="py-6 text-center">
                  <p className="text-sm font-medium text-good">
                    Aucune correction nécessaire.
                  </p>
                  <p className="mx-auto mt-1 max-w-sm text-[11px] leading-relaxed text-ink-3">
                    Le fichier est arrivé propre : rien à réparer, rien à
                    reformuler. C&apos;est le résultat attendu : un pipeline qui
                    corrige ce qui n&apos;est pas cassé fait autant de dégâts
                    qu&apos;un pipeline qui laisse passer les vraies erreurs.
                  </p>
                </div>
              ) : (
              <StackedShare
                segments={[
                  {
                    label: "Règles déterministes",
                    value: decByAuthor.RULE,
                    color: TIER_COLOR.rules,
                    hint: linesHint(corrByAuthor.RULE, decByAuthor.RULE),
                  },
                  {
                    label: "Enrichissement LLM",
                    value: decByAuthor.LLM,
                    color: TIER_COLOR.llm,
                    hint: linesHint(corrByAuthor.LLM, decByAuthor.LLM),
                  },
                  {
                    label: "Validation humaine",
                    value: decByAuthor.HUMAN,
                    color: TIER_COLOR.human,
                    hint: linesHint(corrByAuthor.HUMAN, decByAuthor.HUMAN),
                  },
                ]}
              />
              )}
              {corrByAuthor.RULE === 0 && checksRun > 0 && (
                <p className="mt-3 rounded-lg border border-line bg-surface-2/50 px-3 py-2 text-[11px] leading-relaxed text-ink-2">
                  <strong>Zéro correction par les règles ne veut pas dire zéro
                  travail.</strong> Elles ont passé{" "}
                  {nf.format(checksRun)} contrôles sur ce fichier : checksum GS1,
                  schéma et disponibilité des images, taux légal, hiérarchie de
                  catégories : et n&apos;ont rien trouvé à réparer. Sur un fichier
                  abîmé, c&apos;est cet étage qui produit l&apos;essentiel des
                  corrections.
                </p>
              )}
              {hasDecisions && corrTotal > 0 && (
                <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
                  <strong className="text-ink-2">
                    Une décision, pas une ligne.
                  </strong>{" "}
                  Le LLM tranche sur des libellés distincts, puis sa réponse
                  s&apos;applique à toutes les lignes qui portent ce libellé.
                  Compter les lignes ferait passer l&apos;étage qui décide le
                  moins pour celui qui corrige le plus :{" "}
                  {nf.format(corrTotal)} lignes corrigées pour{" "}
                  {nf.format(decByAuthor.RULE + decByAuthor.LLM + decByAuthor.HUMAN)}{" "}
                  décisions.
                </p>
              )}
              <p className="mt-4 text-[11px] leading-relaxed text-ink-3">
                Chaque étage déblaie le terrain pour le suivant. Le LLM ne reçoit
                que ce que les règles n&apos;ont pas résolu, et il ne voit que des
                libellés <strong>distincts</strong> : {nf.format(distinctLabels)}{" "}
                pour {nf.format(run.rows_in)} lignes
                {distinctLabels < run.rows_in * 0.9
                  ? `, soit ${(run.rows_in / Math.max(distinctLabels, 1)).toFixed(1)}× moins d'appels`
                  : ". Ici chaque ligne porte un libellé différent, donc la déduplication ne joue pas et le coût est maximal"}
                .
              </p>
            </Card>
          </div>

          {/* Pas d'enveloppe cumulée ici. `report.llm_budget` est un instantané
              figé à la fin de ce run : sur un traitement d'hier il affiche un
              plafond périmé, et sur le dernier il répète le tableau de bord.
              Cette page répond à « combien a coûté CE fichier » — le reste est
              une question de flotte, elle est traitée là où elle se pose. */}
          <LlmUsageCard
            usage={run.report?.llm_usage}
            distinctLabels={distinctLabels}
            rowsIn={run.rows_in}
          />

          {/* Les deux cartes se lisent ensemble : une hauteur commune, et
              chacune défile chez elle. Sans plafond, le tableau de droite —
              cent lignes — étirait la paire sur trois écrans et laissait la
              liste de gauche flotter dans le vide. */}
          <div className="grid gap-5 lg:grid-cols-[1fr_1.1fr]">
            <Card
              title="Anomalies par type"
              subtitle="Cliquez pour voir les lignes concernées"
              className="flex h-full max-h-[34rem] flex-col"
              bodyClassName="flex flex-col"
            >
              {anomalyItems.length === 0 && (
                <p className="shrink-0 py-4 text-center text-xs leading-relaxed text-ink-3">
                  Rien à signaler sur ce fichier : les contrôles ont tourné et
                  n&apos;ont trouvé aucun problème. À ne pas confondre avec les{" "}
                  <strong className="text-ink-2">corrections</strong> ci-dessus :                   reformuler un libellé abrégé n&apos;est pas réparer une anomalie.
                </p>
              )}
              <div className="min-h-0 flex-1 overflow-auto">
                <BarList
                  items={anomalyItems}
                  total={Object.values(codes).reduce((a, b) => a + b, 0)}
                  selected={selectedCode}
                  onSelect={(code) => setSelectedCode(code === selectedCode ? null : code)}
                  colorOf={(code) => TIER_COLOR[ANOMALY_INFO[code]?.tier ?? "rules"]}
                />
              </div>
              <div className="mt-4 flex shrink-0 flex-wrap gap-x-4 gap-y-1 border-t border-line pt-3">
                {(["rules", "llm", "human"] as const).map((tier) => (
                  <span key={tier} className="flex items-center gap-1.5 text-[11px]">
                    <span
                      className="inline-block h-2.5 w-2.5 rounded-sm"
                      style={{ background: TIER_COLOR[tier] }}
                      aria-hidden="true"
                    />
                    <span className="text-ink-2">{TIER_LABEL[tier]}</span>
                  </span>
                ))}
              </div>
            </Card>

            <Card
              title={
                selectedCode
                  ? ANOMALY_INFO[selectedCode]?.label ?? selectedCode
                  : "Détail des anomalies"
              }
              subtitle={
                selectedCode
                  ? ANOMALY_INFO[selectedCode]?.why
                  : "Sélectionnez un type d'anomalie pour voir les lignes réelles"
              }
              action={
                selectedCode && (
                  <Button variant="ghost" onClick={() => setSelectedCode(null)}>
                    Tout afficher
                  </Button>
                )
              }
              // La liste des types, à gauche, est longue : sans cette hauteur
              // pleine, le tableau s'arrêtait au milieu de la carte et laissait
              // un vide sous lui alors qu'il avait des lignes à montrer.
              className="flex h-full max-h-[34rem] flex-col"
              bodyClassName="flex flex-col"
            >
              {anomalies.length === 0 ? (
                <EmptyState title="Aucune ligne à afficher" />
              ) : (
                <div className="min-h-0 flex-1 overflow-auto">
                  <table className="w-full min-w-[34rem] text-left text-xs">
                    <thead className="sticky top-0 bg-surface-1">
                      <tr className="text-ink-3">
                        {/* L'identifiant du magasin d'abord : c'est par lui que
                            l'utilisateur retrouve la ligne dans SON fichier.
                            Nommer la colonne « Ligne » le faisait passer pour un
                            numero d'ordre, et son debut d'UUID pour du charabia
                            hexadecimal. La colonne porte donc le nom exact de la
                            colonne du CSV. */}
                        <th
                          className="py-1.5 pr-3 font-medium"
                          title="La colonne id_produit du fichier déposé (un UUID). Seul son début est affiché ; survolez une valeur pour l'identifiant complet."
                        >
                          id_produit
                        </th>
                        <th className="py-1.5 pr-3 font-medium">Produit</th>
                        <th className="py-1.5 pr-3 font-medium">Champ</th>
                        <th className="py-1.5 font-medium">Valeur en cause</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-[var(--border)]">
                      {anomalies.slice(0, 100).map((anomaly, index) => (
                        <tr key={`${anomaly.row_id}-${index}`} className="align-top">
                          {/* Tronque pour tenir dans la colonne, mais l'entier
                              reste atteignable : c'est lui qu'on recopie pour
                              retrouver la ligne dans le fichier d'origine. */}
                          <td
                            className="py-1.5 pr-3 font-mono text-[11px] text-ink-2"
                            title={anomaly.row_id}
                          >
                            {anomaly.row_id.slice(0, 8)}
                            <span className="text-ink-3">…</span>
                          </td>
                          <td className="py-1.5 pr-3 text-ink">
                            {anomaly.label || <span className="text-ink-3">—</span>}
                          </td>
                          <td className="py-1.5 pr-3">
                            <span className="font-mono text-[11px] text-ink-2">
                              {anomaly.field_name}
                            </span>
                            {anomaly.severity !== "INFO" && (
                              <span className="ml-1.5 align-middle">
                                <Badge tone={anomaly.severity === "ERROR" ? "critical" : "warn"}>
                                  {anomaly.severity}
                                </Badge>
                              </span>
                            )}
                          </td>
                          <td className="py-1.5 text-ink-2">
                            {anomaly.value ? (
                              <span className="font-mono text-ink">{anomaly.value}</span>
                            ) : null}
                            {anomaly.value && anomaly.detail ? " — " : null}
                            {anomaly.detail}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {/* Un tableau tronqué qui ne le dit pas se lit comme un tableau
                  complet : l'utilisateur croirait avoir vu toutes les lignes. */}
              {anomalies.length > 0 && (
                <p className="mt-3 shrink-0 border-t border-line pt-2 text-[11px] text-ink-3">
                  {nf.format(Math.min(anomalies.length, 100))} ligne(s) affichée(s)
                  {selectedCode
                    ? ` sur ${nf.format(codes[selectedCode] ?? anomalies.length)} portant ce code`
                    : ""}
                  .
                </p>
              )}
            </Card>
          </div>

          {byTier.human > 0 && (
            <Card
              title="Pourquoi un humain doit intervenir"
              subtitle="Ces cas ne sont jamais corrigés automatiquement, quelle que soit la confiance"
              action={
                <Button variant="primary" onClick={() => navigate("/revue")}>
                  Ouvrir la file de revue
                </Button>
              }
            >
              <ul className="grid gap-2 md:grid-cols-2">
                {Object.entries(codes)
                  .filter(([code]) => ANOMALY_INFO[code]?.tier === "human")
                  .sort((a, b) => b[1] - a[1])
                  .map(([code, count]) => (
                    <li
                      key={code}
                      className="rounded-lg border border-line bg-surface-2/50 p-3"
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-xs font-semibold text-ink">
                          {ANOMALY_INFO[code].label}
                        </span>
                        <span className="tnum text-xs font-semibold text-warn">
                          {nf.format(count)}
                        </span>
                      </div>
                      <p className="mt-1 text-[11px] leading-snug text-ink-3">
                        {ANOMALY_INFO[code].why}
                      </p>
                    </li>
                  ))}
              </ul>
            </Card>
          )}
        </>
      )}

      {user.role !== "admin" && byTier.human > 0 && (
        <p className="text-[11px] text-ink-3">
          Votre rôle est « relecteur » : les décisions touchant à la TVA vous seront
          présentées mais devront être tranchées par un administrateur.
        </p>
      )}
    </div>
  );
}

function StepRow({
  definition,
  step,
  isCurrent,
}: {
  definition: PipelineStep;
  step?: Step;
  isCurrent: boolean;
}) {
  const status = step?.status ?? "PENDING";
  const done = status === "DONE";
  const counters = Object.entries(step?.counters ?? {}).filter(([, v]) => v > 0);

  return (
    <li
      className={`rounded-lg border p-3 transition-colors ${
        isCurrent
          ? "running-ring border-s1/60 bg-s1/5"
          : done
            ? "border-line bg-surface-2/40"
            : "border-line bg-surface-1"
      }`}
    >
      <div className="flex items-start gap-3">
        <StepIcon status={status} isCurrent={isCurrent} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium text-ink">{definition.label}</span>
            <Badge tone={definition.family === "semantic" ? "serious" : "info"}>
              {definition.family === "semantic" ? "sémantique" : "règles"}
            </Badge>
            {status === "SKIPPED" && <Badge tone="neutral">non exécutée</Badge>}
            {status === "FAILED" && <Badge tone="critical">échec</Badge>}
          </div>
          <p className="mt-0.5 text-[11px] leading-snug text-ink-3">
            {definition.description}
          </p>

          {done && counters.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {counters.map(([key, value]) => (
                <span
                  key={key}
                  className="rounded border border-line bg-surface-1 px-1.5 py-0.5 font-mono text-[10px] text-ink-2"
                >
                  {key} <span className="tnum font-semibold text-ink">{nf.format(value)}</span>
                </span>
              ))}
            </div>
          )}
          {step?.error && (
            <p className="mt-2 font-mono text-[11px] text-critical">{step.error}</p>
          )}
        </div>

        {done && step && (
          <div className="tnum shrink-0 text-right text-[11px] text-ink-3">
            <div className="text-ink-2">
              {nf.format(step.rows_in)} → {nf.format(step.rows_out)}
            </div>
            <div>{formatDuration(step.duration_ms)}</div>
            {step.corrections > 0 && (
              <div className="text-s1">{nf.format(step.corrections)} corrections</div>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function StepIcon({ status, isCurrent }: { status: string; isCurrent: boolean }) {
  if (isCurrent || status === "RUNNING") {
    return <Spinner className="mt-0.5 text-s1" />;
  }
  if (status === "DONE") {
    return (
      <svg width="16" height="16" viewBox="0 0 16 16" className="mt-0.5" aria-hidden="true">
        <circle cx="8" cy="8" r="7" fill="var(--status-good)" opacity="0.2" />
        <path
          d="m4.8 8.2 2.2 2.2 4.2-4.6"
          stroke="var(--status-good)"
          strokeWidth="1.8"
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  if (status === "FAILED") {
    return (
      <svg width="16" height="16" viewBox="0 0 16 16" className="mt-0.5" aria-hidden="true">
        <circle cx="8" cy="8" r="7" fill="var(--status-critical)" opacity="0.2" />
        <path
          d="M8 4.5v4M8 11h.01"
          stroke="var(--status-critical)"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    );
  }
  return (
    <span
      className="mt-1 inline-block h-2.5 w-2.5 shrink-0 rounded-full border border-line-strong"
      aria-hidden="true"
    />
  );
}

export function RunStatusBadge({ status }: { status: string }) {
  const map: Record<string, { tone: "good" | "warn" | "critical" | "info" | "neutral"; label: string }> = {
    COMPLETED: { tone: "good", label: "terminé" },
    RUNNING: { tone: "info", label: "en cours" },
    PENDING: { tone: "neutral", label: "en attente" },
    QUARANTINE: { tone: "warn", label: "quarantaine" },
    FAILED: { tone: "critical", label: "échec" },
  };
  const entry = map[status] ?? { tone: "neutral" as const, label: status };
  return <Badge tone={entry.tone}>{entry.label}</Badge>;
}

/**
 * Choix coût/délai, présenté comme un arbitrage et non comme un réglage.
 *
 * Le prix est affiché sur le bouton lui-même : une décision d'argent se prend
 * sur un chiffre visible, pas dans un menu de configuration.
 */
function ModeChoice({
  active,
  onSelect,
  title,
  price,
  detail,
}: {
  active: boolean;
  onSelect: () => void;
  title: string;
  price: string;
  detail: string;
}) {
  return (
    <button
      onClick={onSelect}
      aria-pressed={active}
      className={`rounded-lg border px-3 py-2 text-left transition-colors ${
        active
          ? "border-s1/60 bg-s1/10"
          : "border-line-strong bg-surface-2 hover:bg-surface-3"
      }`}
    >
      <span className="flex items-baseline justify-between gap-2">
        <span className="text-xs font-medium text-ink">{title}</span>
        <span
          className={`text-[11px] font-semibold ${active ? "text-s1" : "text-ink-3"}`}
        >
          {price}
        </span>
      </span>
      <span className="mt-0.5 block text-[11px] leading-snug text-ink-3">
        {detail}
      </span>
    </button>
  );
}
