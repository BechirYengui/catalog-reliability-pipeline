import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  api,
  type Override,
  type Merge,
  type Product,
  type SourceRow,
  type StoreCatalog,
} from "../lib/api";
import { Badge, Button, Card, EmptyState, Spinner, StatTile, nf, pf } from "../components/ui";
import { invalidate, put, useResource } from "../lib/cache";

export default function Catalog() {
  const [query, setQuery] = useState("");
  // La recherche entre dans la CLE du cache : sans anti-rebond, chaque frappe
  // creerait une entree et une requete. On ne fait donc suivre la saisie qu'a
  // l'arret de la frappe, comme avant.
  const [debounced, setDebounced] = useState("");
  const [onlyPublishable, setOnlyPublishable] = useState(false);
  const [store, setStore] = useState("");
  const [selected, setSelected] = useState<Product | null>(null);
  // Fiches cochées en vue d'une fusion. Un Set d'identifiants plutôt que la
  // liste des fiches : la table se recharge, les identifiants survivent.
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [merging, setMerging] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(query), 200);
    return () => window.clearTimeout(timer);
  }, [query]);

  const productsKey =
    `/api/products?q=${debounced}&store=${store}&pub=${onlyPublishable}`;
  const { data: products = [], loading } = useResource<Product[]>(productsKey, () =>
    api.products({
      q: debounced || undefined,
      only_publishable: onlyPublishable,
      store_id: store || undefined,
    }),
  );

  // La liste des magasins vient du serveur, pas des fiches affichees : celles-ci
  // sont limitees a 100 lignes triees par volume, donc un magasin volumineux
  // masquait tous les autres.
  const { data: catalogs = [] } = useResource<StoreCatalog[]>("/api/stores", api.stores);

  const mergesKey = `/api/merges?store=${store}`;
  const { data: merges = [] } = useResource<Merge[]>(mergesKey, () =>
    api.merges(store || undefined),
  );

  function toggle(id: string) {
    setMergeError(null);
    setChecked((previous) => {
      const next = new Set(previous);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  /** Une fusion change les fiches, les compteurs par magasin et le registre
   *  des fusions : les trois sont remis en question d'un coup. Les tables
   *  restent affichees pendant qu'on va rechercher. */
  function refresh() {
    invalidate("/api/products");
    invalidate("/api/stores");
    invalidate("/api/merges");
    invalidate("/api/metrics");
  }

  async function merge() {
    setMerging(true);
    setMergeError(null);
    try {
      const survivor = await api.mergeProducts([...checked]);
      setChecked(new Set());
      setSelected(survivor);
      refresh();
    } catch (error) {
      // Le serveur explique POURQUOI il refuse (marques ou contenances
      // différentes) : ce message vaut mieux qu'un « échec » générique.
      setMergeError(
        error instanceof ApiError ? error.message : "La fusion a échoué.",
      );
    } finally {
      setMerging(false);
    }
  }

  async function undo(mergeId: string) {
    setMergeError(null);
    try {
      await api.undoMerge(mergeId);
      setSelected(null);
      refresh();
    } catch (error) {
      setMergeError(
        error instanceof ApiError ? error.message : "L'annulation a échoué.",
      );
    }
  }

  /**
   * Ce que la sélection courante a d'incompatible, AVANT d'appeler le serveur.
   *
   * Le serveur refuse déjà ces deux cas, mais laisser l'utilisateur cliquer
   * pour se faire dire non est une mauvaise manière : il doit voir pourquoi ces
   * deux fiches ne peuvent pas être le même produit au moment où il les coche.
   */
  const conflict = useMemo(() => {
    const picked = products.filter((p) => checked.has(p.id));
    if (picked.length < 2) return null;

    const brands = new Set(
      picked.map((p) => (p.brand ?? "").trim().toUpperCase()).filter(Boolean),
    );
    if (brands.size > 1) {
      return `Marques différentes (${[...brands].join(", ")}) : deux références distinctes, même produit apparent.`;
    }
    const sizes = new Set(
      picked
        .filter((p) => p.quantity_value)
        .map((p) => `${p.quantity_value} ${p.quantity_unit ?? ""}`.trim()),
    );
    if (sizes.size > 1) {
      return `Contenances différentes (${[...sizes].join(", ")}) : deux formats ne sont pas le même produit.`;
    }
    return null;
  }, [products, checked]);

  const stores = useMemo(
    () => [...catalogs].sort((a, b) => b.products - a.products),
    [catalogs],
  );
  const total = useMemo(
    () => stores.reduce((sum, c) => sum + c.products, 0),
    [stores],
  );

  const stats = useMemo(() => {
    const publishable = products.filter((p) => p.publishable).length;
    const merged = products.reduce((sum, p) => sum + p.source_rows, 0);
    return { publishable, merged };
  }, [products]);

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="text-lg font-semibold text-ink">Référentiel produit</h1>
        <p className="mt-0.5 text-xs text-ink-3">
          Chaque magasin a son propre catalogue. Une fiche par produit réel,
          issue de la fusion des lignes en double. Redéposer un fichier remplace
          les fiches de ce magasin, il ne s&apos;y ajoute pas.
        </p>
      </div>

      <StorePicker
        stores={stores}
        total={total}
        selected={store}
        onSelect={setStore}
      />

      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile label="Fiches affichées" value={nf.format(products.length)} />
        <StatTile
          label="Publiables"
          value={products.length ? pf.format(stats.publishable / products.length) : "—"}
          tone={stats.publishable / Math.max(products.length, 1) > 0.6 ? "good" : "warn"}
        />
        <StatTile
          label="Lignes sources fusionnées"
          value={nf.format(stats.merged)}
          hint={products.length ? `${(stats.merged / products.length).toFixed(1)}× par fiche` : undefined}
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Rechercher un produit…"
          className="w-full max-w-sm rounded-lg border border-line-strong bg-surface-2 px-3 py-2 text-sm text-ink outline-none focus:border-s1"
        />
        <label className="flex cursor-pointer items-center gap-2 text-xs text-ink-2">
          <input
            type="checkbox"
            checked={onlyPublishable}
            onChange={(e) => setOnlyPublishable(e.target.checked)}
            className="accent-[var(--series-1)]"
          />
          Publiables uniquement
        </label>
        {loading && <Spinner className="text-ink-3" />}
      </div>

      <div className="grid gap-4 lg:grid-cols-[1.5fr_1fr] lg:gap-5">
        <Card
          title="Fiches produit"
          subtitle="Cochez deux fiches qui désignent le même produit pour les fusionner"
        >
          {checked.size > 0 && (
            <div className="mb-3 rounded-lg border border-s1/40 bg-s1/5 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="text-xs font-medium text-ink">
                  {nf.format(checked.size)} fiche(s) sélectionnée(s)
                </span>
                <div className="flex items-center gap-2">
                  <Button variant="ghost" onClick={() => setChecked(new Set())}>
                    Annuler
                  </Button>
                  <Button
                    variant="primary"
                    disabled={checked.size < 2 || merging || conflict !== null}
                    onClick={merge}
                  >
                    {merging ? <Spinner /> : null} Fusionner
                  </Button>
                </div>
              </div>
              {conflict ? (
                <p className="mt-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[11px] leading-relaxed text-ink">
                  {conflict} Ces deux-là ne peuvent pas être fusionnées : si
                  l&apos;information est fausse, c&apos;est la fiche qu&apos;il
                  faut corriger, pas la fusion qu&apos;il faut forcer.
                </p>
              ) : (
                <p className="mt-2 text-[11px] leading-relaxed text-ink-3">
                  Les lignes du magasin des fiches absorbées suivent la fusion.
                  La décision est tracée avec votre nom, rejouée au prochain
                  dépôt de ce magasin, et annulable à tout moment.
                </p>
              )}
              {mergeError && (
                <p className="mt-2 rounded-lg border border-critical/40 bg-critical/10 px-3 py-2 text-[11px] leading-relaxed text-ink">
                  {mergeError}
                </p>
              )}
            </div>
          )}
          {products.length === 0 && !loading ? (
            <EmptyState
              title="Aucun produit"
              hint="Le référentiel se remplit après le traitement d'un premier catalogue."
            />
          ) : (
            <div className="max-h-[36rem] overflow-auto">
              <table className="w-full min-w-[34rem] text-left text-xs">
                <thead className="sticky top-0 bg-surface-1">
                  <tr className="border-b border-line text-ink-3">
                    <th className="w-6 py-2 pr-2 font-medium">
                      <span className="sr-only">Sélection</span>
                    </th>
                    <th className="py-2 pr-3 font-medium">Libellé</th>
                    <th className="py-2 pr-3 font-medium">EAN</th>
                    <th className="py-2 pr-3 text-right font-medium">TVA</th>
                    <th className="py-2 pr-3 text-right font-medium">Lignes</th>
                    <th className="py-2 font-medium">État</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[var(--border)]">
                  {products.map((product) => (
                    <tr
                      key={product.id}
                      onClick={() => setSelected(product)}
                      className={`cursor-pointer hover:bg-surface-2 ${
                        selected?.id === product.id ? "bg-surface-2" : ""
                      }`}
                    >
                      {/* La case arrête le clic : cocher n'est pas ouvrir. */}
                      <td className="py-2 pr-2" onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={checked.has(product.id)}
                          onChange={() => toggle(product.id)}
                          aria-label={`Sélectionner ${product.label}`}
                          className="accent-[var(--series-1)]"
                        />
                      </td>
                      <td className="py-2 pr-3">
                        <div className="font-medium text-ink">
                          {product.label_enriched || product.label}
                        </div>
                        {product.label_enriched && (
                          <div className="font-mono text-[11px] text-ink-3">
                            {product.label}
                          </div>
                        )}
                        <div className="text-[11px] text-ink-3">
                          {product.store_id} ·{" "}
                          {[product.taxonomy_1, product.taxonomy_4].filter(Boolean).join(" › ")}
                        </div>
                      </td>
                      <td className="py-2 pr-3 font-mono text-ink-2">
                        {product.ean ?? (
                          <span className="text-ink-3">
                            {product.internal_code ? `${product.internal_code} (interne)` : "—"}
                          </span>
                        )}
                      </td>
                      <td className="tnum py-2 pr-3 text-right text-ink-2">
                        {product.vat_rate ?? "—"}
                      </td>
                      <td className="tnum py-2 pr-3 text-right text-ink-2">
                        {nf.format(product.source_rows)}
                      </td>
                      <td className="py-2">
                        {product.publishable ? (
                          <Badge tone="good">publiable</Badge>
                        ) : product.url_checked === false ? (
                          <Badge tone="neutral">image non vérifiée</Badge>
                        ) : (
                          <Badge tone="warn">à compléter</Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div className="flex flex-col gap-4 lg:gap-5">
          <Card
            title={selected ? "Fiche produit" : "Détail"}
            subtitle={selected ? undefined : "Sélectionnez une ligne"}
          >
            {selected ? (
              <ProductDetail
                product={selected}
                onUpdated={(fiche) => {
                  setSelected(fiche);
                  // La ligne corrigee est remplacee dans le cache, pas
                  // seulement a l'ecran : sinon un aller-retour de page
                  // rapporterait l'ancienne valeur.
                  put(
                    productsKey,
                    products.map((p) => (p.id === fiche.id ? fiche : p)),
                  );
                  invalidate("/api/audit");
                }}
              />
            ) : (
              <EmptyState title="Aucune fiche sélectionnée" />
            )}
          </Card>

          {merges.length > 0 && (
            <Card
              title="Fusions manuelles"
              subtitle="Décidées par un humain, rejouées à chaque dépôt"
            >
              <ul className="flex flex-col divide-y divide-[var(--border)]">
                {merges.map((merge) => (
                  <li
                    key={merge.id}
                    className="flex items-start justify-between gap-3 py-2"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-xs text-ink">
                        {merge.absorbed_label}
                      </div>
                      <div className="truncate text-[11px] text-ink-3">
                        fusionnée dans {merge.survivor_label} ·{" "}
                        {new Date(merge.created_at).toLocaleString("fr-FR")}
                      </div>
                    </div>
                    <Button variant="ghost" onClick={() => undo(merge.id)}>
                      Annuler
                    </Button>
                  </li>
                ))}
              </ul>
              <p className="mt-3 text-[11px] leading-relaxed text-ink-3">
                Annuler remet la fiche et ses lignes du magasin en place
                immédiatement, et retire la règle apprise pour que le prochain
                dépôt ne refasse pas la fusion. L&apos;historique des
                corrections, lui, garde les deux gestes.
              </p>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Un magasin, un catalogue. Le decompte porte sur la table entiere, pas sur la
 * page affichee : c'est la seule facon de voir qu'un magasin existe alors que
 * ses fiches sont hors de la page.
 */
function StorePicker({
  stores,
  total,
  selected,
  onSelect,
}: {
  stores: StoreCatalog[];
  total: number;
  selected: string;
  onSelect: (store: string) => void;
}) {
  if (stores.length === 0) return null;

  return (
    <div className="flex gap-2 overflow-x-auto pb-1">
      <StoreChip
        label="Tous les magasins"
        count={total}
        hint={`${stores.length} magasin${stores.length > 1 ? "s" : ""}`}
        active={selected === ""}
        onClick={() => onSelect("")}
      />
      {stores.map((catalog) => (
        <StoreChip
          key={catalog.store_id}
          label={catalog.store_id}
          count={catalog.products}
          hint={`${pf.format(catalog.publishable / Math.max(catalog.products, 1))} publiables`}
          active={selected === catalog.store_id}
          onClick={() => onSelect(catalog.store_id)}
        />
      ))}
    </div>
  );
}

function StoreChip({
  label,
  count,
  hint,
  active,
  onClick,
}: {
  label: string;
  count: number;
  hint: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`shrink-0 rounded-xl border px-3 py-2 text-left transition-colors ${
        active
          ? "border-s1/60 bg-s1/10"
          : "border-line bg-surface-1 hover:bg-surface-2"
      }`}
    >
      <div className="text-xs font-medium text-ink">{label}</div>
      <div className="tnum mt-0.5 text-sm font-semibold text-ink">
        {nf.format(count)} <span className="text-[11px] font-normal text-ink-3">fiches</span>
      </div>
      <div className="mt-0.5 text-[11px] text-ink-3">{hint}</div>
    </button>
  );
}

function ProductDetail({
  product,
  onUpdated,
}: {
  product: Product;
  onUpdated: (fiche: Product) => void;
}) {
  const [overrides, setOverrides] = useState<Override[]>([]);

  const rechargerCorrections = useCallback(() => {
    api.overrides(product.id).then(setOverrides).catch(() => setOverrides([]));
  }, [product.id]);

  useEffect(rechargerCorrections, [rechargerCorrections]);

  const corrige = (champ: string) => overrides.find((o) => o.field_name === champ);
  const appliquer = (fiche: Product) => {
    onUpdated(fiche);
    rechargerCorrections();
  };

  const missing: string[] = [];
  if (!product.ean) missing.push("EAN exploitable");
  // Une image non vérifiée n'est pas une image morte : ne pas savoir et
  // savoir que c'est cassé appellent des actions différentes.
  if (!product.url_ok) {
    missing.push(
      product.url_checked === false ? "vérification de l'image" : "image valide",
    );
  }
  if (!product.taxonomy_4) missing.push("taxonomie complète");

  return (
    <div className="flex flex-col gap-4">
      <div>
        <EditableField
          product={product}
          field="label"
          label="Libellé publié"
          value={product.label_enriched || product.label}
          override={corrige("label")}
          onSaved={appliquer}
          strong
        />
        {product.label_enriched ? (
          <div className="mt-1 text-[11px] leading-snug text-ink-3">
            Libellé de caisse d&apos;origine :{" "}
            <span className="font-mono text-ink-2">{product.label}</span>
          </div>
        ) : (
          <div className="mt-0.5 font-mono text-[11px] text-ink-3">
            {product.label_normalized}
          </div>
        )}
      </div>

      {product.url_image && product.url_ok && (
        <img
          src={product.url_image}
          alt=""
          loading="lazy"
          className="h-36 w-full rounded-lg border border-line object-cover"
        />
      )}

      <dl className="grid grid-cols-2 gap-3 text-xs">
        <EditableField
          product={product}
          field="ean"
          label="EAN"
          value={product.ean}
          override={corrige("ean")}
          onSaved={appliquer}
          mono
        />
        <EditableField
          product={product}
          field="internal_code"
          label="Code interne"
          value={product.internal_code}
          override={corrige("internal_code")}
          onSaved={appliquer}
          mono
        />
        <div>
          <dt className="text-ink-3">TVA</dt>
          <dd className="mt-0.5 text-ink-2">
            {product.vat_rate !== null ? `${product.vat_rate} %` : "—"}
          </dd>
          <p className="mt-0.5 text-[10px] leading-snug text-ink-3">
            Non modifiable ici : une TVA se tranche en revue, par un
            administrateur.
          </p>
        </div>
        <EditableField
          product={product}
          field="quantity_value"
          label="Contenance"
          value={product.quantity_value === null ? null : String(product.quantity_value)}
          suffix={product.quantity_unit ?? ""}
          override={corrige("quantity_value")}
          onSaved={appliquer}
        />
        <div className="col-span-2">
          <EditableField
            product={product}
            field="taxonomy_4"
            label="Catégorie la plus fine"
            value={product.taxonomy_4}
            override={corrige("taxonomy_4")}
            onSaved={appliquer}
          />
          <p className="mt-1 text-[11px] text-ink-3">
            {[product.taxonomy_1, product.taxonomy_2, product.taxonomy_3]
              .filter(Boolean)
              .join(" › ") || "—"}
          </p>
        </div>
        <Field label="Lignes fusionnées" value={nf.format(product.source_rows)} />
        <Field label="Statut" value={product.status} />
      </dl>

      <SourceRows product={product} />

      {missing.length > 0 && (
        <div className="rounded-lg border border-warn/40 bg-warn/10 p-3">
          <p className="text-xs font-medium text-warn">Non publiable</p>
          <p className="mt-1 text-[11px] leading-snug text-ink-2">
            Il manque : {missing.join(", ")}. Une marketplace refuserait ou
            dépublierait cette fiche en l&apos;état.
          </p>
        </div>
      )}
    </div>
  );
}

/**
 * Les lignes du fichier magasin que cette fiche represente.
 *
 * Une fiche annonce « 945 lignes fusionnees ». Sans la liste, c'est une
 * affirmation invérifiable, et une fusion erronée reste invisible : ce sont les
 * libellés d'origine qui permettent de la contester. C'est aussi le fil que le
 * système du magasin suit pour retrouver ses propres identifiants.
 */
function SourceRows({ product }: { product: Product }) {
  const [rows, setRows] = useState<SourceRow[] | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setRows(null);
    setOpen(false);
  }, [product.id]);

  useEffect(() => {
    if (!open || rows) return;
    api.sourceRows(product.id).then(setRows).catch(() => setRows([]));
  }, [open, rows, product.id]);

  const labels = useMemo(() => {
    if (!rows) return [];
    const counts = new Map<string, number>();
    for (const row of rows) {
      counts.set(row.source_label, (counts.get(row.source_label) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [rows]);

  return (
    <div className="rounded-lg border border-line bg-surface-2/40 p-3">
      <button
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 text-left"
      >
        <span className="text-xs font-medium text-ink-2">
          Lignes du magasin rattachées
        </span>
        <span className="tnum text-xs font-semibold text-ink">
          {nf.format(product.source_rows)}
          <span className="ml-1.5 text-[11px] font-normal text-ink-3">
            {open ? "masquer" : "voir"}
          </span>
        </span>
      </button>

      {!open && (
        <p className="mt-1.5 text-[11px] leading-snug text-ink-3">
          Ces lignes ne disparaissent pas : chacune garde son identifiant
          d&apos;origine et pointe vers cette fiche, pour que le système du
          magasin retrouve ses produits.
        </p>
      )}

      {open && rows === null && (
        <div className="mt-3 flex items-center gap-2 text-[11px] text-ink-3">
          <Spinner /> Chargement…
        </div>
      )}

      {open && rows !== null && (
        <div className="mt-3">
          <p className="text-[11px] text-ink-3">
            {labels.length === 1
              ? "Un seul libellé d'origine."
              : `${labels.length} libellés d'origine différents, tous rattachés à cette fiche.`}
          </p>
          <ul className="mt-2 flex flex-col gap-1">
            {labels.slice(0, 8).map(([label, count]) => (
              <li
                key={label}
                className="flex items-baseline justify-between gap-3 text-[11px]"
              >
                <span className="truncate font-mono text-ink-2">{label || "(vide)"}</span>
                <span className="tnum shrink-0 text-ink-3">{nf.format(count)}</span>
              </li>
            ))}
          </ul>

          <div className="mt-3 border-t border-line pt-2">
            <p className="text-[11px] text-ink-3">Identifiants du magasin</p>
            <p className="mt-1 break-all font-mono text-[11px] leading-relaxed text-ink-2">
              {rows.slice(0, 12).map((row) => row.row_id).join(", ")}
              {rows.length > 12 && ` … +${nf.format(rows.length - 12)}`}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Un champ que l'humain peut corriger, et dont la correction tient.
 *
 * Le marqueur « corrigé » n'est pas décoratif : le référentiel est recalculé à
 * chaque dépôt, donc il faut pouvoir distinguer ce que le pipeline a produit de
 * ce qu'un humain a imposé, et revenir en arrière sur le second sans toucher au
 * premier.
 */
function EditableField({
  product,
  field,
  label,
  value,
  override,
  onSaved,
  mono,
  strong,
  suffix,
}: {
  product: Product;
  field: string;
  label: string;
  value: string | null;
  override?: Override;
  onSaved: (fiche: Product) => void;
  mono?: boolean;
  strong?: boolean;
  suffix?: string;
}) {
  const [edition, setEdition] = useState(false);
  const [brouillon, setBrouillon] = useState(value ?? "");
  const [busy, setBusy] = useState(false);
  const [erreur, setErreur] = useState<string | null>(null);

  useEffect(() => {
    setBrouillon(value ?? "");
    setEdition(false);
    setErreur(null);
  }, [value, product.id]);

  async function enregistrer() {
    if (busy) return;
    const propre = brouillon.trim();
    if (propre === (value ?? "")) {
      setEdition(false);
      return;
    }
    setBusy(true);
    setErreur(null);
    try {
      onSaved(await api.updateProduct(product.id, field, propre || null));
      setEdition(false);
    } catch (exc) {
      setErreur(
        exc instanceof ApiError && exc.status === 400
          ? "Valeur refusée : ce champ attend un nombre."
          : "La correction n'a pas pu être enregistrée.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function annuler() {
    setBusy(true);
    try {
      onSaved(await api.undoOverride(product.id, field));
    } catch {
      setErreur("L'annulation a échoué.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <dt className="flex items-center gap-1.5 text-ink-3">
        {label}
        {override && <Badge tone="info">corrigé</Badge>}
      </dt>

      {edition ? (
        <dd className="mt-1 flex flex-wrap items-center gap-1.5">
          <input
            autoFocus
            value={brouillon}
            onChange={(e) => setBrouillon(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") enregistrer();
              if (e.key === "Escape") setEdition(false);
            }}
            className={`min-w-0 flex-1 rounded-md border border-line-strong bg-surface-2 px-2 py-1 text-xs text-ink outline-none focus:border-s1 ${
              mono ? "font-mono" : ""
            }`}
          />
          <Button variant="primary" disabled={busy} onClick={enregistrer}>
            {busy ? <Spinner /> : "Enregistrer"}
          </Button>
          <Button variant="ghost" disabled={busy} onClick={() => setEdition(false)}>
            Annuler
          </Button>
        </dd>
      ) : (
        <dd className="mt-0.5 flex items-baseline gap-2">
          <span
            className={`min-w-0 break-words ${
              strong ? "text-sm font-semibold text-ink" : "text-ink-2"
            } ${mono ? "font-mono" : ""}`}
          >
            {value || "—"}
            {suffix ? ` ${suffix}` : ""}
          </span>
          <button
            onClick={() => setEdition(true)}
            className="shrink-0 text-[11px] text-s1 hover:underline"
          >
            modifier
          </button>
          {override && (
            <button
              onClick={annuler}
              disabled={busy}
              title={`Valeur calculée : ${override.previous_value ?? "vide"}`}
              className="shrink-0 text-[11px] text-ink-3 hover:underline"
            >
              rétablir
            </button>
          )}
        </dd>
      )}

      {erreur && <p className="mt-1 text-[11px] text-critical">{erreur}</p>}
    </div>
  );
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-ink-3">{label}</dt>
      <dd className={`mt-0.5 text-ink-2 ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}
