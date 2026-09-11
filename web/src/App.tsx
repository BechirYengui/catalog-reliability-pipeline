import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api, type User } from "./lib/api";
import { clearCache } from "./lib/cache";
import { Badge, Button, Spinner } from "./components/ui";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import RunView from "./pages/RunView";
import Review from "./pages/Review";
import Catalog from "./pages/Catalog";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const logout = useCallback(async () => {
    await api.logout().catch(() => undefined);
    // Les donnees d'un compte ne restent pas en memoire pour le suivant : le
    // cache est un confort de navigation, pas un contournement de session.
    clearCache();
    setUser(null);
    navigate("/login");
  }, [navigate]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-ink-3">
        <Spinner /> Chargement…
      </div>
    );
  }

  if (!user) {
    return (
      <Routes>
        <Route path="/login" element={<Login onSuccess={setUser} />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <Header user={user} onLogout={logout} />
      <main className="mx-auto w-full max-w-[100rem] flex-1 px-3 py-4 sm:px-5 sm:py-5">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/runs/:runId" element={<RunView user={user} />} />
          <Route path="/traitement" element={<RunView user={user} />} />
          <Route path="/revue" element={<Review user={user} />} />
          <Route path="/referentiel" element={<Catalog />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Header({ user, onLogout }: { user: User; onLogout: () => void }) {
  const tabs = [
    { to: "/", label: "Tableau de bord", end: true },
    { to: "/traitement", label: "Traiter un fichier" },
    { to: "/revue", label: "Revue humaine" },
    { to: "/referentiel", label: "Référentiel" },
  ];

  return (
    <header className="sticky top-0 z-20 border-b border-line bg-surface-1/95 backdrop-blur">
      <div className="mx-auto flex w-full max-w-[100rem] items-center gap-3 px-3 py-2.5 sm:gap-6 sm:px-5">
        <div className="flex shrink-0 items-center gap-2">
          <Logo />
          {/* Le sous-titre est le premier a partir : il informe, il ne sert pas. */}
          <div className="leading-tight">
            <div className="text-sm font-semibold text-ink">Catalog</div>
            <div className="hidden text-[11px] text-ink-3 lg:block">Fiabilisation produit</div>
          </div>
        </div>

        {/* Defilement horizontal plutot que retour a la ligne : la barre garde
            une hauteur constante et aucun onglet ne devient inaccessible. */}
        <nav className="-mx-1 flex min-w-0 flex-1 items-center gap-1 overflow-x-auto px-1
                        [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {tabs.map((tab) => (
            <NavLink
              key={tab.to}
              to={tab.to}
              end={tab.end}
              className={({ isActive }) =>
                `whitespace-nowrap rounded-lg px-2.5 py-1.5 text-sm font-medium transition-colors sm:px-3 ${
                  isActive
                    ? "bg-surface-3 text-ink"
                    : "text-ink-2 hover:bg-surface-2 hover:text-ink"
                }`
              }
            >
              {tab.label}
            </NavLink>
          ))}
        </nav>

        <div className="flex shrink-0 items-center gap-2 sm:gap-3">
          {/* L'identite complete est un confort : sur petit ecran le role suffit.
              Le pouvoir attache au role n'a pas sa place dans la barre : il se
              lit la ou il s'exerce, sur la decision qui le reclame. */}
          <div className="hidden text-right leading-tight xl:block">
            <div className="text-xs text-ink-2">{user.email}</div>
          </div>
          <Badge tone={user.role === "admin" ? "info" : "neutral"}>{user.role}</Badge>
          <Button variant="ghost" onClick={onLogout} title="Déconnexion">
            <span className="hidden sm:inline">Déconnexion</span>
            <span className="sm:hidden" aria-hidden="true">⏻</span>
            <span className="sr-only sm:hidden">Déconnexion</span>
          </Button>
        </div>
      </div>
    </header>
  );
}

function Logo() {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
      <rect x="1" y="1" width="24" height="24" rx="6" fill="var(--series-1)" opacity="0.16" />
      <path d="M6 9h14M6 13h9M6 17h11" stroke="var(--series-1)" strokeWidth="2" strokeLinecap="round" />
      <circle cx="19" cy="17" r="3" fill="var(--status-good)" />
    </svg>
  );
}
