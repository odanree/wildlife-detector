// why-did-you-render — dev-only runtime probe that logs when a
// component re-renders with props/state that are shallow-equal to the
// previous render. Catches the H5-class findings from the React audit
// (unstable callback identity threaded into many children) the moment
// they're introduced, instead of waiting for an audit sweep.
//
// Import order matters: this MUST run before React is imported anywhere
// else in the app — hence the guarded import in main.tsx before any
// React consumer. Tree-shaken out of prod builds by the `import.meta.env.DEV`
// gate at the call site.
//
// Pattern: dev-mode runtime probe (as opposed to CI-time static lint
// or prod-time telemetry sink). Cheap because it never runs in prod.

import React from "react";
import whyDidYouRender from "@welldone-software/why-did-you-render";

whyDidYouRender(React, {
  // Track every function component. The library is smart enough to
  // ignore trivial re-renders; setting this true gives us maximum
  // signal at dev time. Prod builds don't include this module.
  trackAllPureComponents: true,
  // Log renders from hooks too — useState, useReducer, useMemo,
  // useCallback. This is what catches the "unstable callback identity"
  // class (H5 in the audit).
  trackHooks: true,
  // Include what changed in the console output so the operator can see
  // "prop X changed from ref#123 to ref#124 (shallow-equal contents)".
  logOnDifferentValues: false,
  // Collapse the console group so a page full of legitimate renders
  // doesn't drown out the anomalies.
  collapseGroups: true,
});
