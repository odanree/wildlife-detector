// Phase 3 canary — expect gate to fail again AND sticky comment to re-post.
// After this phase completes and comment appears, close PR without merging.
import { useEffect, useState } from "react";
export function useTestCanary(externalValue: number): number {
  const [mirror, setMirror] = useState(externalValue);
  useEffect(() => {
    setMirror(externalValue);
  }, [externalValue]);
  return mirror;
}
