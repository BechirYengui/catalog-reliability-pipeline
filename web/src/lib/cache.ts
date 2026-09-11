/**
 * Cache mémoire partagé par les pages.
 *
 * Le défaut qu'il corrige : chaque page gardait ses données dans son propre
 * `useState`. Quitter la page détruisait le composant, donc les données ;
 * y revenir trois secondes plus tard rejouait les mêmes requêtes et repassait
 * par l'écran vide. Mesuré dans le journal nginx sur une seule session de
 * navigation : `/api/products` (40 Ko) demandé 4 fois, `/api/review/tasks`
 * (14 Ko) 6 fois, `/api/pipeline` — une description d'étapes qui ne change
 * JAMAIS — 7 fois.
 *
 * Le principe est celui du « périmé pendant qu'on rafraîchit » : on affiche
 * tout de suite ce qu'on a, on revalide derrière, et l'écran ne se vide qu'à
 * la toute première visite. Deux conséquences voulues :
 *
 * - le spinner ne s'affiche que lorsqu'il n'y a RIEN à montrer. Remplacer des
 *   données correctes par un spinner pour aller les rechercher est une perte
 *   sèche pour l'utilisateur ;
 * - une donnée périmée ne survit jamais à une action qui l'écrit. Après une
 *   fusion ou une décision de revue, `invalidate` remet les entrées
 *   concernées en question tout de suite : afficher du périmé APRÈS un geste
 *   de l'utilisateur serait pire que l'attente qu'on cherche à supprimer.
 *
 * Volontairement une cinquantaine de lignes plutôt qu'une bibliothèque : un
 * seul écran, un seul serveur, et la stack du projet est arrêtée.
 */

import { useCallback, useEffect, useRef, useState } from "react";

interface Entry {
  data: unknown;
  /** Horodatage de la réponse. `0` = marquée périmée par `invalidate`. */
  at: number;
}

const store = new Map<string, Entry>();
const inflight = new Map<string, Promise<unknown>>();
const listeners = new Map<string, Set<() => void>>();

/** Durées de vie, en millisecondes. Elles disent ce qu'on accepte de voir de
 *  retard sur chaque donnée, pas une performance. */
export const TTL = {
  /** Ne change pas d'une session à l'autre : la description du pipeline. */
  fixe: Number.POSITIVE_INFINITY,
  /** Une file de revue bouge sous les doigts de son utilisateur. */
  court: 10_000,
  /** Référentiel, runs, indicateurs : le dépôt suivant est le seul événement
   *  qui les change vraiment, et il passe par `invalidate`. */
  moyen: 45_000,
} as const;

function notify(key: string): void {
  listeners.get(key)?.forEach((callback) => callback());
}

export function peek(key: string): Entry | undefined {
  return store.get(key);
}

export function put<T>(key: string, data: T): void {
  store.set(key, { data, at: Date.now() });
  notify(key);
}

/**
 * Un seul appel réseau par clé, même si trois composants la demandent en même
 * temps. Le `Catalog` demandait déjà `/api/stores` deux fois par montage.
 */
function fetchOnce<T>(key: string, fetcher: () => Promise<T>): Promise<T> {
  const running = inflight.get(key);
  if (running) return running as Promise<T>;

  const promise = fetcher()
    .then((data) => {
      put(key, data);
      return data;
    })
    .finally(() => {
      inflight.delete(key);
    });
  inflight.set(key, promise);
  return promise;
}

/**
 * Remet en question tout ce dont la clé commence par `prefix`, sans effacer :
 * les données restent à l'écran pendant qu'on va chercher les nouvelles.
 */
export function invalidate(prefix: string): void {
  for (const [key, entry] of store) {
    if (key.startsWith(prefix)) {
      store.set(key, { ...entry, at: 0 });
      notify(key);
    }
  }
}

/** Vide tout : à la déconnexion, les données d'un compte ne doivent pas
 *  rester en mémoire pour le suivant. */
export function clearCache(): void {
  store.clear();
  inflight.clear();
  listeners.forEach((set) => set.forEach((callback) => callback()));
}

export interface Resource<T> {
  /** `undefined` seulement à la première visite, avant toute réponse. */
  data: T | undefined;
  /** Vrai uniquement quand il n'y a rien à afficher : c'est le seul cas où
   *  une attente vaut mieux que le contenu précédent. */
  loading: boolean;
  /** Un rafraîchissement en fond, données déjà à l'écran. */
  refreshing: boolean;
  error: string | null;
  reload: () => void;
}

/**
 * Lit une ressource par sa clé. `key === null` = rien à charger pour l'instant
 * (un panneau fermé, un filtre pas encore choisi).
 */
export function useResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  ttl: number = TTL.moyen,
): Resource<T> {
  // Initialisation SYNCHRONE depuis le cache : c'est ce qui évite le clignement
  // d'écran vide au retour sur une page déjà visitée.
  const [entry, setEntry] = useState<Entry | undefined>(() =>
    key ? peek(key) : undefined,
  );
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Le fetcher est souvent une lambda recréée à chaque rendu : la mettre dans
  // les dépendances relancerait une requête à chaque frappe au clavier.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(
    (force: boolean) => {
      if (!key) return;
      const current = peek(key);
      if (!force && current && Date.now() - current.at < ttl) return;
      setRefreshing(true);
      fetchOnce(key, () => fetcherRef.current())
        .then(() => setError(null))
        .catch((exc: unknown) =>
          setError(exc instanceof Error ? exc.message : "chargement impossible"),
        )
        .finally(() => setRefreshing(false));
    },
    [key, ttl],
  );

  useEffect(() => {
    if (!key) {
      setEntry(undefined);
      return;
    }
    // `invalidate` marque l'entrée périmée sans l'effacer : c'est ici qu'un
    // composant monté le voit et va rechercher, ses données toujours à
    // l'écran. Sans cette relance, une fusion n'aurait rafraîchi la table
    // qu'au prochain montage de la page.
    const onChange = () => {
      const current = peek(key);
      setEntry(current);
      if (current && current.at === 0) load(false);
    };
    const set = listeners.get(key) ?? new Set<() => void>();
    set.add(onChange);
    listeners.set(key, set);

    onChange();
    load(false);

    return () => {
      set.delete(onChange);
      if (set.size === 0) listeners.delete(key);
    };
  }, [key, load]);

  return {
    data: entry?.data as T | undefined,
    // Une erreur sans donnée n'est pas un chargement : laisser tourner le
    // sablier ferait attendre pour rien devant un serveur qui a déjà répondu.
    loading: key !== null && entry === undefined && error === null,
    refreshing,
    error,
    reload: () => load(true),
  };
}
