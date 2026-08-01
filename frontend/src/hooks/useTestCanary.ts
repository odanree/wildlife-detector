// TEMPORARY canary file for testing the sticky-anti-pattern-comment CI
// behavior added in PR #57. Deliberately trips the
// `react-you-might-not-need-an-effect/no-adjust-state-when-a-prop-changes`
// rule so the anti-pattern gate fails and the sticky comment fires.
//
// This file is on branch test/sticky-antipattern-comment ONLY. Do not
// merge — the PR closes without merging once verification is done.
import { useEffect, useState } from "react";

export function useTestCanary(externalValue: number): number {
  const [mirror, setMirror] = useState(externalValue);
  useEffect(() => {
    setMirror(externalValue);
  }, [externalValue]);
  return mirror;
}
