import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type PublicationState = "fresh" | "stale" | "unknown";

export interface PublicationArtifact {
  generated_at?: string | null;
  sha256?: string | null;
  status?: "ready" | "preserved" | "missing" | string;
}

export interface PublicationManifest {
  schema_version?: number;
  snapshot_id?: string;
  published_at?: string;
  artifacts?: Record<string, PublicationArtifact | undefined>;
}

export interface PublicationFreshness {
  state: PublicationState;
  ageMinutes: number | null;
  generatedAt: string | null;
}

export interface PublicationContextValue {
  manifest: PublicationManifest | null;
  snapshotVersion: string | null;
  artifactHashes: Record<string, string>;
  operational: PublicationFreshness;
  strategy: PublicationFreshness;
  error: string | null;
  versionForArtifact: (name: string) => string | null;
  acknowledgeArtifactLoaded: (name: string, version: string | null) => void;
}

const POLL_INTERVAL_MS = 60_000;
const OPERATIONAL_MAX_AGE_MINUTES = 10;
const STRATEGY_MAX_AGE_MINUTES = 20;
const BASE = import.meta.env.BASE_URL ?? "./";
const UNKNOWN: PublicationFreshness = {
  state: "unknown",
  ageMinutes: null,
  generatedAt: null,
};

const PublicationContext = createContext<PublicationContextValue | null>(null);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isManifest(value: unknown): value is PublicationManifest {
  if (!isRecord(value)) return false;
  return value.artifacts === undefined || isRecord(value.artifacts);
}

function freshnessFor(
  manifest: PublicationManifest | null,
  artifactNames: string[],
  loadedArtifactVersions: Record<string, string>,
  maxAgeMinutes: number,
  now: number,
): PublicationFreshness {
  if (!manifest?.artifacts) return UNKNOWN;

  const timestamps: { iso: string; time: number }[] = [];
  for (const name of artifactNames) {
    const artifact = manifest.artifacts[name];
    if (!artifact || (artifact.status !== "ready" && artifact.status !== "preserved")) return UNKNOWN;
    const expectedVersion = artifact.sha256 ?? manifest.snapshot_id;
    if (!expectedVersion || loadedArtifactVersions[name] !== expectedVersion) return UNKNOWN;
    const iso = artifact.generated_at;
    if (typeof iso !== "string" || !iso) return UNKNOWN;
    const time = Date.parse(iso);
    if (Number.isNaN(time)) return UNKNOWN;
    timestamps.push({ iso, time });
  }

  const oldest = timestamps.reduce((candidate, value) =>
    value.time < candidate.time ? value : candidate,
  );
  const ageMinutes = Math.max(0, (now - oldest.time) / 60_000);
  return {
    state: ageMinutes > maxAgeMinutes ? "stale" : "fresh",
    ageMinutes,
    generatedAt: oldest.iso,
  };
}

export function PublicationProvider({ children }: { children: ReactNode }) {
  const [manifest, setManifest] = useState<PublicationManifest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [loadedArtifactVersions, setLoadedArtifactVersions] = useState<Record<string, string>>({});

  const acknowledgeArtifactLoaded = useCallback((name: string, version: string | null) => {
    if (!version) return;
    setLoadedArtifactVersions((current) =>
      current[name] === version ? current : { ...current, [name]: version },
    );
  }, []);

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const controller = new AbortController();

    const refresh = async () => {
      try {
        const response = await fetch(`${BASE}publication_manifest.json`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`publication_manifest.json: HTTP ${response.status}`);
        const payload: unknown = await response.json();
        if (!isManifest(payload)) throw new Error("publication_manifest.json: invalid manifest");
        if (alive) {
          setManifest(payload);
          setError(null);
          setNow(Date.now());
        }
      } catch (reason) {
        if (alive && !controller.signal.aborted) {
          setError(String(reason));
          setNow(Date.now());
        }
      } finally {
        if (alive) {
          timer = window.setTimeout(() => {
            setNow(Date.now());
            void refresh();
          }, POLL_INTERVAL_MS);
        }
      }
    };

    void refresh();
    return () => {
      alive = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

  const value = useMemo<PublicationContextValue>(() => {
    const artifactHashes: Record<string, string> = {};
    for (const [name, artifact] of Object.entries(manifest?.artifacts ?? {})) {
      if (typeof artifact?.sha256 === "string" && artifact.sha256) artifactHashes[name] = artifact.sha256;
    }
    const snapshotVersion =
      typeof manifest?.snapshot_id === "string" && manifest.snapshot_id ? manifest.snapshot_id : null;
    return {
      manifest,
      snapshotVersion,
      artifactHashes,
      operational: freshnessFor(
        manifest,
        ["trading_signal.json", "cities_data.json"],
        loadedArtifactVersions,
        OPERATIONAL_MAX_AGE_MINUTES,
        now,
      ),
      strategy: freshnessFor(
        manifest,
        ["strategy_research.json"],
        loadedArtifactVersions,
        STRATEGY_MAX_AGE_MINUTES,
        now,
      ),
      error,
      versionForArtifact: (name: string) => artifactHashes[name] ?? snapshotVersion,
      acknowledgeArtifactLoaded,
    };
  }, [acknowledgeArtifactLoaded, error, loadedArtifactVersions, manifest, now]);

  return <PublicationContext.Provider value={value}>{children}</PublicationContext.Provider>;
}

// Provider and hook intentionally share one public module.
// oxlint-disable-next-line react/only-export-components
export function usePublication(): PublicationContextValue {
  const value = useContext(PublicationContext);
  if (!value) throw new Error("usePublication must be used inside PublicationProvider");
  return value;
}
