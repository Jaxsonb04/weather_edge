import { useEffect } from "react";
import { usePublication } from "../lib/publication";

/** Test-only stand-in for resource hooks that have loaded the exact manifest versions. */
export function PublicationLoaded({ artifacts }: { artifacts: string[] }) {
  const { acknowledgeArtifactLoaded, versionForArtifact } = usePublication();
  const versions = artifacts.map((name) => versionForArtifact(name));

  useEffect(() => {
    artifacts.forEach((name, index) => acknowledgeArtifactLoaded(name, versions[index]));
  }, [acknowledgeArtifactLoaded, artifacts, versions]);

  return null;
}
