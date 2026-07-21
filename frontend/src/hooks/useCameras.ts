import { useEffect, useState } from "react";
import { type CamerasResponse, fetchCameras } from "../api/cameras";

interface UseCamerasResult {
  data: CamerasResponse | null;
  error: Error | null;
}

/**
 * One-shot fetch of the camera roster. Not polled — the roster only
 * changes when compose brings a new detector up, which is a manual
 * operator action; a periodic re-fetch would burn requests for no gain.
 */
export function useCameras(): UseCamerasResult {
  const [data, setData] = useState<CamerasResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    fetchCameras(controller.signal)
      .then(setData)
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof Error ? e : new Error(String(e)));
      });
    return () => controller.abort();
  }, []);

  return { data, error };
}
