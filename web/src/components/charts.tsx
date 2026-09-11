import { useState } from "react";
import { nf, pf } from "./ui";

/**
 * Entonnoir du pipeline : combien de lignes entrent et sortent de chaque étage.
 *
 * C'est une magnitude ordonnée sur une seule dimension → des barres horizontales,
 * pas un camembert : comparer des longueurs alignées sur une base commune est
 * l'encodage le plus précis, comparer des angles ne l'est pas.
 * Rampe d'une seule teinte, du clair au foncé, parce que les étages sont
 * ordonnés : une palette catégorielle suggérerait à tort qu'ils sont
 * interchangeables.
 */
export function Funnel({
  stages,
}: {
  stages: Array<{ label: string; value: number; note?: string }>;
}) {
  const max = Math.max(...stages.map((s) => s.value), 1);
  const ramp = [
    "var(--ramp-1)",
    "var(--ramp-2)",
    "var(--ramp-3)",
    "var(--ramp-4)",
    "var(--ramp-5)",
    "var(--ramp-6)",
  ];
  return (
    <div className="flex flex-col gap-2">
      {stages.map((stage, index) => {
        const width = (stage.value / max) * 100;
        const previous = index > 0 ? stages[index - 1].value : stage.value;
        const drop = previous > 0 ? 1 - stage.value / previous : 0;
        return (
          <div key={stage.label} className="grid grid-cols-[5.5rem_1fr_auto] items-center gap-2 sm:grid-cols-[10rem_1fr_auto] sm:gap-3">
            <div className="truncate text-xs text-ink-2" title={stage.label}>
              {stage.label}
            </div>
            <div className="relative h-6 rounded bg-surface-2">
              <div
                className="h-6 rounded-r-[4px] transition-[width] duration-500"
                style={{
                  width: `${Math.max(width, 0.6)}%`,
                  background: ramp[Math.min(index, ramp.length - 1)],
                }}
              />
              {stage.note && (
                <span className="pointer-events-none absolute inset-y-0 left-2 flex items-center text-[11px] font-medium text-white mix-blend-luminosity">
                  {stage.note}
                </span>
              )}
            </div>
            <div className="tnum w-20 text-right text-xs sm:w-28">
              <span className="font-semibold text-ink">{nf.format(stage.value)}</span>
              {index > 0 && drop > 0.001 && (
                <span className="ml-1.5 whitespace-nowrap text-ink-3">−{pf.format(drop)}</span>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Classement de catégories par volume. Barres horizontales : les libellés sont
 * longs et se lisent horizontalement sans rotation.
 */
export function BarList({
  items,
  total,
  colorOf,
  onSelect,
  selected,
}: {
  items: Array<{ label: string; value: number; hint?: string }>;
  total?: number;
  colorOf?: (label: string) => string;
  onSelect?: (label: string) => void;
  selected?: string | null;
}) {
  const max = Math.max(...items.map((i) => i.value), 1);
  const [hovered, setHovered] = useState<string | null>(null);

  if (items.length === 0) {
    return <p className="py-6 text-center text-xs text-ink-3">Aucune donnée</p>;
  }

  return (
    <div className="flex flex-col gap-1">
      {items.map((item) => {
        const share = total ? item.value / total : null;
        const active = selected === item.label || hovered === item.label;
        return (
          <button
            key={item.label}
            type="button"
            onClick={onSelect ? () => onSelect(item.label) : undefined}
            onMouseEnter={() => setHovered(item.label)}
            onMouseLeave={() => setHovered(null)}
            className={`group grid w-full grid-cols-[1fr_auto] items-center gap-3 rounded-md px-2 py-1 text-left transition-colors ${
              onSelect ? "cursor-pointer hover:bg-surface-2" : "cursor-default"
            } ${selected === item.label ? "bg-surface-2" : ""}`}
            title={item.hint ?? item.label}
          >
            <div className="min-w-0">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate font-mono text-xs text-ink-2">{item.label}</span>
              </div>
              <div className="mt-1 h-2 rounded bg-surface-2">
                <div
                  className="h-2 rounded-r-[3px] transition-all duration-300"
                  style={{
                    width: `${Math.max((item.value / max) * 100, 1)}%`,
                    background: colorOf ? colorOf(item.label) : "var(--series-1)",
                    opacity: active ? 1 : 0.85,
                  }}
                />
              </div>
            </div>
            <div className="tnum w-16 text-right sm:w-24">
              <div className="text-xs font-semibold text-ink">{nf.format(item.value)}</div>
              {share !== null && (
                <div className="text-[11px] text-ink-3">{pf.format(share)}</div>
              )}
            </div>
          </button>
        );
      })}
    </div>
  );
}

/**
 * Évolution du taux publiable au fil des runs.
 *
 * Une seule série : pas de légende, le titre la nomme. Axe unique, jamais deux
 * échelles superposées. Repère au survol plutôt qu'une valeur sur chaque point,
 * qui saturerait la lecture.
 */
export function TrendLine({
  points,
}: {
  points: Array<{ label: string; value: number; sub?: string }>;
}) {
  const [active, setActive] = useState<number | null>(null);

  if (points.length < 2) {
    return (
      <p className="py-8 text-center text-xs text-ink-3">
        Au moins deux runs sont nécessaires pour dessiner une tendance.
      </p>
    );
  }

  const width = 640;
  const height = 160;
  const pad = { top: 12, right: 12, bottom: 22, left: 34 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;

  const x = (i: number) => pad.left + (i / (points.length - 1)) * innerW;
  const y = (v: number) => pad.top + innerH - v * innerH;

  const path = points.map((p, i) => `${i === 0 ? "M" : "L"} ${x(i)} ${y(p.value)}`).join(" ");
  const area = `${path} L ${x(points.length - 1)} ${pad.top + innerH} L ${x(0)} ${pad.top + innerH} Z`;

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="w-full"
        role="img"
        aria-label="Taux de produits publiables au fil des runs"
        onMouseLeave={() => setActive(null)}
      >
        {[0, 0.25, 0.5, 0.75, 1].map((tick) => (
          <g key={tick}>
            <line
              x1={pad.left}
              x2={width - pad.right}
              y1={y(tick)}
              y2={y(tick)}
              stroke="var(--border)"
              strokeWidth="1"
            />
            <text
              x={pad.left - 6}
              y={y(tick) + 3}
              textAnchor="end"
              className="tnum"
              fontSize="9"
              fill="var(--text-muted)"
            >
              {Math.round(tick * 100)}%
            </text>
          </g>
        ))}

        <path d={area} fill="var(--series-1)" opacity="0.12" />
        <path d={path} fill="none" stroke="var(--series-1)" strokeWidth="2" strokeLinejoin="round" />

        {points.map((p, i) => (
          <circle
            key={p.label + i}
            cx={x(i)}
            cy={y(p.value)}
            r={active === i ? 5 : 3.5}
            fill="var(--series-1)"
            stroke="var(--surface-1)"
            strokeWidth="2"
          />
        ))}

        {/* Zones de survol larges : viser un point de 3 px à la souris est pénible. */}
        {points.map((_, i) => (
          <rect
            key={`hit-${i}`}
            x={x(i) - innerW / points.length / 2}
            y={pad.top}
            width={innerW / points.length}
            height={innerH}
            fill="transparent"
            onMouseEnter={() => setActive(i)}
          />
        ))}

        {active !== null && (
          <line
            x1={x(active)}
            x2={x(active)}
            y1={pad.top}
            y2={pad.top + innerH}
            stroke="var(--border-strong)"
            strokeDasharray="3 3"
          />
        )}
      </svg>

      {active !== null && (
        <div className="pointer-events-none absolute left-1/2 top-0 -translate-x-1/2 rounded-lg border border-line-strong bg-surface-2 px-3 py-1.5 text-xs shadow-lg">
          <div className="font-semibold text-ink">{pf.format(points[active].value)} publiable</div>
          <div className="text-ink-3">{points[active].label}</div>
          {points[active].sub && <div className="text-ink-3">{points[active].sub}</div>}
        </div>
      )}
    </div>
  );
}

/**
 * Répartition d'un total en parts. Barre empilée plutôt qu'un camembert :
 * les parts se comparent sur une longueur commune, et un écart de 2 % reste
 * visible. Un espace de 2 px sépare les segments pour qu'ils ne se fondent pas.
 */
/**
 * Répartition en une barre. `hint` porte une seconde grandeur sous le
 * segment : elle existe parce que deux mesures peuvent décrire le même étage
 * sans dire la même chose — décisions prises et lignes touchées, par exemple.
 * Les mêler dans une seule barre ferait passer l'étage qui décide le moins
 * pour celui qui corrige le plus ; les séparer laisse chacune répondre à sa
 * question.
 */
export function StackedShare({
  segments,
}: {
  segments: Array<{ label: string; value: number; color: string; hint?: string }>;
}) {
  const total = segments.reduce((sum, s) => sum + s.value, 0) || 1;
  return (
    <div>
      <div className="flex h-3 w-full gap-[2px] overflow-hidden rounded">
        {segments.map((segment) => (
          <div
            key={segment.label}
            style={{
              width: `${(segment.value / total) * 100}%`,
              background: segment.color,
            }}
            title={`${segment.label} : ${nf.format(segment.value)}`}
          />
        ))}
      </div>
      <ul className="mt-3 flex flex-wrap gap-x-5 gap-y-2">
        {segments.map((segment) => (
          <li key={segment.label} className="text-xs">
            <div className="flex items-center gap-1.5">
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
                style={{ background: segment.color }}
                aria-hidden="true"
              />
              <span className="text-ink-2">{segment.label}</span>
              <span className="tnum font-semibold text-ink">{nf.format(segment.value)}</span>
              <span className="tnum text-ink-3">{pf.format(segment.value / total)}</span>
            </div>
            {segment.hint && (
              <div className="ml-4 mt-0.5 text-[11px] text-ink-3">{segment.hint}</div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
