import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError, type User } from "../lib/api";
import { Button, Spinner } from "../components/ui";

export default function Login({ onSuccess }: { onSuccess: (user: User) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const user = await api.login(email, password);
      onSuccess(user);
      navigate("/");
    } catch (exc) {
      setError(
        exc instanceof ApiError && exc.status === 401
          ? "Identifiants invalides."
          : "Connexion impossible. Réessayez dans un instant.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-7 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-s1/15">
            <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
              <path
                d="M6 9h14M6 13h9M6 17h11"
                stroke="var(--series-1)"
                strokeWidth="2"
                strokeLinecap="round"
              />
              <circle cx="19" cy="17" r="3" fill="var(--status-good)" />
            </svg>
          </div>
          <h1 className="text-lg font-semibold text-ink">Catalog</h1>
          <p className="mt-1 text-sm text-ink-3">
            Fiabilisation des catalogues produits avant publication
          </p>
        </div>

        <form
          onSubmit={submit}
          className="rounded-xl border border-line bg-surface-1 p-5"
        >
          <label className="block">
            <span className="text-xs font-medium text-ink-2">Adresse e-mail</span>
            <input
              type="email"
              required
              autoFocus
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1.5 w-full rounded-lg border border-line-strong bg-surface-2 px-3 py-2 text-sm text-ink outline-none focus:border-s1"
              placeholder="prenom.nom@ulty.fr"
            />
          </label>

          <label className="mt-4 block">
            <span className="text-xs font-medium text-ink-2">Mot de passe</span>
            <input
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1.5 w-full rounded-lg border border-line-strong bg-surface-2 px-3 py-2 text-sm text-ink outline-none focus:border-s1"
              placeholder="••••••••"
            />
          </label>

          {error && (
            <p
              role="alert"
              className="mt-4 rounded-lg border border-critical/40 bg-critical/10 px-3 py-2 text-xs text-critical"
            >
              {error}
            </p>
          )}

          <Button
            type="submit"
            variant="primary"
            disabled={busy}
            className="mt-5 w-full"
          >
            {busy ? (
              <>
                <Spinner /> Connexion…
              </>
            ) : (
              "Se connecter"
            )}
          </Button>
        </form>

        <p className="mt-4 text-center text-[11px] leading-relaxed text-ink-3">
          Deux rôles : un <strong className="text-ink-2">relecteur</strong> traite
          les libellés et les taxonomies, un{" "}
          <strong className="text-ink-2">administrateur</strong> est seul habilité
          à valider un taux de TVA : l&apos;erreur a une conséquence fiscale.
        </p>
      </div>
    </div>
  );
}
